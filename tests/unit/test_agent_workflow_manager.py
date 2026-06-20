"""智能体工作流管理器单元测试。

本脚本验证并行子 agent 编排、scoped_workdir 权限审计、workflow tool 入参传递、策略注册分发和模型 preset 覆盖行为。
作用是确保 AgentWorkflowManager 写入完整审计产物，保持子 agent 上下文隔离，并通过测试文件复现边界输入。
关键执行流程：构造 fake LLM 和 NativeRuntime，运行 workflow manager 或 agent_workflow_tool，读取 workflow 产物和审计日志进行断言。
关键函数：_runtime 构造测试 runtime，_audit_records 读取审计日志，各 test_* 函数覆盖并行执行、失败收口、权限隔离、策略分发和 CLI preset。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import application.agent_workflows.manager as workflow_manager_module
from application.agent_roles import AgentRoleManager
from application.agent_workflows.manager import (
    AgentWorkflowManager,
    SubAgentReportProjection,
)
from application.agent_workflows.strategies.base import WorkflowRunRequest, WorkflowStrategyNotFound
from application.subagents.manager import SubAgentManager, SubAgentTask
from core.agent_spec import AgentSpec
from core.contracts import LLMRequest, LLMResponse, ToolContext
from core.message import Message, ToolCall
from core.runner import Runner
from hosts.cli.main import _apply_model_preset_or_exit
from infrastructure.config.models import Config, LLMPresetConfig, ModelConfig, WebConfig
from runtime_assembly.native_runtime import NativeRuntime
from sessions import SessionBootstrap, build_session
from tools import AutoAllowApproval, ToolRegistry
from tools.agent_workflow_tool import AgentWorkflowHandle, build_agent_workflow_tool
from tools.builtin.file_tool import build_file_tools


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
            usage=usage_by_content.get(content, {}),
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
                                ]
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
                usage={
                    "input_tokens": 7,
                    "output_tokens": 3,
                    "cache_read_input_tokens": 2,
                    "provider_extra_tokens": 11,
                },
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
            usage={
                "input_tokens": 5,
                "output_tokens": 2,
                "cache_creation_input_tokens": 1,
                "provider_extra_tokens": 13,
            },
        )


class _ProgressSubagents:
    """测试用子 agent 管理器，按 task_id 生成完成或失败结果。"""

    async def run_task(
        self,
        *,
        workflow_id: str,
        parent_session_id: str,
        task: SubAgentTask,
        audit_writer: Any | None = None,
    ) -> Any:
        """运行测试任务，输入为子任务，输出为成功 run 或抛出测试异常。"""
        if task.task_id == "fail":
            raise RuntimeError("child exploded")
        return workflow_manager_module.SubAgentRun(
            task=task,
            session_id=f"child-{task.task_id}",
            run_id=f"run-{task.task_id}",
            status="completed",
            content=f"{task.task_id} done",
            error_message=None,
            turn_count=1,
            usage={},
        )


class _FakeWorkflowTaskProgressInput:
    """测试用 workflow 进度输入，记录构造 payload。"""

    def __init__(self, **payload: object) -> None:
        """保存输入 payload，输入为字段字典，输出为可断言对象。"""
        self.payload = payload

    @classmethod
    def model_validate(
        cls,
        payload: dict[str, object],
    ) -> _FakeWorkflowTaskProgressInput:
        """模拟 Pydantic 合同入口，输入为字段字典，输出为 fake 输入对象。"""
        return cls(**payload)


class _FakeSessionTaskProgressManager:
    """测试用进度 Manager，记录 sync_workflow_tasks 调用。"""

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

    def sync_workflow_tasks(
        self,
        *,
        session_id: str,
        workflow_id: str,
        tasks: list[_FakeWorkflowTaskProgressInput],
    ) -> None:
        """记录 workflow 同步调用，输入为任务对象，输出为 calls 追加记录。"""
        status_map = {
            "assigned": "pending",
            "running": "in_progress",
            "completed": "completed",
            "failed": "pending",
        }
        normalized_tasks = []
        for task in tasks:
            payload = dict(task.payload)
            task_run_id = str(payload["task_run_id"])
            source_status = str(payload["status"])
            orchestration_task_id = f"{workflow_id}:{task_run_id}"
            normalized_tasks.append(
                {
                    "id": orchestration_task_id,
                    "orchestration_task_id": orchestration_task_id,
                    "workflow_id": workflow_id,
                    "task_id": payload["task_id"],
                    "task_run_id": task_run_id,
                    "desc": payload["desc"],
                    "status": status_map[source_status],
                    "source_status": source_status,
                    "error_message": payload["error_message"],
                    "display_order": payload["display_order"],
                }
            )
        self.calls.append(
            {
                "session_id": session_id,
                "workflow_id": workflow_id,
                "tasks": normalized_tasks,
            }
        )


class _FailingSessionTaskProgressManager:
    """测试用失败进度 Manager，模拟磁盘或权限错误。"""

    @classmethod
    def from_config(cls, config: Config) -> _FailingSessionTaskProgressManager:
        """按生产合同构造失败 fake manager，输入为 Config，输出为实例。"""
        del config
        return cls()

    def sync_workflow_tasks(
        self,
        *,
        session_id: str,
        workflow_id: str,
        tasks: list[Any],
    ) -> None:
        """模拟进度同步失败，输入为同步参数，输出为 RuntimeError。"""
        del session_id, workflow_id, tasks
        raise RuntimeError("progress disk unavailable")


def _config(tmp_path: Path) -> Config:
    """构造测试配置，输入为临时目录，输出为 file session 和 auto_allow 审批配置。"""
    cfg = Config(
        model=ModelConfig(
            name="fake-model",
            base_url="http://127.0.0.1:1234/v1",
            api_key="",
        )
    )
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
        subagents=_ProgressSubagents(),  # type: ignore[arg-type]
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
) -> tuple[Config, NativeRuntime]:
    """构造测试 runtime，输入为临时目录、fake LLM 和可选工具表，输出为配置和 NativeRuntime。"""
    cfg = _config(tmp_path)
    bootstrap = SessionBootstrap(
        agent_name="test-agent",
        model_name=cfg.model.name,
        instruction_sources=[],
        instruction_text_hash="test",
        created_at=1.0,
        cwd=str(tmp_path),
    )

    def session_factory(sid: str):  # type: ignore[no-untyped-def]
        """构造测试 session，输入为 session ID，输出为 file backend session。"""
        return build_session(cfg, sid, bootstrap=bootstrap)

    runtime = NativeRuntime(
        config=cfg,
        runner=Runner(),
        llm=llm,
        tools=tools or ToolRegistry(),
        enabled_tool_names=[],
        approval=AutoAllowApproval(),
        session_factory=session_factory,
        event_sinks=[],
        agent_spec=AgentSpec(
            name="parent",
            instructions="parent instructions",
            default_model=cfg.model.name,
        ),
    )
    return cfg, runtime


def _audit_records(workflow_dir: Path) -> list[dict[str, Any]]:
    """读取 workflow 审计日志，输入为 workflow 目录，输出为 audit.jsonl 记录列表。"""
    return [
        json.loads(line)
        for line in (workflow_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]


@pytest.mark.asyncio
async def test_parallel_workflow_writes_audit_and_keeps_child_contexts_isolated(
    tmp_path: Path,
) -> None:
    """验证并行 workflow 审计和上下文隔离，输入为临时目录，输出为产物与审计断言。"""
    llm = _EchoLLM()
    cfg, runtime = _runtime(tmp_path, llm)
    try:
        manager = AgentWorkflowManager(
            subagents=SubAgentManager(runtime),
            config=cfg,
            workspace_root=tmp_path,
            role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
        )
        result = await manager.run_parallel(
            parent_session_id="parent-session",
            desc="  梳理现有 thread/session 路径与顶部工具栏结构  ",
            tasks=[
                SubAgentTask(task_id="alpha", task_name="alpha", prompt="alpha task"),
                SubAgentTask(task_id="beta", task_name="beta", prompt="beta task"),
            ],
        )
    finally:
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
            "input_tokens": 11,
            "output_tokens": 3,
            "cache_read_input_tokens": 2,
            "provider_extra_tokens": 7,
        },
        {
            "input_tokens": 13,
            "output_tokens": 4,
            "cache_creation_input_tokens": 5,
            "provider_extra_tokens": 9,
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
    monkeypatch.setattr(
        workflow_manager_module,
        "WorkflowTaskProgressInput",
        _FakeWorkflowTaskProgressInput,
    )
    cfg = _config(tmp_path)
    manager = AgentWorkflowManager(
        subagents=_ProgressSubagents(),  # type: ignore[arg-type]
        config=cfg,
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )

    result = await manager.run_parallel(
        parent_session_id="parent-session",
        tasks=[
            SubAgentTask(task_id="alpha", task_name="Alpha Review", prompt="alpha task"),
            SubAgentTask(task_id="fail", task_name="Failure Case", prompt="fail task"),
        ],
    )

    progress = _FakeSessionTaskProgressManager.instances[0]
    assert progress.calls
    assert all(call["session_id"] == "parent-session" for call in progress.calls)
    assert all(call["workflow_id"] == result.workflow_id for call in progress.calls)

    first_tasks = progress.calls[0]["tasks"]
    assert isinstance(first_tasks, list)
    assert [task["source_status"] for task in first_tasks] == ["assigned", "assigned"]
    assert [task["status"] for task in first_tasks] == ["pending", "pending"]

    assert any(
        any(
            task["task_id"] == "alpha"
            and task["task_run_id"] == "001-alpha"
            and task["status"] == "in_progress"
            and task["source_status"] == "running"
            for task in call["tasks"]
        )
        for call in progress.calls
    )

    final_tasks = progress.calls[-1]["tasks"]
    assert isinstance(final_tasks, list)
    final_by_task_id = {str(task["task_id"]): task for task in final_tasks}
    alpha = final_by_task_id["alpha"]
    failure = final_by_task_id["fail"]
    assert alpha["task_run_id"] == "001-alpha"
    assert alpha["desc"] == "Alpha Review"
    assert alpha["status"] == "completed"
    assert alpha["source_status"] == "completed"
    assert alpha["orchestration_task_id"] == f"{result.workflow_id}:001-alpha"
    assert failure["task_run_id"] == "002-fail"
    assert failure["desc"] == "Failure Case"
    assert failure["status"] == "pending"
    assert failure["source_status"] == "failed"
    assert failure["error_message"] == "child exploded"
    assert failure["orchestration_task_id"] == f"{result.workflow_id}:002-fail"


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
    manager = AgentWorkflowManager(
        subagents=_ProgressSubagents(),  # type: ignore[arg-type]
        config=cfg,
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )

    result = await manager.run_parallel(
        parent_session_id="parent-session",
        tasks=[SubAgentTask(task_id="alpha", task_name="Alpha Review", prompt="alpha task")],
    )

    assert result.completed is True
    audit_actions = [
        json.loads(line)["action"]
        for line in (result.workflow_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "task_progress_sync_failed" in audit_actions


@pytest.mark.asyncio
async def test_workflow_rejects_parent_session_path_traversal(tmp_path: Path) -> None:
    """验证父会话 ID 不能穿越 session 根目录，输入为 traversal ID，输出为拒绝。"""
    llm = _EchoLLM()
    cfg, runtime = _runtime(tmp_path, llm)
    try:
        manager = AgentWorkflowManager(
            subagents=SubAgentManager(runtime),
            config=cfg,
            workspace_root=tmp_path,
            role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
        )
        with pytest.raises(ValueError, match="session root"):
            await manager.run_parallel(
                parent_session_id="../escape",
                tasks=[SubAgentTask(task_id="alpha", task_name="alpha", prompt="alpha task")],
            )
    finally:
        await runtime.aclose()

    assert not (tmp_path / "escape").exists()


@pytest.mark.asyncio
async def test_parallel_workflow_reports_failed_child_task(tmp_path: Path) -> None:
    """验证失败子任务收口，输入为缺失工具任务，输出为 failed 报告和审计断言。"""
    llm = _EchoLLM()
    cfg, runtime = _runtime(tmp_path, llm)
    try:
        manager = AgentWorkflowManager(
            subagents=SubAgentManager(runtime),
            config=cfg,
            workspace_root=tmp_path,
            role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
        )
        result = await manager.run_parallel(
            parent_session_id="parent-session",
            tasks=[
                SubAgentTask(
                    task_id="bad-tool",
                    task_name="bad tool",
                    prompt="bad task",
                    tool_names=("missing_tool",),
                )
            ],
        )
    finally:
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
    try:
        manager = AgentWorkflowManager(
            subagents=SubAgentManager(runtime),
            config=cfg,
            workspace_root=tmp_path,
            role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
        )
        result = await manager.run_parallel(
            parent_session_id="parent-session",
            tasks=[
                SubAgentTask(task_id="a/b", task_name="alpha", prompt="alpha task"),
                SubAgentTask(task_id="a?b", task_name="beta", prompt="beta task"),
            ],
        )
    finally:
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
    try:
        manager = AgentWorkflowManager(
            subagents=SubAgentManager(runtime),
            config=cfg,
            workspace_root=tmp_path,
            role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
        )
        result = await manager.run_workflow_specs(
            mode="parallel",
            parent_session_id="parent-session",
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
        "input_tokens": 12,
        "output_tokens": 5,
        "cache_creation_input_tokens": 1,
        "provider_extra_tokens": 24,
        "cache_read_input_tokens": 2,
    }
    assert creation["completed_status"] == "completed"
    assert creation["completed_turn_count"] == result.runs[0].turn_count
    assert Path(result.reports[0].working_dir or "") == working_dir

    records = _audit_records(result.workflow_dir)
    actions = [record["action"] for record in records]
    assert "subagent_created" in actions
    assert "subagent_grant_bound" in actions
    approval_payload = next(
        record["payload"] for record in records if record["action"] == "subagent_approval_decided"
    )
    assert approval_payload["decision"] == "approved"
    assert approval_payload["decision_source"] == "grant_allow"
    assert approval_payload["resolved_path"] == str(working_dir / "result.txt")


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
    try:
        manager = AgentWorkflowManager(
            subagents=SubAgentManager(runtime),
            config=cfg,
            workspace_root=tmp_path,
            role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
        )
        result = await manager.run_workflow_specs(
            mode="parallel",
            parent_session_id="parent-session",
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
        await runtime.aclose()

    assert result.completed is True
    task_run_dir = result.workflow_dir / "agents" / "001-agent-1"
    assert not (task_run_dir / "outside.txt").exists()
    assert result.reports[0].summary == "write rejected by scoped permission"
    approval_payload = next(
        record["payload"]
        for record in _audit_records(result.workflow_dir)
        if record["action"] == "subagent_approval_decided"
    )
    assert approval_payload["decision"] == "rejected"
    assert approval_payload["decision_source"] == "scope_deny"
    assert approval_payload["target_path"] == "../outside.txt"


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
    try:
        manager = AgentWorkflowManager(
            subagents=SubAgentManager(runtime),
            config=cfg,
            workspace_root=tmp_path,
            role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
        )
        result = await manager.run_workflow_specs(
            mode="parallel",
            parent_session_id="parent-session",
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
    try:
        manager = AgentWorkflowManager(
            subagents=SubAgentManager(runtime),
            config=cfg,
            workspace_root=tmp_path,
            role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
        )
        with pytest.raises(ValueError, match=r"task_specs\[1\]\.task_name"):
            await manager.run_workflow_specs(
                mode="parallel",
                parent_session_id="parent-session",
                task_specs=[{}],
            )
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_run_workflow_specs_rejects_more_than_eight_task_specs(
    tmp_path: Path,
) -> None:
    """验证并行任务数量上限，输入为 9 个 task specs，输出为数量限制错误断言。"""
    llm = _EchoLLM()
    cfg, runtime = _runtime(tmp_path, llm)
    try:
        manager = AgentWorkflowManager(
            subagents=SubAgentManager(runtime),
            config=cfg,
            workspace_root=tmp_path,
            role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
        )
        with pytest.raises(ValueError, match="at most 8 task specs"):
            await manager.run_workflow_specs(
                mode="parallel",
                parent_session_id="parent-session",
                task_specs=[{"task_name": f"task {index}", "prompt": "work"} for index in range(9)],
            )
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_run_workflow_specs_unknown_mode_uses_strategy_registry(
    tmp_path: Path,
) -> None:
    """验证未知 mode 走策略注册表错误，输入为 missing mode，输出为可用策略列表断言。"""
    llm = _EchoLLM()
    cfg, runtime = _runtime(tmp_path, llm)
    try:
        manager = AgentWorkflowManager(
            subagents=SubAgentManager(runtime),
            config=cfg,
            workspace_root=tmp_path,
            role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
        )
        with pytest.raises(WorkflowStrategyNotFound) as exc_info:
            await manager.run_workflow_specs(
                mode="missing",
                parent_session_id="parent-session",
                task_specs=[{"task_name": "ok", "prompt": "work"}],
            )
    finally:
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
    try:
        manager = AgentWorkflowManager(
            subagents=SubAgentManager(runtime),
            config=cfg,
            workspace_root=tmp_path,
            role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
        )
        with pytest.raises(ValueError, match=error):
            await manager.run_workflow_specs(
                mode="parallel",
                parent_session_id="parent-session",
                task_specs=task_specs,  # type: ignore[arg-type]
            )
    finally:
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
    try:
        manager = AgentWorkflowManager(
            subagents=SubAgentManager(runtime),
            config=cfg,
            workspace_root=tmp_path,
            role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
        )
        with pytest.raises(ValueError, match=error):
            await manager.run_workflow(
                WorkflowRunRequest(
                    mode="parallel",
                    parent_session_id="parent-session",
                    payload=payload,
                    source="unit-test",
                )
            )
    finally:
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

        async def run_workflow_specs(
            self,
            *,
            mode: str,
            parent_session_id: str,
            task_specs: list[dict[str, object]],
            desc: str | None = None,
        ) -> Any:
            """记录 workflow 调用，输入为 mode、父会话和 task_specs，输出为 fake 结果对象。"""
            self.mode = mode
            self.parent_session_id = parent_session_id
            self.desc = desc
            self.task_specs = task_specs

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
        await tool._run(
            {
                "tasks": [
                    {
                        "task_name": "calc",
                        "prompt": "calculate",
                    }
                ]
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
        model_name=cfg.model.name,
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
    runtime = NativeRuntime(
        config=cfg,
        runner=Runner(),
        llm=llm,
        tools=registry,
        enabled_tool_names=["run_parallel_subagents"],
        approval=AutoAllowApproval(),
        session_factory=session_factory,
        event_sinks=[],
        agent_spec=AgentSpec(
            name="parent",
            instructions="parent instructions",
            default_model=cfg.model.name,
            tool_names=("run_parallel_subagents",),
            max_turns=5,
        ),
    )
    manager = AgentWorkflowManager(
        subagents=SubAgentManager(runtime),
        config=cfg,
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )
    handle.bind(manager)
    try:
        result = await runtime.run("delegate work", session_id="parent-session")
    finally:
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
    """验证模型 preset 覆盖 CLI 模型，输入为环境变量和 preset ID，输出为更新后的模型配置断言。"""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-test")
    cfg = Config(
        model=ModelConfig(
            name="local",
            base_url="http://127.0.0.1:1234/v1",
            api_key="",
        ),
        web=WebConfig(
            llm_presets=[
                LLMPresetConfig(
                    id="minimax-m3",
                    display_name="MiniMax M3",
                    base_url="https://api.minimaxi.com/anthropic",
                    model="MiniMax-M3",
                    api_key_env="MINIMAX_API_KEY",
                    reasoning_effort="high",
                )
            ]
        ),
    )

    updated = _apply_model_preset_or_exit(cfg, "minimax-m3")

    assert updated.model.name == "MiniMax-M3"
    assert updated.model.base_url == "https://api.minimaxi.com/anthropic"
    assert updated.model.api_key == "sk-test"
    assert updated.model.reasoning_effort == "high"
    assert updated.model.effective_provider == "anthropic"
