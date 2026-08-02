"""智能体工作流管理器单元测试。

本脚本验证并行子 agent 编排、scoped_workdir 权限审计、workflow tool 入参传递、策略注册分发和模型 preset 覆盖行为。
作用是确保 AgentWorkflowManager 写入完整审计产物，保持子 agent 上下文隔离，并通过测试文件复现边界输入。
关键执行流程：构造 fake LLM 和 SessionEngine，运行 workflow manager 或 agent_workflow_tool，读取 workflow 产物和审计日志进行断言。
关键函数：_runtime 构造测试 runtime，_audit_records 读取审计日志，各 test_* 函数覆盖并行执行、失败收口、权限隔离、策略分发和 CLI preset。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import application.agent_workflows.manager as workflow_manager_module
from application.agent_roles import AgentRoleManager
from application.agent_workflows.manager import (
    AgentWorkflowManager,
    SubAgentReportProjection,
)
from application.agent_workflows.strategies.base import WorkflowRunRequest, WorkflowStrategyNotFound
from application.agent_workflows.task_models import SubAgentTask
from application.subagents.permissions import SubAgentPermissionSpec
from core.agent_spec import AgentSpec
from core.contracts import (
    LLMRequest,
    LLMResponse,
    ProviderUsageFamily,
    ProviderUsageSnapshot,
    ToolContext,
)
from core.message import Message, ToolCall
from core.runner import Runner
from hosts.cli.main import _apply_model_preset_or_exit
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.models import Config, ModelSelectionConfig
from infrastructure.llm_providers.usage import ProviderUsageManager
from runtime_assembly.session_engine import SessionEngine
from sessions import SessionBootstrap, build_session
from tests.support.workflow_agent_tree import (
    WorkflowAgentTreeBinding,
    bind_workflow_agent_tree,
)
from tools import AutoAllowApproval, ToolRegistry
from tools.agent_workflow_tool import AgentWorkflowHandle, build_agent_workflow_tool
from tools.builtin.file_tool import build_file_tools


def _anthropic_usage(raw_usage: dict[str, Any]) -> ProviderUsageSnapshot:
    """经真实 Manager 构造 Anthropic 测试快照。"""
    return ProviderUsageManager().normalize(
        family=ProviderUsageFamily.ANTHROPIC_MESSAGES,
        raw_usage=raw_usage,
    )


class _EchoLLM:
    """测试用回声 LLM，根据用户文本返回固定子任务结果。"""

    def __init__(self) -> None:
        """初始化回声 LLM，输入为空，输出为可记录请求的实例。"""
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """完成一次 LLM 请求，输入为 LLMRequest，输出为按文本匹配生成的 LLMResponse。"""
        self.calls.append(request)
        user_text = "\n".join(
            message.content or "" for message in request.messages if message.role == "user"
        )
        if "7 + 5" in user_text:
            content = "12"
        elif "alpha" in user_text:
            content = "alpha done"
        elif "beta" in user_text:
            content = "beta done"
        else:
            content = "done"
        usage_by_content = {
            "alpha done": {
                "input_tokens": 11,
                "output_tokens": 3,
                "cache_read_input_tokens": 2,
                "provider_extra_tokens": 7,
                "provider_label": "alpha",
            },
            "beta done": {
                "input_tokens": 13,
                "output_tokens": 4,
                "cache_creation_input_tokens": 5,
                "provider_extra_tokens": 9,
            },
        }
        return LLMResponse(
            message=Message.assistant(content),
            finish_reason="stop",
            usage=(
                _anthropic_usage(usage_by_content[content]) if content in usage_by_content else None
            ),
        )


class _WorkflowLLM:
    """测试用 workflow LLM，先请求创建子 agent，再返回父子任务结果。"""

    def __init__(self) -> None:
        """初始化 workflow LLM，输入为空，输出为可记录请求的实例。"""
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """完成 workflow 请求，输入为 LLMRequest，输出为工具调用或固定文本回复。"""
        self.calls.append(request)
        tool_names = {tool.name for tool in request.tools}
        has_workflow_result = any(
            message.role == "tool" and message.name == "run_parallel_subagents"
            for message in request.messages
        )
        if "run_parallel_subagents" in tool_names and not has_workflow_result:
            return LLMResponse(
                message=Message.assistant(
                    tool_calls=[
                        ToolCall(
                            call_id="call-workflow",
                            tool_name="run_parallel_subagents",
                            arguments={
                                "tasks": [
                                    {
                                        "task_name": "alpha",
                                        "prompt": "alpha child task",
                                        "permission": {"mode": "scoped_workdir"},
                                    },
                                    {
                                        "task_name": "beta",
                                        "prompt": "beta child task",
                                        "permission": {"mode": "scoped_workdir"},
                                    },
                                ],
                            },
                        )
                    ]
                ),
                finish_reason="tool_calls",
            )
        user_text = "\n".join(
            message.content or "" for message in request.messages if message.role == "user"
        )
        if "alpha child task" in user_text:
            return LLMResponse(
                message=Message.assistant("alpha child result"), finish_reason="stop"
            )
        if "beta child task" in user_text:
            return LLMResponse(message=Message.assistant("beta child result"), finish_reason="stop")
        return LLMResponse(message=Message.assistant("parent summary"), finish_reason="stop")


class _ToolCallingLLM:
    """测试用工具调用 LLM，用于驱动子 agent 调用指定工具。"""

    def __init__(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        final_content: str = "tool result observed",
    ) -> None:
        """初始化工具调用 LLM，输入为工具名、参数和最终文本，输出为可记录请求的实例。"""
        self.tool_name = tool_name
        self.arguments = arguments
        self.final_content = final_content
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """完成工具调用请求，输入为 LLMRequest，输出为工具调用或观察后的最终回复。"""
        self.calls.append(request)
        has_tool_result = any(message.role == "tool" for message in request.messages)
        if has_tool_result:
            return LLMResponse(
                message=Message.assistant(self.final_content),
                finish_reason="stop",
                usage=_anthropic_usage(
                    {
                        "input_tokens": 7,
                        "output_tokens": 3,
                        "cache_read_input_tokens": 2,
                        "provider_extra_tokens": 11,
                    }
                ),
            )
        return LLMResponse(
            message=Message.assistant(
                tool_calls=[
                    ToolCall(
                        call_id="call-child-tool",
                        tool_name=self.tool_name,
                        arguments=self.arguments,
                    )
                ]
            ),
            finish_reason="tool_calls",
            usage=_anthropic_usage(
                {
                    "input_tokens": 5,
                    "output_tokens": 2,
                    "cache_creation_input_tokens": 1,
                    "provider_extra_tokens": 13,
                }
            ),
        )


class _ProgressAgentManager:
    """测试用 AgentManager 投影，按 source_task_id 回灌完成或失败 child_result。"""

    def __init__(self) -> None:
        """初始化父 cell 和 child 索引，输入为空，输出可执行 fake。"""
        self.root = SimpleNamespace(
            agent_id="root-agent",
            session_id="parent-session",
            mailbox=asyncio.Queue(),
        )
        self.children: dict[str, object] = {}

    def get_agent(self, agent_id: str) -> object | None:
        """查询父/子 cell，输入为 agent id，输出匹配对象。"""
        if agent_id == self.root.agent_id:
            return self.root
        return self.children.get(agent_id)

    def spawn(self, request: Any) -> object:
        """回灌测试 child_result，输入为 SpawnAgentRequest，输出 dispatched 投影。"""
        task_id = request.source_task_id or "task"
        child_id = f"child-{task_id}"
        child_session_id = request.child_session_id or f"child-session-{task_id}"
        self.children[child_id] = SimpleNamespace(
            agent_id=child_id,
            session_id=child_session_id,
        )
        metadata: dict[str, object] = {}
        if task_id == "fail":
            metadata["child_error_reason"] = "child exploded"
        self.root.mailbox.put_nowait(
            SimpleNamespace(
                kind="child_result",
                task_id=f"spawn-{task_id}",
                payload=Message.assistant(f"{task_id} done", metadata=metadata),
            )
        )
        return SimpleNamespace(child_id=child_id, task_id=f"spawn-{task_id}")


class _RejectingSubagents:
    """测试用旧子 agent 管理器，记录 run_task 调用并返回失败信号。"""

    def __init__(self) -> None:
        """初始化调用计数，输入为空，输出可断言 fake。"""
        self.run_task_calls = 0

    async def run_task(self, **_kwargs: Any) -> Any:
        """记录旧路径调用，输入为任意参数，输出异常以暴露错误分支。"""
        self.run_task_calls += 1
        raise AssertionError("legacy subagent path called")


class _RuntimeBackedRejectingSubagents(_RejectingSubagents):
    """测试用旧 subagent manager，提供 runtime 供 spawn path 构造 run overrides。"""

    def __init__(self, cfg: Config) -> None:
        """初始化 fake，输入为配置，输出带 runtime 的 rejecting subagents。"""
        super().__init__()
        self.runtime = SimpleNamespace(
            tools=ToolRegistry(build_file_tools()),
            approval=AutoAllowApproval(),
            config=cfg,
        )


class _FakeWorkflowSpawnAgentManager:
    """测试用 AgentManager，记录 spawn request 并向父 mailbox 投递 child_result。"""

    def __init__(self, root: Any) -> None:
        """初始化 fake manager，输入为 root cell，输出可记录 spawn 的实例。"""
        self.root = root
        self.requests: list[Any] = []
        self.child = SimpleNamespace(agent_id="child-alpha", session_id="parent-session-child")

    def get_agent(self, agent_id: str) -> Any | None:
        """查询 fake cell，输入为 agent_id，输出 root / child / None。"""
        if agent_id == self.root.agent_id:
            return self.root
        if agent_id == self.child.agent_id:
            return self.child
        return None

    def spawn(self, request: Any) -> Any:
        """记录 request 并投递测试 mail，输入为 SpawnAgentRequest，输出 SpawnResult 形态。"""
        self.requests.append(request)
        spawn_result = SimpleNamespace(child_id=self.child.agent_id, task_id="spawn-task-alpha")
        self.root.mailbox.put_nowait(
            SimpleNamespace(
                kind="system_notice",
                task_id="unmatched",
                payload=Message.user("unmatched"),
            )
        )
        self.root.mailbox.put_nowait(
            SimpleNamespace(
                kind="child_result",
                task_id=spawn_result.task_id,
                payload=Message.assistant(
                    "spawn path done",
                    metadata={
                        "usage": _anthropic_usage(
                            {"input_tokens": 5, "output_tokens": 2}
                        ).to_payload()
                    },
                ),
            )
        )
        return spawn_result


class _OutOfOrderWorkflowSpawnAgentManager:
    """测试用 AgentManager，先投递 beta 结果再投递 alpha 结果，覆盖 demux 缓存。"""

    def __init__(self, root: Any) -> None:
        """初始化 fake manager，输入为 root cell，输出可记录 spawn 的实例。"""
        self.root = root
        self.requests: list[Any] = []
        self.children: dict[str, Any] = {}

    def get_agent(self, agent_id: str) -> Any | None:
        """查询 fake cell，输入为 agent_id，输出 root / child / None。"""
        if agent_id == self.root.agent_id:
            return self.root
        return self.children.get(agent_id)

    def spawn(self, request: Any) -> Any:
        """记录 request 并乱序投递结果，输入为 request，输出 SpawnResult 形态。"""
        self.requests.append(request)
        source_task_id = request.source_task_id or f"task-{len(self.requests)}"
        child_id = f"child-{source_task_id}"
        self.children[child_id] = SimpleNamespace(
            agent_id=child_id,
            session_id=request.child_session_id or f"session-{source_task_id}",
        )
        spawn_result = SimpleNamespace(child_id=child_id, task_id=f"spawn-{source_task_id}")
        if source_task_id == "alpha":
            self.root.mailbox.put_nowait(
                SimpleNamespace(
                    kind="child_result",
                    task_id="spawn-beta",
                    payload=Message.assistant("beta done"),
                )
            )
            self.root.mailbox.put_nowait(
                SimpleNamespace(
                    kind="child_result",
                    task_id="spawn-alpha",
                    payload=Message.assistant("alpha done"),
                )
            )
        return spawn_result


class _FakeSessionTaskProgressManager:
    """测试用进度 Manager，记录初始化和运行时状态迁移。"""

    instances: list[_FakeSessionTaskProgressManager] = []

    def __init__(self) -> None:
        """初始化 fake manager，输入为空，输出为带调用记录的实例。"""
        self.calls: list[dict[str, object]] = []

    @classmethod
    def from_config(cls, config: Config) -> _FakeSessionTaskProgressManager:
        """按生产合同构造 fake manager，输入为 Config，输出为记录实例。"""
        manager = cls()
        cls.instances.append(manager)
        return manager

    def open_workflow(
        self,
        *,
        session_id: str,
        workflow_id: str,
        title: str,
        control_mode: object,
        tasks: list[object],
    ) -> None:
        """记录前台 workflow 初始化，输入为坐标和不可变骨架，输出为调用记录。"""
        self.calls.append(
            {
                "kind": "open",
                "session_id": session_id,
                "workflow_id": workflow_id,
                "title": title,
                "control_mode": control_mode,
                "tasks": tasks,
            }
        )

    def record_runtime_transition(
        self,
        *,
        session_id: str,
        workflow_id: str,
        task_id: str,
        runtime_status: object,
        error_message: str | None = None,
    ) -> None:
        """记录 runtime 事实，输入为任务状态和错误，输出为调用记录。"""
        self.calls.append(
            {
                "kind": "transition",
                "session_id": session_id,
                "workflow_id": workflow_id,
                "task_id": task_id,
                "runtime_status": runtime_status,
                "error_message": error_message,
            }
        )


class _FailingSessionTaskProgressManager:
    """测试用失败进度 Manager，模拟磁盘或权限错误。"""

    @classmethod
    def from_config(cls, config: Config) -> _FailingSessionTaskProgressManager:
        """按生产合同构造失败 fake manager，输入为 Config，输出为实例。"""
        del config
        return cls()

    def open_workflow(
        self,
        *,
        session_id: str,
        workflow_id: str,
        title: str,
        control_mode: object,
        tasks: list[object],
    ) -> None:
        """模拟进度初始化失败，输入为 workflow 骨架，输出为 RuntimeError。"""
        del session_id, workflow_id, title, control_mode, tasks
        raise RuntimeError("progress disk unavailable")

    def record_runtime_transition(
        self,
        *,
        session_id: str,
        workflow_id: str,
        task_id: str,
        runtime_status: object,
        error_message: str | None = None,
    ) -> None:
        """模拟生命周期落盘失败，输入为状态事实，输出为 RuntimeError。"""
        del session_id, workflow_id, task_id, runtime_status, error_message
        raise RuntimeError("progress disk unavailable")


def _config(tmp_path: Path) -> Config:
    """构造测试配置，输入为临时目录，输出为 file session 和 auto_allow 审批配置。"""
    cfg = Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"))
    cfg.session.backend = "file"
    cfg.session.file_store_path = str(tmp_path / "sessions")
    cfg.approval.mode = "auto_allow"
    return cfg


def test_workflow_dir_resolves_kongming_home_session_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 workflow 目录复用统一路径解析，输入为默认相对路径，输出为 KONGMING_HOME 下目录。"""
    home = tmp_path / "home"
    monkeypatch.setenv("KONGMING_HOME", str(home))
    cfg = _config(tmp_path)
    cfg.session.file_store_path = ".kongming/sessions"
    manager = AgentWorkflowManager(
        config=cfg,
        workspace_root=tmp_path / "workspace",
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )

    workflow_dir = manager._workflow_dir(
        parent_session_id="parent-session",
        workflow_id="wf-test",
    )

    assert workflow_dir == home / "sessions" / "parent-session" / "agent-workflows" / "wf-test"


def _runtime(
    tmp_path: Path,
    llm: Any,
    *,
    tools: ToolRegistry | None = None,
) -> tuple[Config, SessionEngine]:
    """构造测试 runtime，输入为临时目录、fake LLM 和可选工具表，输出为配置和 SessionEngine。"""
    cfg = _config(tmp_path)
    bootstrap = SessionBootstrap(
        agent_name="test-agent",
        model_name="gemma-4-e4b-it",
        instruction_sources=[],
        instruction_text_hash="test",
        created_at=1.0,
        cwd=str(tmp_path),
    )

    def session_factory(sid: str):  # type: ignore[no-untyped-def]
        """构造测试 session，输入为 session ID，输出为 file backend session。"""
        return build_session(cfg, sid, bootstrap=bootstrap)

    registry = tools or ToolRegistry()
    enabled_tool_names = [tool.name for tool in registry.all_tools()]
    catalog_manager = ModelCatalogManager()
    model_config = catalog_manager.resolve_runtime(cfg.model)
    runtime = SessionEngine(
        config=cfg,
        runner=Runner(),
        llm=llm,
        tools=registry,
        enabled_tool_names=enabled_tool_names,
        approval=AutoAllowApproval(),
        session_factory=session_factory,
        event_sinks=[],
        model_catalog_manager=catalog_manager,
        model_config=model_config,
        agent_spec=AgentSpec(
            name="parent",
            instructions="parent instructions",
            default_model="gemma-4-e4b-it",
            tool_names=tuple(enabled_tool_names),
        ),
    )
    return cfg, runtime


def _audit_records(workflow_dir: Path) -> list[dict[str, Any]]:
    """读取 workflow 审计日志，输入为 workflow 目录，输出为 audit.jsonl 记录列表。"""
    return [
        json.loads(line)
        for line in (workflow_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def _workflow_manager(
    *,
    runtime: SessionEngine,
    config: Config,
    workspace_root: Path,
) -> tuple[AgentWorkflowManager, WorkflowAgentTreeBinding]:
    """装配真实 workflow agent 树，输入为 runtime/config，输出 manager 与清理绑定。"""
    binding = bind_workflow_agent_tree(runtime)
    return (
        AgentWorkflowManager(
            runtime=runtime,
            agent_manager=binding.manager,
            config=config,
            workspace_root=workspace_root,
            role_manager=AgentRoleManager(role_dir=workspace_root / "roles"),
        ),
        binding,
    )


@pytest.mark.asyncio
async def test_parallel_workflow_writes_audit_and_keeps_child_contexts_isolated(
    tmp_path: Path,
) -> None:
    """验证并行 workflow 审计和上下文隔离，输入为临时目录，输出为产物与审计断言。"""
    llm = _EchoLLM()
    cfg, runtime = _runtime(tmp_path, llm)
    manager, binding = _workflow_manager(
        runtime=runtime,
        config=cfg,
        workspace_root=tmp_path,
    )
    try:
        result = await manager.run_workflow_payload(
            mode="parallel",
            parent_session_id="parent-session",
            parent_agent=binding.parent_agent,
            desc="  梳理现有 thread/session 路径与顶部工具栏结构  ",
            payload={
                "task_specs": [
                    {
                        "task_id": "alpha",
                        "task_name": "alpha",
                        "prompt": "alpha task",
                    },
                    {
                        "task_id": "beta",
                        "task_name": "beta",
                        "prompt": "beta task",
                    },
                ]
            },
        )
    finally:
        await binding.aclose()
        await runtime.aclose()

    assert result.completed is True
    assert result.desc == "梳理现有 thread/session 路径与顶部工具栏结构"
    assert result.workflow_dir.is_dir()
    assert result.report_index_path.is_file()
    workflow = json.loads((result.workflow_dir / "workflow.json").read_text(encoding="utf-8"))
    assert workflow["status"] == "completed"
    assert workflow["desc"] == "梳理现有 thread/session 路径与顶部工具栏结构"
    assert len(workflow["assigned_agents"]) == 2

    audit_records = [
        json.loads(line)
        for line in (result.workflow_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    audit_actions = [record["action"] for record in audit_records]
    assert audit_actions.count("workflow_started") == 1
    assert audit_actions.count("agent_assigned") == 2
    assert audit_actions.count("agent_completed") == 2
    assert audit_actions.count("subagent_reported") == 2
    assert audit_actions.count("workflow_completed") == 1
    assert all("subagent_runtime" not in record["payload"] for record in audit_records)
    assert all("runtime_spec" not in record["payload"] for record in audit_records)
    assert all(
        record["payload"]["resolved_runtime"]["model"] == "gemma-4-e4b-it"
        for record in audit_records
    )

    report_index = json.loads(result.report_index_path.read_text(encoding="utf-8"))
    assert report_index["status"] == "completed"
    assert report_index["desc"] == "梳理现有 thread/session 路径与顶部工具栏结构"
    assert [report["task_id"] for report in report_index["reports"]] == ["alpha", "beta"]
    assert [report["display_order"] for report in report_index["reports"]] == [1, 2]
    assert "content" not in report_index["reports"][0]

    workflow_result = json.loads((result.workflow_dir / "result.json").read_text(encoding="utf-8"))
    assert workflow_result["report_index_path"] == str(result.report_index_path)
    assert workflow_result["desc"] == "梳理现有 thread/session 路径与顶部工具栏结构"
    assert [report["task_id"] for report in workflow_result["reports"]] == ["alpha", "beta"]
    assert [run.usage for run in result.runs] == [
        {
            "input_uncached_tokens": 11,
            "cache_read_tokens": 2,
            "output_total_tokens": 3,
        },
        {
            "input_uncached_tokens": 13,
            "cache_write_tokens": 5,
            "output_total_tokens": 4,
        },
    ]
    assert workflow_result["runs"][0]["usage"] == result.runs[0].usage
    assert workflow_result["reports"][0]["usage"] == result.runs[0].usage
    assert report_index["reports"][0]["usage"] == result.runs[0].usage
    assert "provider_label" not in workflow_result["runs"][0]["usage"]

    user_messages = [
        "\n".join(message.content or "" for message in call.messages if message.role == "user")
        for call in llm.calls
    ]
    assert any("alpha task" in text for text in user_messages)
    assert any("beta task" in text for text in user_messages)
    assert all("alpha task" not in text or "beta task" not in text for text in user_messages)

    for index, run in enumerate(result.runs):
        session_dir = Path(cfg.session.file_store_path) / run.session_id
        assert session_dir.is_dir()
        assert (session_dir / f"{run.session_id}.jsonl").is_file()
        report_path = Path(result.reports[index].report_path)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        expected_digest = f"sha256:{hashlib.sha256(report['content'].encode()).hexdigest()}"
        assert report["task_id"] == run.task.task_id
        assert report["status"] == "completed"
        assert report["content_digest"] == expected_digest
        assert report["summary"]
        assert report["usage"] == run.usage
        agent_result = json.loads(
            (
                result.workflow_dir
                / "agents"
                / f"{index + 1:03d}-{run.task.task_id}"
                / "result.json"
            ).read_text(encoding="utf-8")
        )
        assert agent_result["usage"] == run.usage
        completed = next(
            record["payload"]
            for record in audit_records
            if record["action"] == "agent_completed"
            and record["payload"]["task_id"] == run.task.task_id
        )
        assert completed["usage"] == run.usage
        assert completed["resolved_runtime"]["model"] == "gemma-4-e4b-it"
        reported = next(
            record["payload"]
            for record in audit_records
            if record["action"] == "subagent_reported"
            and record["payload"]["task_id"] == run.task.task_id
        )
        assert reported["task_run_id"] == f"{index + 1:03d}-{run.task.task_id}"
        assert reported["report_path"] == str(report_path)
        assert reported["content_digest"] == report["content_digest"]
        assert reported["usage"] == run.usage
        assert reported["resolved_runtime"]["model"] == "gemma-4-e4b-it"


@pytest.mark.asyncio
async def test_parallel_workflow_syncs_task_progress_from_workflow_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 workflow 事件桥接，输入为完成和失败任务，输出为进度 Manager 写入断言。"""
    _FakeSessionTaskProgressManager.instances.clear()
    monkeypatch.setattr(
        workflow_manager_module,
        "SessionTaskProgressManager",
        _FakeSessionTaskProgressManager,
    )
    cfg = _config(tmp_path)
    agent_manager = _ProgressAgentManager()
    manager = AgentWorkflowManager(
        agent_manager=agent_manager,
        config=cfg,
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )

    result = await manager.run_workflow_payload(
        mode="parallel",
        parent_session_id="parent-session",
        parent_agent={"agent_id": agent_manager.root.agent_id},
        payload={
            "task_specs": [
                {
                    "task_id": "alpha",
                    "task_name": "Alpha Review",
                    "prompt": "alpha task",
                },
                {
                    "task_id": "fail",
                    "task_name": "Failure Case",
                    "prompt": "fail task",
                },
            ]
        },
    )

    progress = _FakeSessionTaskProgressManager.instances[0]
    assert progress.calls
    assert all(call["session_id"] == "parent-session" for call in progress.calls)
    assert all(call["workflow_id"] == result.workflow_id for call in progress.calls)
    opened = progress.calls[0]
    assert opened["kind"] == "open"
    assert str(opened["control_mode"]) == "runtime_lifecycle"
    opened_tasks = opened["tasks"]
    assert isinstance(opened_tasks, list)
    assert [(task.task_id, task.task_run_id, task.desc) for task in opened_tasks] == [
        ("alpha", "001-alpha", "Alpha Review"),
        ("fail", "002-fail", "Failure Case"),
    ]
    transitions = {
        (str(call["task_id"]), str(call["runtime_status"]), call["error_message"])
        for call in progress.calls
        if call["kind"] == "transition"
    }
    assert ("alpha", "running", None) in transitions
    assert ("alpha", "completed", None) in transitions
    assert ("fail", "running", None) in transitions
    assert ("fail", "failed", "child exploded") in transitions


@pytest.mark.asyncio
async def test_parallel_workflow_audits_task_progress_sync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证进度写入失败不打断 workflow，输入为失败 Manager，输出为审计事件。"""
    monkeypatch.setattr(
        workflow_manager_module,
        "SessionTaskProgressManager",
        _FailingSessionTaskProgressManager,
    )
    cfg = _config(tmp_path)
    agent_manager = _ProgressAgentManager()
    manager = AgentWorkflowManager(
        agent_manager=agent_manager,
        config=cfg,
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )

    result = await manager.run_workflow_payload(
        mode="parallel",
        parent_session_id="parent-session",
        parent_agent={"agent_id": agent_manager.root.agent_id},
        payload={
            "task_specs": [
                {
                    "task_id": "alpha",
                    "task_name": "Alpha Review",
                    "prompt": "alpha task",
                }
            ]
        },
    )

    assert result.completed is True
    audit_actions = [
        json.loads(line)["action"]
        for line in (result.workflow_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "task_progress_sync_failed" in audit_actions


@pytest.mark.asyncio
async def test_workflow_task_uses_agent_manager_spawn_when_parent_agent_id_exists(
    tmp_path: Path,
) -> None:
    """验证 workflow 子任务走 AgentManager.spawn，输入为父 agent 快照，输出为 completed 报告。"""
    cfg = _config(tmp_path)
    root = SimpleNamespace(
        agent_id="root-agent",
        session_id="parent-session",
        mailbox=asyncio.Queue(),
    )
    fake_agent_manager = _FakeWorkflowSpawnAgentManager(root)
    fake_subagents = _RejectingSubagents()
    manager = AgentWorkflowManager(
        agent_manager=fake_agent_manager,
        config=cfg,
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )
    context = manager._build_workflow_context(
        WorkflowRunRequest(
            mode="parallel",
            parent_session_id="parent-session",
            payload={},
            source="unit",
            parent_agent={"agent_id": "root-agent", "model": "fake-model"},
        )
    )
    task = manager.prepare_subagent_tasks(
        workflow_dir=context.workflow_dir,
        tasks=[
            SubAgentTask(
                task_id="alpha",
                task_name="Alpha",
                prompt="alpha task",
            )
        ],
        parent_agent=context.parent_agent,
    )[0]

    outcome = await manager.run_subagent_task(
        context=context,
        task=task,
        display_order=1,
    )

    assert fake_subagents.run_task_calls == 0
    assert len(fake_agent_manager.requests) == 1
    request = fake_agent_manager.requests[0]
    assert request.parent_agent_id == "root-agent"
    assert request.source_task_id == "alpha"
    assert request.metadata["workflow_id"] == context.workflow_id
    assert request.cwd == task.metadata["working_dir"]
    assert outcome.run.status == "completed"
    assert outcome.run.session_id == "parent-session-child"
    assert outcome.run.content == "spawn path done"
    assert outcome.run.usage == {
        "input_uncached_tokens": 5,
        "output_total_tokens": 2,
    }
    assert outcome.report.status == "completed"
    report = json.loads(Path(outcome.report.report_path).read_text(encoding="utf-8"))
    assert report["content"] == "spawn path done"
    assert report["usage"] == outcome.run.usage
    agent_result = json.loads(
        (context.workflow_dir / "agents" / "001-alpha" / "result.json").read_text(encoding="utf-8")
    )
    assert agent_result["status"] == "completed"
    assert agent_result["content"] == "spawn path done"
    assert root.mailbox.qsize() == 1
    buffered = root.mailbox.get_nowait()
    assert buffered.task_id == "unmatched"


def test_child_mail_parser_preserves_zero_turn_count() -> None:
    """child Result 的 turn_count=0 经 workflow parser 保持为 0。"""
    task = SubAgentTask(
        task_id="zero-turn",
        task_name="Zero turn",
        prompt="stop immediately",
    )
    mail = SimpleNamespace(
        payload=Message.assistant(
            "cancelled before first turn",
            metadata={"turn_count": 0},
        )
    )

    run = workflow_manager_module._subagent_run_from_child_mail(
        task=task,
        session_id="child-session",
        run_id="child-run",
        mail=mail,
    )

    assert run.turn_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_axis", ["manager", "identity"])
async def test_workflow_without_parent_agent_manager_fails_before_child_side_effects(
    tmp_path: Path,
    missing_axis: str,
) -> None:
    """SC_14：manager/identity 两个故障轴都在目录、spawn 和审计前失败。"""
    cfg = _config(tmp_path)
    root = SimpleNamespace(mailbox=asyncio.Queue())
    fake_agent_manager = _FakeWorkflowSpawnAgentManager(root)
    manager = AgentWorkflowManager(
        agent_manager=fake_agent_manager if missing_axis == "identity" else None,
        config=cfg,
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )
    workflow_root = Path(cfg.session.file_store_path) / "parent-session" / "agent-workflows"
    parent_agent = (
        None
        if missing_axis == "identity"
        else {"agent_id": "root-agent", "session_id": "parent-session", "model": "fake"}
    )

    with pytest.raises(
        RuntimeError,
        match="workflow requires a booted parent AgentManager and parent agent identity",
    ):
        await manager.run_workflow_payload(
            mode="parallel",
            parent_session_id="parent-session",
            parent_agent=parent_agent,
            payload={
                "task_specs": [{"task_id": "alpha", "task_name": "Alpha", "prompt": "alpha task"}]
            },
        )

    assert fake_agent_manager.requests == []
    assert root.mailbox.empty()
    assert workflow_root.exists() is False


@pytest.mark.asyncio
async def test_parallel_workflow_demuxes_out_of_order_child_results(tmp_path: Path) -> None:
    """验证并发 workflow child_result demux，输入为乱序结果，输出各任务拿到匹配结果。"""
    cfg = _config(tmp_path)
    root = SimpleNamespace(
        agent_id="root-agent",
        session_id="parent-session",
        mailbox=asyncio.Queue(),
    )
    fake_agent_manager = _OutOfOrderWorkflowSpawnAgentManager(root)
    manager = AgentWorkflowManager(
        agent_manager=fake_agent_manager,
        config=cfg,
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )

    result = await manager.run_workflow_payload(
        mode="parallel",
        parent_session_id="parent-session",
        parent_agent={"agent_id": "root-agent", "model": "fake-model"},
        payload={
            "task_specs": [
                {"task_id": "alpha", "task_name": "Alpha", "prompt": "alpha task"},
                {"task_id": "beta", "task_name": "Beta", "prompt": "beta task"},
            ]
        },
    )

    assert result.completed is True
    contents = {run.task.task_id: run.content for run in result.runs}
    assert contents == {"alpha": "alpha done", "beta": "beta done"}
    assert [request.source_task_id for request in fake_agent_manager.requests] == [
        "alpha",
        "beta",
    ]


@pytest.mark.asyncio
async def test_workflow_spawn_request_carries_scoped_run_overrides(tmp_path: Path) -> None:
    """验证 scoped workflow spawn，输入为授权文件任务，输出 request 带工具快照和 scope。"""
    cfg = _config(tmp_path)
    root = SimpleNamespace(
        agent_id="root-agent",
        session_id="parent-session",
        mailbox=asyncio.Queue(),
    )
    fake_agent_manager = _FakeWorkflowSpawnAgentManager(root)
    runtime_resources = _RuntimeBackedRejectingSubagents(cfg).runtime
    manager = AgentWorkflowManager(
        agent_manager=fake_agent_manager,
        runtime=runtime_resources,
        config=cfg,
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )
    context = manager._build_workflow_context(
        WorkflowRunRequest(
            mode="parallel",
            parent_session_id="parent-session",
            payload={},
            source="unit",
            parent_agent={"agent_id": "root-agent", "model": "fake-model"},
        )
    )
    task = manager.prepare_subagent_tasks(
        workflow_dir=context.workflow_dir,
        tasks=[
            SubAgentTask(
                task_id="alpha",
                task_name="Alpha",
                prompt="read scoped file",
                tool_names=("read_file",),
                permission=SubAgentPermissionSpec(mode="scoped_workdir"),
            )
        ],
        parent_agent=context.parent_agent,
    )[0]

    await manager.run_subagent_task(context=context, task=task, display_order=1)

    request = fake_agent_manager.requests[0]
    assert request.child_session_id is not None
    assert request.enabled_tools is not None
    assert [tool.name for tool in request.enabled_tools] == ["read_file"]
    assert request.scope_allowed_tool_names == ("list_dir", "read_file", "write_file")
    assert request.lifecycle_hooks
    assert request.timeout_seconds == task.runtime.timeout_seconds
    assert request.llm_request_metadata["resolved_runtime"]["model"] == task.runtime.model
    creation_path = context.workflow_dir / "agents" / "001-alpha" / "subagent.json"
    creation = json.loads(creation_path.read_text(encoding="utf-8"))
    assert creation["grant"]["session_id"] == request.child_session_id
    assert creation["grant"]["allowed_tools"] == ["read_file"]


@pytest.mark.asyncio
async def test_workflow_rejects_parent_session_path_traversal(tmp_path: Path) -> None:
    """验证父会话 ID 不能穿越 session 根目录，输入为 traversal ID，输出为拒绝。"""
    llm = _EchoLLM()
    cfg, runtime = _runtime(tmp_path, llm)
    manager, binding = _workflow_manager(
        runtime=runtime,
        config=cfg,
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(ValueError, match="session root"):
            await manager.run_workflow_payload(
                mode="parallel",
                parent_session_id="../escape",
                parent_agent=binding.parent_agent,
                payload={
                    "task_specs": [
                        {
                            "task_id": "alpha",
                            "task_name": "alpha",
                            "prompt": "alpha task",
                        }
                    ]
                },
            )
    finally:
        await binding.aclose()
        await runtime.aclose()

    assert not (tmp_path / "escape").exists()


@pytest.mark.asyncio
async def test_parallel_workflow_reports_failed_child_task(tmp_path: Path) -> None:
    """验证失败子任务收口，输入为缺失工具任务，输出为 failed 报告和审计断言。"""
    llm = _EchoLLM()
    cfg, runtime = _runtime(tmp_path, llm)
    manager, binding = _workflow_manager(
        runtime=runtime,
        config=cfg,
        workspace_root=tmp_path,
    )
    try:
        result = await manager.run_workflow_payload(
            mode="parallel",
            parent_session_id="parent-session",
            parent_agent=binding.parent_agent,
            payload={
                "task_specs": [
                    {
                        "task_id": "bad-tool",
                        "task_name": "bad tool",
                        "prompt": "bad task",
                        "tool_names": ["missing_tool"],
                    }
                ]
            },
        )
    finally:
        await binding.aclose()
        await runtime.aclose()

    assert result.completed is False
    assert result.runs[0].status == "failed"
    assert result.reports[0].status == "failed"
    assert result.report_index_path.is_file()

    audit_actions = [
        json.loads(line)["action"]
        for line in (result.workflow_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert audit_actions.count("agent_failed") == 1
    assert audit_actions.count("subagent_reported") == 1
    assert audit_actions.count("workflow_completed") == 1

    report_path = Path(result.reports[0].report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert "missing_tool" in report["error_message"]
    assert report["content_digest"].startswith("sha256:")

    workflow_result = json.loads((result.workflow_dir / "result.json").read_text(encoding="utf-8"))
    assert workflow_result["completed"] is False
    assert workflow_result["reports"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_parallel_workflow_uses_unique_task_run_paths_for_slug_collisions(
    tmp_path: Path,
) -> None:
    """验证 slug 冲突时生成唯一路径，输入为冲突任务 ID，输出为独立 result/report 路径断言。"""
    llm = _EchoLLM()
    cfg, runtime = _runtime(tmp_path, llm)
    manager, binding = _workflow_manager(
        runtime=runtime,
        config=cfg,
        workspace_root=tmp_path,
    )
    try:
        result = await manager.run_workflow_payload(
            mode="parallel",
            parent_session_id="parent-session",
            parent_agent=binding.parent_agent,
            payload={
                "task_specs": [
                    {
                        "task_id": "a/b",
                        "task_name": "alpha",
                        "prompt": "alpha task",
                    },
                    {
                        "task_id": "a?b",
                        "task_name": "beta",
                        "prompt": "beta task",
                    },
                ]
            },
        )
    finally:
        await binding.aclose()
        await runtime.aclose()

    assert result.completed is True
    assert len({run.session_id for run in result.runs}) == 2
    assert {Path(report.report_path).name for report in result.reports} == {
        "001-a-b.json",
        "002-a-b.json",
    }
    assert (result.workflow_dir / "agents" / "001-a-b" / "result.json").is_file()
    assert (result.workflow_dir / "agents" / "002-a-b" / "result.json").is_file()

    report_index = json.loads(result.report_index_path.read_text(encoding="utf-8"))
    assert [report["task_id"] for report in report_index["reports"]] == ["a/b", "a?b"]
    assert len({report["report_path"] for report in report_index["reports"]}) == 2


@pytest.mark.asyncio
async def test_scoped_parallel_workflow_writes_subagent_creation_and_workdir_file(
    tmp_path: Path,
) -> None:
    """验证 scoped 子 agent 创建记录和工作目录写入，输入为 write_file 任务，输出为授权审计断言。"""
    llm = _ToolCallingLLM(
        tool_name="write_file",
        arguments={"path": "result.txt", "content": "scoped ok"},
    )
    cfg, runtime = _runtime(tmp_path, llm, tools=ToolRegistry(build_file_tools()))
    manager, binding = _workflow_manager(
        runtime=runtime,
        config=cfg,
        workspace_root=tmp_path,
    )
    try:
        result = await manager.run_workflow_specs(
            mode="parallel",
            parent_session_id="parent-session",
            parent_agent=binding.parent_agent,
            task_specs=[
                {
                    "task_name": "write ok",
                    "prompt": "write result.txt",
                    "tool_names": ["write_file"],
                    "skill_names": ["writer"],
                    "permission": {"mode": "scoped_workdir"},
                }
            ],
        )
    finally:
        await binding.aclose()
        await runtime.aclose()

    assert result.completed is True
    task_run_dir = result.workflow_dir / "agents" / "001-agent-1"
    working_dir = task_run_dir / "work"
    assert (working_dir / "result.txt").read_text(encoding="utf-8") == "scoped ok"
    creation = json.loads((task_run_dir / "subagent.json").read_text(encoding="utf-8"))
    assert creation["task_run_id"] == "001-agent-1"
    assert creation["permission"] == {"mode": "scoped_workdir"}
    assert creation["grant"]["allowed_tools"] == ["write_file"]
    assert creation["grant"]["allowed_skills"] == ["writer"]
    assert creation["working_dir"] == str(working_dir)
    assert creation["usage"] == {
        "input_uncached_tokens": 12,
        "output_total_tokens": 5,
    }
    assert creation["completed_status"] == "completed"
    assert creation["completed_turn_count"] == result.runs[0].turn_count
    assert Path(result.reports[0].working_dir or "") == working_dir

    records = _audit_records(result.workflow_dir)
    actions = [record["action"] for record in records]
    assert "subagent_created" in actions
    assert "subagent_grant_bound" in actions
    creation_payload = next(
        record["payload"] for record in records if record["action"] == "subagent_created"
    )
    assert creation_payload["resolved_runtime"]["model"] == "gemma-4-e4b-it"
    assert "runtime_spec" not in creation_payload
    assert "subagent_runtime" not in creation_payload
    assert "subagent_approval_decided" not in actions


@pytest.mark.asyncio
async def test_scoped_parallel_workflow_rejects_outside_write_without_side_effect(
    tmp_path: Path,
) -> None:
    """验证 scoped 权限拒绝越界写入，输入为父目录路径，输出为无副作用和拒绝审计断言。"""
    llm = _ToolCallingLLM(
        tool_name="write_file",
        arguments={"path": "../outside.txt", "content": "bad"},
        final_content="write rejected by scoped permission",
    )
    cfg, runtime = _runtime(tmp_path, llm, tools=ToolRegistry(build_file_tools()))
    manager, binding = _workflow_manager(
        runtime=runtime,
        config=cfg,
        workspace_root=tmp_path,
    )
    try:
        result = await manager.run_workflow_specs(
            mode="parallel",
            parent_session_id="parent-session",
            parent_agent=binding.parent_agent,
            task_specs=[
                {
                    "task_name": "write denied",
                    "prompt": "try outside write",
                    "tool_names": ["write_file"],
                    "permission": {"mode": "scoped_workdir"},
                }
            ],
        )
    finally:
        await binding.aclose()
        await runtime.aclose()

    assert result.completed is True
    task_run_dir = result.workflow_dir / "agents" / "001-agent-1"
    assert not (task_run_dir / "outside.txt").exists()
    assert result.reports[0].summary == "write rejected by scoped permission"
    assert all(
        record["action"] != "subagent_approval_decided"
        for record in _audit_records(result.workflow_dir)
    )


@pytest.mark.asyncio
async def test_scoped_parallel_workflow_audits_hallucinated_not_registered_tool(
    tmp_path: Path,
) -> None:
    """验证幻觉工具调用审计，输入为未注册工具名，输出为 not_registered 拒绝记录断言。"""
    llm = _ToolCallingLLM(
        tool_name="missing_tool",
        arguments={"path": "../outside.txt", "x": 1},
        final_content="missing tool rejected",
    )
    cfg, runtime = _runtime(tmp_path, llm, tools=ToolRegistry(build_file_tools()))
    manager, binding = _workflow_manager(
        runtime=runtime,
        config=cfg,
        workspace_root=tmp_path,
    )
    try:
        result = await manager.run_workflow_specs(
            mode="parallel",
            parent_session_id="parent-session",
            parent_agent=binding.parent_agent,
            task_specs=[
                {
                    "task_name": "hallucinated tool",
                    "prompt": "call a missing tool",
                    "tool_names": ["write_file"],
                    "permission": {"mode": "scoped_workdir"},
                }
            ],
        )
    finally:
        await binding.aclose()
        await runtime.aclose()

    assert result.completed is True
    approval_payload = next(
        record["payload"]
        for record in _audit_records(result.workflow_dir)
        if record["action"] == "subagent_approval_decided"
    )
    assert approval_payload["decision"] == "rejected"
    assert approval_payload["decision_source"] == "not_registered"
    assert approval_payload["tool_name"] == "missing_tool"
    assert approval_payload["raw_args"] == {"path": "../outside.txt", "x": 1}


@pytest.mark.asyncio
async def test_run_workflow_specs_rejects_missing_task_fields_with_index(
    tmp_path: Path,
) -> None:
    """验证缺失任务字段报错带索引，输入为空 task spec，输出为 task_name 错误断言。"""
    llm = _EchoLLM()
    cfg, runtime = _runtime(tmp_path, llm)
    manager, binding = _workflow_manager(
        runtime=runtime,
        config=cfg,
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(ValueError, match=r"task_specs\[1\]\.task_name"):
            await manager.run_workflow_specs(
                mode="parallel",
                parent_session_id="parent-session",
                parent_agent=binding.parent_agent,
                task_specs=[{}],
            )
    finally:
        await binding.aclose()
        await runtime.aclose()


@pytest.mark.asyncio
async def test_run_workflow_specs_rejects_more_than_eight_task_specs(
    tmp_path: Path,
) -> None:
    """验证并行任务数量上限，输入为 9 个 task specs，输出为数量限制错误断言。"""
    llm = _EchoLLM()
    cfg, runtime = _runtime(tmp_path, llm)
    manager, binding = _workflow_manager(
        runtime=runtime,
        config=cfg,
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(ValueError, match="at most 8 task specs"):
            await manager.run_workflow_specs(
                mode="parallel",
                parent_session_id="parent-session",
                parent_agent=binding.parent_agent,
                task_specs=[{"task_name": f"task {index}", "prompt": "work"} for index in range(9)],
            )
    finally:
        await binding.aclose()
        await runtime.aclose()


@pytest.mark.asyncio
async def test_run_workflow_specs_unknown_mode_uses_strategy_registry(
    tmp_path: Path,
) -> None:
    """验证未知 mode 走策略注册表错误，输入为 missing mode，输出为可用策略列表断言。"""
    llm = _EchoLLM()
    cfg, runtime = _runtime(tmp_path, llm)
    manager, binding = _workflow_manager(
        runtime=runtime,
        config=cfg,
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(WorkflowStrategyNotFound) as exc_info:
            await manager.run_workflow_specs(
                mode="missing",
                parent_session_id="parent-session",
                parent_agent=binding.parent_agent,
                task_specs=[{"task_name": "ok", "prompt": "work"}],
            )
    finally:
        await binding.aclose()
        await runtime.aclose()

    assert exc_info.value.available_modes == (
        "deep_research",
        "map_reduce",
        "parallel",
        "roundtable_review",
        "task_flow",
    )
    assert exc_info.value.runnable_modes == (
        "deep_research",
        "map_reduce",
        "parallel",
        "roundtable_review",
        "task_flow",
    )
    assert exc_info.value.operation == "run"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_specs", "error"),
    [
        ([{"task_name": "missing prompt"}], r"task_specs\[1\]\.prompt"),
        ([{"task_name": "bad context", "prompt": "work", "context": 1}], "context"),
        ([None], r"task_specs\[1\] must be an object"),
    ],
)
async def test_run_workflow_specs_rejects_invalid_task_spec_shapes(
    tmp_path: Path,
    task_specs: list[Any],
    error: str,
) -> None:
    """验证非法 task spec 形状，输入为参数化坏数据，输出为对应 ValueError 断言。"""
    llm = _EchoLLM()
    cfg, runtime = _runtime(tmp_path, llm)
    manager, binding = _workflow_manager(
        runtime=runtime,
        config=cfg,
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(ValueError, match=error):
            await manager.run_workflow_specs(
                mode="parallel",
                parent_session_id="parent-session",
                parent_agent=binding.parent_agent,
                task_specs=task_specs,  # type: ignore[arg-type]
            )
    finally:
        await binding.aclose()
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"task_specs": []}, "non-empty task_specs"),
        ({"task_specs": "bad"}, "non-empty task_specs"),
    ],
)
async def test_parallel_strategy_rejects_empty_or_non_list_task_specs(
    tmp_path: Path,
    payload: dict[str, object],
    error: str,
) -> None:
    """验证 parallel 策略 payload 边界，输入为空或非列表 task_specs，输出为校验错误断言。"""
    llm = _EchoLLM()
    cfg, runtime = _runtime(tmp_path, llm)
    manager, binding = _workflow_manager(
        runtime=runtime,
        config=cfg,
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(ValueError, match=error):
            await manager.run_workflow(
                WorkflowRunRequest(
                    mode="parallel",
                    parent_session_id="parent-session",
                    parent_agent=binding.parent_agent,
                    payload=payload,
                    source="unit-test",
                )
            )
    finally:
        await binding.aclose()
        await runtime.aclose()


@pytest.mark.asyncio
async def test_agent_workflow_tool_passes_task_specs_to_bound_manager() -> None:
    """验证 workflow tool 转发任务规格，输入为工具 payload，输出为 manager 入参和工具结果断言。"""

    class _Manager:
        """测试用 manager，记录 run_workflow_specs 收到的参数。"""

        def __init__(self) -> None:
            """初始化测试 manager，输入为空，输出为可记录调用参数的实例。"""
            self.mode = ""
            self.parent_session_id = ""
            self.desc: str | None = None
            self.task_specs: list[dict[str, object]] = []
            self.parent_agent: object | None = None

        async def run_workflow_specs(
            self,
            *,
            mode: str,
            parent_session_id: str,
            task_specs: list[dict[str, object]],
            parent_agent: object | None = None,
            desc: str | None = None,
        ) -> Any:
            """记录 workflow 调用，输入为 mode、父会话和 task_specs，输出为 fake 结果对象。"""
            self.mode = mode
            self.parent_session_id = parent_session_id
            self.desc = desc
            self.task_specs = task_specs
            self.parent_agent = parent_agent

            class _Result:
                """测试用 workflow 结果对象，提供 tool 输出格式化所需字段。"""

                workflow_id = "wf-test"
                mode = "parallel"
                desc = "实现 session task progress 文件模型"
                workflow_dir = Path("/tmp/wf-test")
                report_index_path = Path("/tmp/wf-test/reports/index.json")
                completed = True
                reports = (
                    SubAgentReportProjection(
                        display_order=1,
                        task_id="agent-1",
                        task_name="calc",
                        status="completed",
                        summary="calculation complete",
                        error_message=None,
                        report_path="/tmp/wf-test/reports/agent-1.json",
                        working_dir="/tmp/wf-test/agents/agent-1",
                        session_id="subagent-test",
                        run_id="run-subagent-test-1",
                        reported_at="2026-06-06T00:00:00+00:00",
                    ),
                    SubAgentReportProjection(
                        display_order=2,
                        task_id="agent-2",
                        task_name="empty",
                        status="completed",
                        summary="",
                        error_message=None,
                        report_path="/tmp/wf-test/reports/agent-2.json",
                        working_dir="/tmp/wf-test/agents/agent-2",
                        session_id="subagent-empty",
                        run_id="run-subagent-empty-1",
                        reported_at="2026-06-06T00:00:01+00:00",
                    ),
                )
                runs: list[Any] = []

            return _Result()

    handle = AgentWorkflowHandle()
    manager = _Manager()
    handle.bind(manager)
    tool = build_agent_workflow_tool(handle)
    content, data = await tool._run(
        {
            "mode": "parallel",
            "desc": "实现 session task progress 文件模型",
            "tasks": [
                {
                    "task_name": "calc",
                    "prompt": "calculate",
                    "context": "only this",
                    "tool_names": ["write_file"],
                    "skill_names": ["review-docs"],
                    "permission": {"mode": "scoped_workdir"},
                }
            ],
        },
        ToolContext(run_id="r", session_id="parent-session", turn=1, call_id="c"),
    )

    assert manager.mode == "parallel"
    assert manager.parent_session_id == "parent-session"
    assert manager.desc == "实现 session task progress 文件模型"
    assert manager.parent_agent is None
    assert manager.task_specs == [
        {
            "task_name": "calc",
            "prompt": "calculate",
            "context": "only this",
            "tool_names": ["write_file"],
            "skill_names": ["review-docs"],
            "permission": {"mode": "scoped_workdir"},
        }
    ]
    assert "wf-test" in content
    assert "desc: 实现 session task progress 文件模型" in content
    assert "calculation complete" in content
    assert content.count("summary:") == 2
    assert "error_message: None" in content
    assert "working_dir: /tmp/wf-test/agents/agent-1" in content
    assert "synthesize the final answer" in content
    assert data is not None
    assert data["workflow_id"] == "wf-test"
    assert data["desc"] == "实现 session task progress 文件模型"
    assert data["report_index_path"] == str(Path("/tmp/wf-test/reports/index.json"))
    assert data["reports"][0]["summary"] == "calculation complete"
    assert data["reports"][0]["error_message"] is None


@pytest.mark.asyncio
async def test_agent_workflow_tool_requires_permission() -> None:
    """验证 workflow tool 要求 permission 对象，输入为缺失 permission 的任务，输出为校验错误断言。"""
    handle = AgentWorkflowHandle()
    handle.bind(object())
    tool = build_agent_workflow_tool(handle)

    with pytest.raises(ValueError, match="permission must be an object"):
        await tool.prepare(
            {
                "mode": "parallel",
                "tasks": [
                    {
                        "task_name": "calc",
                        "prompt": "calculate",
                    }
                ],
            },
            ToolContext(run_id="r", session_id="parent-session", turn=1, call_id="c"),
        )


@pytest.mark.asyncio
async def test_parent_agent_can_create_subagents_through_registered_tool(tmp_path: Path) -> None:
    """验证父 agent 通过注册工具创建子 agent，输入为父会话请求，输出为工具消息和 workflow 产物断言。"""
    llm = _WorkflowLLM()
    cfg = _config(tmp_path)
    bootstrap = SessionBootstrap(
        agent_name="test-agent",
        model_name="gemma-4-e4b-it",
        instruction_sources=[],
        instruction_text_hash="test",
        created_at=1.0,
        cwd=str(tmp_path),
    )

    def session_factory(sid: str):  # type: ignore[no-untyped-def]
        """构造父 agent 测试 session，输入为 session ID，输出为 file backend session。"""
        return build_session(cfg, sid, bootstrap=bootstrap)

    handle = AgentWorkflowHandle()
    registry = ToolRegistry([build_agent_workflow_tool(handle)])
    catalog_manager = ModelCatalogManager()
    model_config = catalog_manager.resolve_runtime(cfg.model)
    runtime = SessionEngine(
        config=cfg,
        runner=Runner(),
        llm=llm,
        tools=registry,
        enabled_tool_names=["run_parallel_subagents"],
        approval=AutoAllowApproval(),
        session_factory=session_factory,
        event_sinks=[],
        model_catalog_manager=catalog_manager,
        model_config=model_config,
        agent_spec=AgentSpec(
            name="parent",
            instructions="parent instructions",
            default_model="gemma-4-e4b-it",
            tool_names=("run_parallel_subagents",),
            max_turns=5,
        ),
    )
    manager, binding = _workflow_manager(
        runtime=runtime,
        config=cfg,
        workspace_root=tmp_path,
    )
    handle.bind(manager)
    try:
        result = await runtime.run(
            "delegate work",
            session_id="parent-session",
            thread_id="parent-session",
            agent_id=str(binding.parent_agent["agent_id"]),
        )
    finally:
        await binding.aclose()
        await runtime.aclose()

    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "parent summary"
    tool_messages = [
        message
        for request in llm.calls
        for message in request.messages
        if message.role == "tool" and message.name == "run_parallel_subagents"
    ]
    assert len(tool_messages) == 1
    tool_content = tool_messages[0].content or ""
    assert "report_index:" in tool_content
    assert "summary: alpha child result" in tool_content
    assert "summary: beta child result" in tool_content
    assert "synthesize the final answer" in tool_content
    workflow_roots = list(
        (Path(cfg.session.file_store_path) / "parent-session").glob("agent-workflows/wf-*")
    )
    assert len(workflow_roots) == 1
    workflow_result = json.loads((workflow_roots[0] / "result.json").read_text(encoding="utf-8"))
    assert workflow_result["completed"] is True
    assert {report["summary"] for report in workflow_result["reports"]} == {
        "alpha child result",
        "beta child result",
    }
    assert {run["content"] for run in workflow_result["runs"]} == {
        "alpha child result",
        "beta child result",
    }


def test_apply_model_preset_overrides_cli_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI preset 覆盖只更新运行选择字段。"""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-test")
    cfg = Config(
        model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"),
    )

    updated = _apply_model_preset_or_exit(cfg, "minimax-m3")

    assert updated.model.preset_id == "minimax-m3"
    assert updated.model.reasoning_effort is None
    assert set(updated.model.model_dump()) == {"preset_id", "reasoning_effort"}


def test_apply_model_preset_reads_provider_catalog_when_web_presets_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI preset 直接读取 provider catalog。"""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-test")
    cfg = Config(
        model=ModelSelectionConfig(
            preset_id="local-gemma-4-e4b-it",
            reasoning_effort="high",
        ),
    )

    updated = _apply_model_preset_or_exit(cfg, "minimax-m3")

    assert updated.model.preset_id == "minimax-m3"
    assert updated.model.reasoning_effort == "high"


def test_apply_model_preset_reads_catalog_model_list_when_web_presets_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI preset 可选择 catalog provider 下的非默认模型。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    cfg = Config(
        model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"),
    )

    updated = _apply_model_preset_or_exit(cfg, "deepseek-pro")

    assert updated.model.preset_id == "deepseek-pro"
    assert updated.model.reasoning_effort is None
