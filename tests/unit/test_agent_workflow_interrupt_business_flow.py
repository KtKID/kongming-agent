"""agent workflow 用户中断业务链路红测。

本测试覆盖父 agent 通过 run_agent_workflow 工具启动并行子 agent 后被用户中断的主链路。
作用是固定中断时 workflow 根产物、审计日志和父 Runner 结果之间的一致性合同。
关键执行流程：SessionEngine.run 驱动父 Runner 调用 run_agent_workflow，AgentWorkflowManager
通过真实 AgentManager 启动 child Runner，child LLM 挂起后由测试取消父 run，
再读取父会话、TaskRegistry 投影和 workflow 产物断言。
关键函数：test_user_interrupt_marks_parallel_workflow_cancelled 验证父 run、子任务和 workflow
产物的取消状态同步；test_manager_cancel_finalizer_covers_strategy_entrypoint 验证 manager
统一取消收尾；test_user_interrupt_cancels_child_tool_execution 验证子 agent 工具执行中的取消占位。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from application.agent_roles import AgentRoleManager
from application.agent_workflows.context import WorkflowExecutionContext
from application.agent_workflows.manager import AgentWorkflowManager
from application.agent_workflows.models import AgentWorkflowResult
from application.agent_workflows.strategies.base import WorkflowRunRequest
from application.agent_workflows.strategies.description import WorkflowStrategyDescription
from application.agent_workflows.task_models import SubAgentTask
from core.agent_spec import AgentSpec
from core.contracts import LLMRequest, LLMResponse, PreparedToolCall, Tool, ToolContext, ToolResult
from core.message import Message, ToolCall
from core.runner import Runner
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.models import Config, ModelSelectionConfig
from runtime_assembly.session_engine import SessionEngine
from sessions import SessionBootstrap, build_session
from tests.support.workflow_agent_tree import bind_workflow_agent_tree
from tools import AutoAllowApproval, ToolRegistry
from tools.agent_workflow_tool import AgentWorkflowHandle, build_run_agent_workflow_tool


class _WorkflowInterruptLLM:
    """测试用 LLM，父 agent 发起 workflow，子 agent 调用保持挂起。"""

    def __init__(self) -> None:
        """初始化请求记录，输入为空，输出为可驱动 workflow 的 fake provider。"""
        self.calls: list[LLMRequest] = []
        self.child_started = asyncio.Event()
        self.child_cancelled = asyncio.Event()
        self.child_started_count = 0
        self.child_cancelled_count = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """完成请求，输入为父或子 LLMRequest，输出为父工具调用或挂起子调用。"""
        self.calls.append(request)
        tool_names = {tool.name for tool in request.tools}
        has_workflow_result = any(
            message.role == "tool" and message.name == "run_agent_workflow"
            for message in request.messages
        )
        if "run_agent_workflow" in tool_names and has_workflow_result:
            return LLMResponse(
                message=Message.assistant("workflow result observed"),
                finish_reason="stop",
            )
        if "run_agent_workflow" in tool_names:
            return LLMResponse(
                message=Message.assistant(
                    tool_calls=[
                        ToolCall(
                            call_id="call-workflow",
                            tool_name="run_agent_workflow",
                            arguments={
                                "mode": "parallel",
                                "payload": {
                                    "desc": "用户中断并行子 agent workflow",
                                    "task_specs": [
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
                            },
                        )
                    ]
                ),
                finish_reason="tool_calls",
            )
        self.child_started_count += 1
        self.child_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.child_cancelled_count += 1
            self.child_cancelled.set()
            raise


class _ChildToolInterruptLLM:
    """测试用 LLM，父 agent 启动 workflow，子 agent 调用 read_file 工具。"""

    def __init__(self) -> None:
        """初始化请求记录，输入为空，输出为可驱动子工具调用的 fake provider。"""
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """完成请求，输入为父或子 LLMRequest，输出为父 workflow 调用或子工具调用。"""
        self.calls.append(request)
        tool_names = {tool.name for tool in request.tools}
        completed_tool_names = {
            message.name for message in request.messages if message.role == "tool"
        }
        if "run_agent_workflow" in tool_names and "run_agent_workflow" not in completed_tool_names:
            return LLMResponse(
                message=Message.assistant(
                    tool_calls=[
                        ToolCall(
                            call_id="call-workflow",
                            tool_name="run_agent_workflow",
                            arguments={
                                "mode": "parallel",
                                "payload": {
                                    "desc": "用户中断子 agent 文件工具 workflow",
                                    "task_specs": [
                                        {
                                            "task_name": "child-read",
                                            "prompt": "读取 probe.txt 并报告内容",
                                            "tool_names": ["read_file"],
                                            "permission": {"mode": "scoped_workdir"},
                                        }
                                    ],
                                },
                            },
                        )
                    ]
                ),
                finish_reason="tool_calls",
            )
        if "read_file" in tool_names and "read_file" not in completed_tool_names:
            return LLMResponse(
                message=Message.assistant(
                    tool_calls=[
                        ToolCall(
                            call_id="call-child-read",
                            tool_name="read_file",
                            arguments={"path": "probe.txt"},
                        )
                    ]
                ),
                finish_reason="tool_calls",
            )
        return LLMResponse(message=Message.assistant("observed"), finish_reason="stop")


class _HangingReadFileTool:
    """测试用 read_file 工具，进入 execute 后挂起直到父 run 取消。"""

    name = "read_file"
    description = "Read a UTF-8 text file from disk and return its content."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def __init__(self) -> None:
        """初始化工具执行记录，输入为空，输出为可等待 started/cancelled 的工具。"""
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.started_count = 0
        self.cancelled_count = 0
        self.paths: list[str] = []
        self.call_ids: list[str] = []

    async def execute(self, prepared: PreparedToolCall, ctx: ToolContext) -> ToolResult:
        args = prepared.arguments
        """执行挂起读取，输入为 scoped path 和 ToolContext，输出为取消时透传 CancelledError。"""
        self.started_count += 1
        raw_path = args.get("path")
        if isinstance(raw_path, str):
            self.paths.append(raw_path)
        self.call_ids.append(ctx.call_id)
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled_count += 1
            self.cancelled.set()
            raise


class _ManagerCancelStrategy:
    """测试用 workflow strategy，写入任务骨架后保持运行等待外部取消。"""

    mode = "manager_cancel_probe"

    def __init__(self, manager: AgentWorkflowManager) -> None:
        """初始化策略，输入为真实 manager，输出为可等待 started/cancelled 的策略实例。"""
        self._manager = manager
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    def describe(self) -> WorkflowStrategyDescription:
        """生成测试策略说明，输入为当前策略，输出为可注册的策略描述。"""
        return WorkflowStrategyDescription(
            mode=self.mode,
            title="manager 取消探针",
            status="available",
            runnable=True,
            summary="验证 manager 统一取消收尾。",
            when_to_use=("验证 workflow manager 入口取消时使用",),
            warnings=("仅用于单测",),
            inputs=(),
            outputs=("取消产物",),
            examples=({},),
        )

    def catalog_entry(self):  # type: ignore[no-untyped-def]
        """生成策略目录项，输入为策略描述，输出为紧凑目录条目。"""
        return self.describe().catalog_entry()

    async def run(
        self,
        context: WorkflowExecutionContext,
        payload: Mapping[str, object],
    ) -> AgentWorkflowResult:
        """执行挂起策略，输入为 manager context 和 payload，输出为取消前保持运行。"""
        task = SubAgentTask(
            task_id="probe",
            task_name="manager cancel probe",
            prompt="等待取消",
            context="",
        )
        prepared = self._manager.prepare_subagent_tasks(
            workflow_dir=context.workflow_dir,
            tasks=[task],
            parent_agent=context.parent_agent,
        )
        self._manager.write_workflow_manifest(
            context=context,
            tasks=prepared,
            status="running",
        )
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("manager cancel probe should be cancelled")


def _config(tmp_path: Path) -> Config:
    """构造测试配置，输入为临时目录，输出为 file session 和本地 fake model 配置。"""
    cfg = Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"))
    cfg.session.backend = "file"
    cfg.session.file_store_path = str(tmp_path / "sessions")
    cfg.approval.mode = "auto_allow"
    return cfg


def _runtime(
    tmp_path: Path,
    cfg: Config,
    llm: Any,
    handle: AgentWorkflowHandle,
    *,
    extra_tools: Sequence[Tool] = (),
) -> tuple[SessionEngine, dict[str, Any]]:
    """构造父 agent runtime，输入为配置、fake LLM 和 workflow handle，输出 runtime 和 session 表。"""
    bootstrap = SessionBootstrap(
        agent_name="parent",
        model_name="gemma-4-e4b-it",
        instruction_sources=[],
        instruction_text_hash="test",
        created_at=1.0,
        cwd=str(tmp_path),
    )
    sessions: dict[str, Any] = {}

    def session_factory(sid: str):  # type: ignore[no-untyped-def]
        """构造 file session，输入为 session ID，输出为仓库业务 session 实现。"""
        session = build_session(cfg, sid, bootstrap=bootstrap)
        sessions[sid] = session
        return session

    registry = ToolRegistry([build_run_agent_workflow_tool(handle), *extra_tools])
    enabled_tool_names = ["run_agent_workflow", *(tool.name for tool in extra_tools)]
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
            max_turns=5,
        ),
    )
    return runtime, sessions


async def _wait_until(predicate: Any, *, timeout: float = 1.0) -> None:
    """等待条件成立，输入为同步 predicate，输出为超时前完成或抛 AssertionError。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("predicate did not become true within timeout")
        await asyncio.sleep(0.01)


def _read_json(path: Path) -> dict[str, Any] | None:
    """读取可选 JSON 文件，输入为路径，输出为字典或 None。"""
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _last_audit_action(workflow_dir: Path) -> str | None:
    """读取最后一条 workflow 审计动作，输入为 workflow 目录，输出为 action 或 None。"""
    audit_path = workflow_dir / "audit.jsonl"
    if not audit_path.is_file():
        return None
    records = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not records:
        return None
    action = records[-1].get("action")
    return action if isinstance(action, str) else None


def _workflow_dir(cfg: Config, parent_session_id: str) -> Path:
    """定位唯一 workflow 目录，输入为配置和父 session，输出为单个 workflow 目录。"""
    workflow_root = Path(cfg.session.file_store_path) / parent_session_id / "agent-workflows"
    workflow_dirs = sorted(workflow_root.glob("wf-*"))
    assert len(workflow_dirs) == 1
    return workflow_dirs[0]


def _task_progress(cfg: Config, parent_session_id: str) -> dict[str, Any]:
    """读取任务进度文件，输入为配置和父 session，输出为 task_progress JSON payload。"""
    return (
        _read_json(Path(cfg.session.file_store_path) / parent_session_id / "task_progress.json")
        or {}
    )


def _agent_result_statuses(workflow_dir: Path) -> list[str]:
    """读取子任务 result 状态，输入为 workflow 目录，输出为按路径排序的状态列表。"""
    statuses: list[str] = []
    for path in sorted(workflow_dir.glob("agents/*/result.json")):
        payload = _read_json(path) or {}
        status = payload.get("status")
        if isinstance(status, str):
            statuses.append(status)
    return statuses


async def _parent_tool_results(sessions: dict[str, Any]) -> list[Message]:
    """读取父 workflow 工具结果，输入为 session 表，输出为 call-workflow 对应 tool 消息。"""
    parent_history = await sessions["parent-session"].history()
    return [
        message
        for message in parent_history
        if message.role == "tool" and message.tool_call_id == "call-workflow"
    ]


@pytest.mark.asyncio
async def test_user_interrupt_marks_parallel_workflow_cancelled(tmp_path: Path) -> None:
    """验证用户中断父 run 时所有业务状态取消，输入为真实子 agent manager，输出为红测断言。"""
    cfg = _config(tmp_path)
    llm = _WorkflowInterruptLLM()
    handle = AgentWorkflowHandle()
    runtime, sessions = _runtime(tmp_path, cfg, llm, handle)
    binding = bind_workflow_agent_tree(runtime)
    manager = AgentWorkflowManager(
        runtime=runtime,
        agent_manager=binding.manager,
        config=cfg,
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )
    handle.bind(manager)

    parent_task = asyncio.create_task(
        runtime.run(
            "请派两个子 agent 检查中断链路",
            session_id="parent-session",
            thread_id="parent-session",
            agent_id=str(binding.parent_agent["agent_id"]),
        )
    )
    try:
        await _wait_until(lambda: llm.child_started_count == 2, timeout=1.0)
        parent_task.cancel()
        result = await asyncio.wait_for(parent_task, timeout=2.0)
        await _wait_until(lambda: llm.child_cancelled_count == 2, timeout=1.0)
    finally:
        await binding.aclose()
        await runtime.aclose()

    workflow_dir = _workflow_dir(cfg, "parent-session")
    manifest = _read_json(workflow_dir / "workflow.json") or {}
    result_payload = _read_json(workflow_dir / "result.json") or {}
    report_index = _read_json(workflow_dir / "reports" / "index.json") or {}
    progress = _task_progress(cfg, "parent-session")
    tool_results = await _parent_tool_results(sessions)
    lifecycle_records = binding.manager.list_task_records(
        "parent-session",
        include_finished=True,
        limit=10,
    )
    observed = {
        "parent_status": result.status,
        "cancelled_tool_call_id": result.metadata.get("cancelled_tool_call_id"),
        "parent_tool_result_count": len(tool_results),
        "parent_tool_result_interrupted": (
            tool_results[-1].metadata.get("interrupted") if tool_results else None
        ),
        "parent_tool_result_interrupt_reason": (
            tool_results[-1].metadata.get("interrupt_reason") if tool_results else None
        ),
        "child_llm_started_count": llm.child_started_count,
        "child_llm_cancelled_count": llm.child_cancelled_count,
        "child_lifecycle_statuses": sorted(record.status for record in lifecycle_records),
        "workflow_status": manifest.get("status"),
        "workflow_finished": bool(manifest.get("finished_at")),
        "result_exists": (workflow_dir / "result.json").is_file(),
        "result_status": result_payload.get("status"),
        "result_completed": result_payload.get("completed"),
        "result_run_statuses": sorted(run.get("status") for run in result_payload.get("runs", [])),
        "report_index_status": report_index.get("status"),
        "report_count": len(report_index.get("reports", [])),
        "agent_result_statuses": _agent_result_statuses(workflow_dir),
        "progress_control_mode": progress.get("control_mode"),
        "progress_counts": progress.get("counts"),
        "progress_task_statuses": sorted(item.get("status") for item in progress.get("tasks", [])),
        "audit_last_action": _last_audit_action(workflow_dir),
    }
    expected = {
        "parent_status": "cancelled",
        "cancelled_tool_call_id": "call-workflow",
        "parent_tool_result_count": 1,
        "parent_tool_result_interrupted": True,
        "parent_tool_result_interrupt_reason": "user_interrupt",
        "child_llm_started_count": 2,
        "child_llm_cancelled_count": 2,
        "child_lifecycle_statuses": ["cancelled", "cancelled"],
        "workflow_status": "cancelled",
        "workflow_finished": True,
        "result_exists": True,
        "result_status": "cancelled",
        "result_completed": False,
        "result_run_statuses": ["cancelled", "cancelled"],
        "report_index_status": "cancelled",
        "report_count": 2,
        "agent_result_statuses": ["cancelled", "cancelled"],
        "progress_control_mode": "runtime_lifecycle",
        "progress_counts": {
            "pending": 0,
            "in_progress": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 2,
            "total": 2,
        },
        "progress_task_statuses": [
            "cancelled",
            "cancelled",
        ],
        "audit_last_action": "workflow_cancelled",
    }
    assert observed == expected, json.dumps(
        {"observed": observed, "expected": expected},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


@pytest.mark.asyncio
async def test_manager_cancel_finalizer_covers_strategy_entrypoint(tmp_path: Path) -> None:
    """验证任意策略经 manager 入口取消时写统一终态，输入为 fake strategy，输出为产物断言。"""
    cfg = _config(tmp_path)
    runtime, _sessions = _runtime(tmp_path, cfg, _WorkflowInterruptLLM(), AgentWorkflowHandle())
    binding = bind_workflow_agent_tree(runtime)
    manager = AgentWorkflowManager(
        runtime=runtime,
        agent_manager=binding.manager,
        config=cfg,
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )
    strategy = _ManagerCancelStrategy(manager)
    manager._strategy_manager.register(strategy)

    workflow_task = asyncio.create_task(
        manager.run_workflow(
            WorkflowRunRequest(
                mode=strategy.mode,
                parent_session_id="parent-session",
                payload={},
                source="unit-test",
                desc="manager 统一取消探针",
                parent_agent=binding.parent_agent,
            )
        )
    )
    try:
        await asyncio.wait_for(strategy.started.wait(), timeout=1.0)
        workflow_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(workflow_task, timeout=2.0)
    finally:
        await binding.aclose()
        await runtime.aclose()

    workflow_dir = _workflow_dir(cfg, "parent-session")
    manifest = _read_json(workflow_dir / "workflow.json") or {}
    result_payload = _read_json(workflow_dir / "result.json") or {}
    report_index = _read_json(workflow_dir / "reports" / "index.json") or {}
    progress = _task_progress(cfg, "parent-session")
    observed = {
        "strategy_cancelled": strategy.cancelled.is_set(),
        "workflow_status": manifest.get("status"),
        "workflow_finished": bool(manifest.get("finished_at")),
        "assigned_count": len(manifest.get("assigned_agents", [])),
        "result_status": result_payload.get("status"),
        "result_completed": result_payload.get("completed"),
        "result_run_statuses": sorted(run.get("status") for run in result_payload.get("runs", [])),
        "report_index_status": report_index.get("status"),
        "report_count": len(report_index.get("reports", [])),
        "agent_result_statuses": _agent_result_statuses(workflow_dir),
        "progress_control_mode": progress.get("control_mode"),
        "progress_counts": progress.get("counts"),
        "progress_task_statuses": sorted(item.get("status") for item in progress.get("tasks", [])),
        "audit_last_action": _last_audit_action(workflow_dir),
    }
    expected = {
        "strategy_cancelled": True,
        "workflow_status": "cancelled",
        "workflow_finished": True,
        "assigned_count": 1,
        "result_status": "cancelled",
        "result_completed": False,
        "result_run_statuses": ["cancelled"],
        "report_index_status": "cancelled",
        "report_count": 1,
        "agent_result_statuses": ["cancelled"],
        "progress_control_mode": "runtime_lifecycle",
        "progress_counts": {
            "pending": 0,
            "in_progress": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 1,
            "total": 1,
        },
        "progress_task_statuses": ["cancelled"],
        "audit_last_action": "workflow_cancelled",
    }
    assert observed == expected, json.dumps(
        {"observed": observed, "expected": expected},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


@pytest.mark.asyncio
async def test_user_interrupt_cancels_child_tool_execution(tmp_path: Path) -> None:
    """验证用户中断父 run 时子 agent 正在执行的工具也写取消占位，输入为真实 scoped 工具链路。"""
    cfg = _config(tmp_path)
    llm = _ChildToolInterruptLLM()
    handle = AgentWorkflowHandle()
    read_tool = _HangingReadFileTool()
    runtime, sessions = _runtime(tmp_path, cfg, llm, handle, extra_tools=[read_tool])
    binding = bind_workflow_agent_tree(runtime)
    manager = AgentWorkflowManager(
        runtime=runtime,
        agent_manager=binding.manager,
        config=cfg,
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )
    handle.bind(manager)

    parent_task = asyncio.create_task(
        runtime.run(
            "请派一个子 agent 用文件工具检查中断链路",
            session_id="parent-session",
            thread_id="parent-session",
            agent_id=str(binding.parent_agent["agent_id"]),
        )
    )
    try:
        await asyncio.wait_for(read_tool.started.wait(), timeout=1.0)
        parent_task.cancel()
        result = await asyncio.wait_for(parent_task, timeout=2.0)
        await _wait_until(lambda: read_tool.cancelled_count == 1, timeout=1.0)
    finally:
        await binding.aclose()
        await runtime.aclose()

    workflow_dir = _workflow_dir(cfg, "parent-session")
    manifest = _read_json(workflow_dir / "workflow.json") or {}
    result_payload = _read_json(workflow_dir / "result.json") or {}
    report_index = _read_json(workflow_dir / "reports" / "index.json") or {}
    progress = _task_progress(cfg, "parent-session")
    parent_tool_results = await _parent_tool_results(sessions)
    lifecycle_records = binding.manager.list_task_records(
        "parent-session",
        include_finished=True,
        limit=10,
    )
    child_session_id = lifecycle_records[0].session_id if lifecycle_records else ""
    child_history = await sessions[child_session_id].history() if child_session_id else []
    child_tool_results = [
        message
        for message in child_history
        if message.role == "tool" and message.tool_call_id == "call-child-read"
    ]
    observed = {
        "parent_status": result.status,
        "cancelled_tool_call_id": result.metadata.get("cancelled_tool_call_id"),
        "parent_tool_result_count": len(parent_tool_results),
        "parent_tool_result_interrupted": (
            parent_tool_results[-1].metadata.get("interrupted") if parent_tool_results else None
        ),
        "child_tool_started_count": read_tool.started_count,
        "child_tool_cancelled_count": read_tool.cancelled_count,
        "child_tool_call_ids": read_tool.call_ids,
        "child_tool_result_count": len(child_tool_results),
        "child_tool_result_interrupted": (
            child_tool_results[-1].metadata.get("interrupted") if child_tool_results else None
        ),
        "child_tool_result_interrupt_reason": (
            child_tool_results[-1].metadata.get("interrupt_reason") if child_tool_results else None
        ),
        "child_lifecycle_statuses": sorted(record.status for record in lifecycle_records),
        "workflow_status": manifest.get("status"),
        "workflow_finished": bool(manifest.get("finished_at")),
        "result_exists": (workflow_dir / "result.json").is_file(),
        "result_status": result_payload.get("status"),
        "result_completed": result_payload.get("completed"),
        "result_run_statuses": sorted(run.get("status") for run in result_payload.get("runs", [])),
        "report_index_status": report_index.get("status"),
        "report_count": len(report_index.get("reports", [])),
        "agent_result_statuses": _agent_result_statuses(workflow_dir),
        "progress_control_mode": progress.get("control_mode"),
        "progress_counts": progress.get("counts"),
        "progress_task_statuses": sorted(item.get("status") for item in progress.get("tasks", [])),
        "audit_last_action": _last_audit_action(workflow_dir),
    }
    expected = {
        "parent_status": "cancelled",
        "cancelled_tool_call_id": "call-workflow",
        "parent_tool_result_count": 1,
        "parent_tool_result_interrupted": True,
        "child_tool_started_count": 1,
        "child_tool_cancelled_count": 1,
        "child_tool_call_ids": ["call-child-read"],
        "child_tool_result_count": 1,
        "child_tool_result_interrupted": True,
        "child_tool_result_interrupt_reason": "user_interrupt",
        "child_lifecycle_statuses": ["cancelled"],
        "workflow_status": "cancelled",
        "workflow_finished": True,
        "result_exists": True,
        "result_status": "cancelled",
        "result_completed": False,
        "result_run_statuses": ["cancelled"],
        "report_index_status": "cancelled",
        "report_count": 1,
        "agent_result_statuses": ["cancelled"],
        "progress_control_mode": "runtime_lifecycle",
        "progress_counts": {
            "pending": 0,
            "in_progress": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 1,
            "total": 1,
        },
        "progress_task_statuses": ["cancelled"],
        "audit_last_action": "workflow_cancelled",
    }
    assert observed == expected, json.dumps(
        {"observed": observed, "expected": expected},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
