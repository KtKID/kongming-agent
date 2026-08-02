"""智能体工作流管理器。

本脚本负责多 agent 编排的 facade、并行子任务执行、子 agent 审计文件写入和结果收口。
作用是把父 agent 选择的 workflow strategy 转换为可执行的子 agent 任务，并把运行结果物化为 workflow.json、audit.jsonl、result.json 和 reports/index.json。
关键执行流程：注册 workflow strategy，接收 mode 和 task_specs，创建 workflow 目录，分配子 agent 工作目录，并发运行子任务，写入审计与报告后返回 AgentWorkflowResult。
关键函数：run_workflow 负责策略注册表分发，run_workflow_specs 把公开 task_specs 转成策略 payload，_run_one 收口单个子 agent 结果。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
import uuid
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from application.agent_roles import AgentRoleManager
from application.agent_workflows.context import WorkflowExecutionContext, WorkflowRuntime
from application.agent_workflows.models import (
    ActiveWorkflowHandle,
    AgentWorkflowResult,
    SubAgentReportProjection,
    WorkflowMode,
)
from application.agent_workflows.strategies.base import WorkflowRunRequest
from application.agent_workflows.strategies.deep_research.strategy import (
    DeepResearchStrategy,
)
from application.agent_workflows.strategies.description import (
    WorkflowStrategyCatalogEntry,
    WorkflowStrategyDescription,
)
from application.agent_workflows.strategies.manager import (
    AgentWorkflowStrategyManager,
)
from application.agent_workflows.strategies.map_reduce.strategy import MapReduceStrategy
from application.agent_workflows.strategies.parallel import ParallelWorkflowStrategy
from application.agent_workflows.strategies.roundtable_review.strategy import (
    RoundtableReviewStrategy,
)
from application.agent_workflows.strategies.task_flow.strategy import TaskFlowStrategy
from application.agent_workflows.task_models import SubAgentRun, SubAgentTask
from application.agents.subagent_tools import (
    build_spawn_request_from_workflow_task,
    parent_agent_id_from_snapshot,
)
from application.subagents.permissions import (
    ALLOWED_SCOPED_FILE_TOOLS,
    SubAgentCreationRecord,
    SubAgentGrant,
    SubAgentToolAuditHook,
    WorkflowAuditWriter,
    to_jsonable,
    validate_scoped_tool_names,
    wrap_scoped_file_tools,
)
from application.subagents.runtime_resolver import (
    ResolvedSubAgentRuntime,
    SubAgentRuntimeResolver,
    resolved_runtime_payload,
)
from application.tool_scope import clip_child_tool_snapshot
from core.contracts import ProviderUsageSnapshot, Tool, ToolLookup
from core.lifecycle import LifecycleHook
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.models import Config
from infrastructure.config.paths import resolve_kongming_path
from sessions import (
    RuntimeTaskProgressStatus,
    SessionTaskProgressManager,
    TaskProgressControlMode,
    TaskProgressTaskDefinition,
)

_WORKFLOW_DESC_MAX_CHARS = 120
_WORKFLOW_ID_CONTEXT: ContextVar[str | None] = ContextVar(
    "agent_workflow_id",
    default=None,
)


class WorkflowRuntimeResources(Protocol):
    """workflow spawn 解析工具和审计路径所需的父 runtime 最小合同。"""

    @property
    def config(self) -> Config:
        """返回 runtime 配置快照。"""

    @property
    def tools(self) -> ToolLookup:
        """返回 runtime 工具查询门户。"""

    @property
    def enabled_tools_snapshot(self) -> tuple[Tool, ...] | None:
        """返回父 run 工具快照；动态工具尚未解析时返回空值。"""


@dataclass(frozen=True)
class SubAgentReportDetail:
    """写入 reports/<task_run_id>.json 的可审计子 agent 报告明细。"""

    task_id: str
    task_name: str
    session_id: str
    run_id: str
    status: str
    summary: str
    content: str
    error_message: str | None
    working_dir: str | None
    content_digest: str
    reported_at: str
    resolved_runtime: dict[str, object]
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _RunOutcome:
    """单个子 agent 运行结果和报告摘要的内部聚合结构。"""

    run: SubAgentRun
    report: SubAgentReportProjection


@dataclass(frozen=True)
class AgentWorkflowAuditWriter:
    """写入 AgentWorkflowManager 管辖的 workflow 审计文件。"""

    _workflow_dir: Path
    _resolved_runtime: dict[str, object] | None

    def __init__(
        self,
        workflow_dir: Path,
        *,
        resolved_runtime: Mapping[str, object] | None = None,
    ) -> None:
        """初始化审计写入器，输入为 workflow 目录，输出为绑定目录的 writer。"""
        object.__setattr__(self, "_workflow_dir", workflow_dir)
        object.__setattr__(
            self,
            "_resolved_runtime",
            dict(resolved_runtime) if resolved_runtime is not None else None,
        )

    @property
    def audit_log_path(self) -> Path:
        """返回审计日志路径，输入为 workflow 目录，输出为 audit.jsonl 路径。"""
        return self._workflow_dir / "audit.jsonl"

    @property
    def resolved_runtime_payload(self) -> dict[str, object] | None:
        """返回 workflow runtime 摘要，输入为 writer 状态，输出为审计可写入 payload。"""
        return dict(self._resolved_runtime) if self._resolved_runtime is not None else None

    def write_event(self, event: Mapping[str, Any]) -> None:
        """写入审计事件，输入为事件映射，输出为追加到 audit.jsonl 的一行 JSON。"""
        action = event.get("action")
        if not isinstance(action, str) or not action:
            raise ValueError("workflow audit event requires non-empty action")
        payload_raw = event.get("payload", {})
        payload = payload_raw if isinstance(payload_raw, dict) else {"value": payload_raw}
        payload = _audit_payload_with_runtime(payload, self._resolved_runtime)
        record = {
            "ts": event.get("ts") if isinstance(event.get("ts"), str) else _now_iso(),
            "action": action,
            "payload": to_jsonable(payload),
        }
        self._workflow_dir.mkdir(parents=True, exist_ok=True)
        with open(self.audit_log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def write_subagent_creation(self, record: SubAgentCreationRecord) -> None:
        """写入子 agent 创建记录，输入为创建记录，输出为 subagent.json 和对应审计事件。"""
        self._write_json(record.task_run_dir / "subagent.json", to_jsonable(record))
        self.write_event(
            {
                "action": "subagent_created",
                "payload": {
                    "workflow_id": record.workflow_id,
                    "task_id": record.task_id,
                    "task_run_id": record.task_run_id,
                    "task_name": record.task_name,
                    "session_id": record.session_id,
                    "working_dir": str(record.working_dir),
                    "subagent_json_path": str(record.task_run_dir / "subagent.json"),
                    "resolved_runtime": record.resolved_runtime,
                },
            }
        )
        self.write_event(
            {
                "action": "subagent_grant_bound",
                "payload": to_jsonable(record.grant),
            }
        )

    def _write_json(self, path: Path, payload: dict[str, object]) -> None:
        """原子写入 JSON 文件，输入为路径和 payload，输出为目标文件内容更新。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)


class _CatalogOnlyWorkflowFacade:
    """只用于生成默认策略目录的轻量 facade，输入为属性访问，输出为明确运行期错误。"""

    def __getattr__(self, name: str) -> object:
        """拒绝 catalog 查询之外的运行期能力，输入为属性名，输出为 RuntimeError。"""
        raise RuntimeError(f"workflow catalog facade cannot provide runtime attribute: {name}")


def _catalog_only_workflow_context(request: WorkflowRunRequest) -> WorkflowExecutionContext:
    """拒绝通过 catalog-only manager 执行策略，输入为运行请求，输出为 RuntimeError。"""
    raise RuntimeError(
        f"workflow strategy {request.mode!r} cannot run from the catalog-only registry"
    )


def _register_default_workflow_strategies(
    strategy_manager: AgentWorkflowStrategyManager,
    workflow_facade: WorkflowRuntime,
) -> None:
    """注册默认 workflow 策略，输入为策略管理器和 facade，输出为已写入注册表。"""
    strategy_manager.register(ParallelWorkflowStrategy())
    strategy_manager.register(DeepResearchStrategy(workflow_facade))
    strategy_manager.register(MapReduceStrategy(workflow_facade))
    strategy_manager.register(RoundtableReviewStrategy(workflow_facade))
    strategy_manager.register(TaskFlowStrategy(workflow_facade))


class _ChildResultMailboxDemux:
    """workflow child_result 分发器，输入为父 mailbox，输出按 task_id 等待的 Mail。"""

    def __init__(self, mailbox: asyncio.Queue[Any]) -> None:
        """初始化 demux，输入为父 mailbox，输出后台 pump 任务。"""
        self._mailbox = mailbox
        self._waiters: dict[str, asyncio.Future[Any]] = {}
        self._cached: dict[str, Any] = {}
        self._ignored_task_ids: set[str] = set()
        self._requeue: list[Any] = []
        self._closed = False
        self._task = asyncio.create_task(self._pump(), name="workflow-child-result-demux")

    async def wait(self, *, task_id: str, timeout_seconds: float) -> Any:
        """等待指定 task_id 的 child_result，输入为 task_id/超时，输出匹配 Mail。"""
        if task_id in self._cached:
            return self._cached.pop(task_id)
        loop = asyncio.get_event_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._waiters[task_id] = future
        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        finally:
            self._waiters.pop(task_id, None)

    def ignore(self, task_id: str) -> None:
        """忽略迟到 child_result，输入为 task_id，输出为 demux 状态更新。"""
        self._ignored_task_ids.add(task_id)
        self._cached.pop(task_id, None)

    async def close(self) -> None:
        """关闭 demux，输入为空，输出为未消费消息回放到父 mailbox。"""
        if self._closed:
            return
        self._closed = True
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        for mail in [*self._cached.values(), *self._requeue]:
            self._mailbox.put_nowait(mail)
        self._cached.clear()
        self._requeue.clear()

    async def _pump(self) -> None:
        """后台读取父 mailbox，输入为空，输出为 waiter/cache/requeue 状态更新。"""
        while True:
            mail = await self._mailbox.get()
            if getattr(mail, "kind", None) != "child_result":
                self._requeue.append(mail)
                continue
            task_id = getattr(mail, "task_id", None)
            if not isinstance(task_id, str) or not task_id:
                self._requeue.append(mail)
                continue
            if task_id in self._ignored_task_ids:
                continue
            waiter = self._waiters.get(task_id)
            if waiter is not None and not waiter.done():
                waiter.set_result(mail)
            else:
                self._cached[task_id] = mail


class AgentWorkflowManager:
    """协调子 agent 执行并持有 workflow 审计产物。"""

    def __init__(
        self,
        *,
        config: Config,
        workspace_root: Path,
        role_manager: AgentRoleManager,
        runtime: WorkflowRuntimeResources | None = None,
        model_catalog_manager: ModelCatalogManager | None = None,
        agent_manager: Any | None = None,
        agent_manager_getter: Callable[[], Any | None] | None = None,
        deep_research_source_provider: Any | None = None,
        deep_research_source_diagnostics: Any | None = None,
    ) -> None:
        """初始化管理器，输入为父 runtime/AgentManager 和工作区，输出为 workflow facade。"""
        self._runtime = runtime
        self._agent_manager = agent_manager
        self._agent_manager_getter = agent_manager_getter
        self._config = config
        self._workspace_root = workspace_root.resolve()
        self._role_manager = role_manager
        self._runtime_resolver = SubAgentRuntimeResolver(
            config,
            role_manager,
            model_catalog_manager=model_catalog_manager,
        )
        self._deep_research_source_provider = deep_research_source_provider
        self._deep_research_source_diagnostics = deep_research_source_diagnostics
        self._strategy_manager = AgentWorkflowStrategyManager(
            context_factory=self._build_workflow_context
        )
        _register_default_workflow_strategies(self._strategy_manager, self)
        self._task_progress_manager = _build_task_progress_manager(config)
        # 运行中 workflow 的注册表：workflow_id → 句柄，供 list_active_workflows / cancel_workflow 使用。
        self._active_workflows: dict[str, ActiveWorkflowHandle] = {}
        self._child_result_demuxes: dict[str, _ChildResultMailboxDemux] = {}

    def _current_agent_manager(self) -> Any | None:
        """读取当前 AgentManager，输入为空，输出为直接注入或延迟解析的 manager。"""
        if self._agent_manager is not None:
            return self._agent_manager
        if self._agent_manager_getter is None:
            return None
        return self._agent_manager_getter()

    @classmethod
    def list_default_workflow_strategies(cls) -> tuple[WorkflowStrategyCatalogEntry, ...]:
        """列出默认 workflow 策略目录，输入为空，输出为 Web catalog 可复用的注册结果。"""
        strategy_manager = AgentWorkflowStrategyManager(
            context_factory=_catalog_only_workflow_context
        )
        _register_default_workflow_strategies(
            strategy_manager,
            cast(WorkflowRuntime, _CatalogOnlyWorkflowFacade()),
        )
        return strategy_manager.list_strategies()

    @classmethod
    def list_default_workflow_strategy_descriptions(
        cls,
    ) -> tuple[WorkflowStrategyDescription, ...]:
        """列出默认 workflow 策略详情，输入为空，输出为 prompt catalog 可复用的说明列表。"""
        strategy_manager = AgentWorkflowStrategyManager(
            context_factory=_catalog_only_workflow_context
        )
        _register_default_workflow_strategies(
            strategy_manager,
            cast(WorkflowRuntime, _CatalogOnlyWorkflowFacade()),
        )
        return tuple(
            strategy_manager.describe_strategy(entry.mode)
            for entry in strategy_manager.list_strategies()
        )

    @property
    def workspace_root(self) -> Path:
        """返回 workflow 运行的工作区根目录，输入为 manager 状态，输出为绝对路径。"""
        return self._workspace_root

    @property
    def role_manager(self) -> AgentRoleManager:
        """返回共享角色管理器，输入为 manager 状态，输出为 AgentRoleManager。"""
        return self._role_manager

    @property
    def deep_research_source_provider(self) -> Any | None:
        """返回 deep_research 来源 provider，输入为 manager 状态，输出为可选 provider。"""
        return self._deep_research_source_provider

    @property
    def deep_research_source_diagnostics(self) -> Any | None:
        """返回 Web 来源 provider 诊断，输入为 manager 状态，输出为可选诊断载荷。"""
        return self._deep_research_source_diagnostics

    def bind_deep_research_source_provider(
        self,
        provider: Any | None,
        diagnostics: Any | None = None,
    ) -> None:
        """绑定 deep_research 来源 provider 和诊断，输入为 provider/diagnostics，输出为状态更新。"""
        self._deep_research_source_provider = provider
        self._deep_research_source_diagnostics = diagnostics

    def set_deep_research_source_provider(
        self,
        provider: Any | None,
        diagnostics: Any | None = None,
    ) -> None:
        """设置 deep_research 来源 provider 和诊断，输入为 provider/diagnostics，输出为兼容测试入口。"""
        self.bind_deep_research_source_provider(provider, diagnostics=diagnostics)

    def list_workflow_strategies(self) -> tuple[WorkflowStrategyCatalogEntry, ...]:
        """列出已注册策略，输入为当前策略注册表，输出为父 agent 可查看的策略目录。"""
        return self._strategy_manager.list_strategies()

    def describe_workflow_strategy(self, mode: str) -> WorkflowStrategyDescription:
        """查询策略详情，输入为策略 mode，输出为面向 LLM 的中文策略说明。"""
        return self._strategy_manager.describe_strategy(mode)

    async def run_workflow(self, request: WorkflowRunRequest) -> AgentWorkflowResult:
        """执行 workflow 并维护运行时注册表，输入为运行请求，输出为 AgentWorkflowResult。

        workflow_id 在 task 发起前生成并登记到 _active_workflows，使外部能在运行期间
        通过 list_active_workflows 查询、通过 cancel_workflow(id) 单独停止某个 workflow。
        cancel 时统一 finalize manifest/result/report/progress 和 workflow_cancelled 审计，
        根治各 strategy 自行收口遗漏 cancel 的结构缺陷。
        """
        self._require_parent_agent_manager(request.parent_agent)
        workflow_id = self._generate_workflow_id()
        started_at = _now_iso()
        # create_task 会复制当前 ContextVar，上下文工厂稍后运行时仍能读取同一个 workflow_id。
        token = _WORKFLOW_ID_CONTEXT.set(workflow_id)
        try:
            result_task = asyncio.create_task(self._strategy_manager.run_strategy(request))
        finally:
            _WORKFLOW_ID_CONTEXT.reset(token)

        handle = ActiveWorkflowHandle(
            workflow_id=workflow_id,
            parent_session_id=request.parent_session_id,
            mode=request.mode.strip(),
            started_at=started_at,
            task=result_task,
        )
        self._active_workflows[workflow_id] = handle

        try:
            result = await result_task
        except asyncio.CancelledError:
            await self._finalize_cancelled_workflow(workflow_id, request, started_at)
            raise
        finally:
            self._active_workflows.pop(workflow_id, None)
            demux = self._child_result_demuxes.pop(workflow_id, None)
            if demux is not None:
                await demux.close()

        if not isinstance(result, AgentWorkflowResult):
            raise TypeError("agent workflow strategy returned an invalid result")
        return result

    def _require_parent_agent_manager(
        self,
        parent_agent: Mapping[str, object] | None,
    ) -> Any:
        """校验 workflow spawn owner，输入为父快照，输出为已 boot 且含父 cell 的 Manager。"""
        agent_manager = self._current_agent_manager()
        parent_agent_id = parent_agent_id_from_snapshot(parent_agent)
        if (
            agent_manager is None
            or parent_agent_id is None
            or agent_manager.get_agent(parent_agent_id) is None
        ):
            raise RuntimeError(
                "workflow requires a booted parent AgentManager and parent agent identity"
            )
        return agent_manager

    def _generate_workflow_id(self) -> str:
        """生成 workflow 唯一 ID，输入为空，输出为时间戳+随机后缀的稳定 ID。"""
        return f"wf-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"

    async def _finalize_cancelled_workflow(
        self,
        workflow_id: str,
        request: WorkflowRunRequest,
        started_at: str,
    ) -> None:
        """cancel 收口：把 workflow 产物改为 cancelled 并写审计事件，输入为被取消的 workflow 信息。

        manifest/result/report_index 的 finished_at 与审计的 cancelled_at 共用同一时间戳，
        保证各处产物时刻一致。
        """
        workflow_dir = self._workflow_dir(
            parent_session_id=request.parent_session_id,
            workflow_id=workflow_id,
        )
        cancelled_at = _now_iso()
        existing_manifest = _read_json_dict(workflow_dir / "workflow.json")
        tasks = _tasks_from_workflow_manifest(existing_manifest)
        runs, reports = self._cancelled_workflow_runs(
            workflow_dir=workflow_dir,
            workflow_id=workflow_id,
            parent_session_id=request.parent_session_id,
            tasks=tasks,
        )
        report_index_path = self._write_report_index(
            workflow_dir,
            workflow_id=workflow_id,
            mode=request.mode.strip(),
            parent_session_id=request.parent_session_id,
            status="cancelled",
            reports=reports,
            desc=_normalize_workflow_desc(request.desc),
        )
        self._write_cancelled_workflow_result(
            workflow_dir=workflow_dir,
            workflow_id=workflow_id,
            mode=request.mode.strip(),
            parent_session_id=request.parent_session_id,
            started_at=started_at,
            finished_at=cancelled_at,
            desc=_normalize_workflow_desc(request.desc),
            report_index_path=report_index_path,
            reports=reports,
            runs=runs,
        )
        self._record_completed_workflow_progress(
            parent_session_id=request.parent_session_id,
            workflow_id=workflow_id,
            runs=runs,
        )
        # 若 strategy 已写过 manifest（running），覆写为 cancelled；否则补写一份 cancelled manifest。
        self._write_workflow_manifest(
            workflow_dir,
            workflow_id=workflow_id,
            mode=request.mode.strip(),
            parent_session_id=request.parent_session_id,
            started_at=started_at,
            desc=_normalize_workflow_desc(request.desc),
            tasks=tasks,
            status="cancelled",
            finished_at=cancelled_at,
            resolved_runtime=_workflow_runtime_payload(tasks)
            or _mapping_or_none(existing_manifest.get("resolved_runtime")),
        )
        self._append_audit(
            workflow_dir,
            action="workflow_cancelled",
            payload={
                "workflow_id": workflow_id,
                "mode": request.mode.strip(),
                "parent_session_id": request.parent_session_id,
                "cancelled_at": cancelled_at,
            },
        )

    def _cancelled_workflow_runs(
        self,
        *,
        workflow_dir: Path,
        workflow_id: str,
        parent_session_id: str,
        tasks: list[SubAgentTask],
    ) -> tuple[tuple[SubAgentRun, ...], tuple[SubAgentReportProjection, ...]]:
        """汇总取消时的子任务结果，输入为任务列表，输出 runs/reports。"""
        runs: list[SubAgentRun] = []
        reports: list[SubAgentReportProjection] = []
        for display_order, task in enumerate(tasks, 1):
            result_path = _agent_result_path(workflow_dir, task)
            result_payload = _read_json_dict(result_path)
            run = (
                _subagent_run_from_result_payload(task, result_payload)
                if result_payload
                else _cancelled_run(
                    workflow_id=workflow_id,
                    parent_session_id=parent_session_id,
                    task=task,
                )
            )
            if not result_payload:
                self._write_json(result_path, _run_payload(run))
            runs.append(run)
            reports.append(
                self._write_subagent_report(
                    workflow_dir,
                    workflow_id=workflow_id,
                    run=run,
                    display_order=display_order,
                )
            )
        return tuple(runs), tuple(reports)

    def _write_cancelled_workflow_result(
        self,
        *,
        workflow_dir: Path,
        workflow_id: str,
        mode: str,
        parent_session_id: str,
        started_at: str,
        finished_at: str,
        desc: str | None,
        report_index_path: Path,
        reports: tuple[SubAgentReportProjection, ...],
        runs: tuple[SubAgentRun, ...],
    ) -> None:
        """写入 cancelled root result，输入为取消收口产物，输出 result.json。"""
        self._write_json(
            workflow_dir / "result.json",
            {
                "workflow_id": workflow_id,
                "mode": mode,
                "parent_session_id": parent_session_id,
                "workflow_dir": str(workflow_dir),
                "started_at": started_at,
                "finished_at": finished_at,
                "desc": desc,
                "status": "cancelled",
                "completed": False,
                "report_index_path": str(report_index_path),
                "resolved_runtime": _workflow_runtime_payload([run.task for run in runs]),
                "reports": [_report_projection_payload(report) for report in reports],
                "runs": [_run_payload(run) for run in runs],
            },
        )

    def list_active_workflows(self) -> tuple[ActiveWorkflowHandle, ...]:
        """返回当前运行中的 workflow 句柄快照，输入为空，输出为不可变句柄元组。"""
        return tuple(self._active_workflows.values())

    async def cancel_workflow(self, workflow_id: str) -> bool:
        """单独取消一个运行中 workflow，输入为 workflow ID，输出为是否命中。

        与取消整个父 run 不同，本方法只取消指定 workflow 的 task，不连带父 run。
        命中返回 True，未知 id 返回 False。
        """
        handle = self._active_workflows.get(workflow_id)
        if handle is None:
            return False
        handle.task.cancel()
        return True

    async def run_workflow_specs(
        self,
        *,
        mode: str,
        parent_session_id: str,
        task_specs: list[dict[str, object]],
        parent_agent: Mapping[str, object] | None = None,
        desc: str | None = None,
    ) -> AgentWorkflowResult:
        """按 mode 执行 workflow，输入为策略 ID、父会话和任务规格，输出为 workflow 结果。"""
        return await self.run_workflow(
            WorkflowRunRequest(
                mode=mode,
                parent_session_id=parent_session_id,
                payload={"task_specs": task_specs},
                source="run_workflow_specs",
                desc=desc,
                parent_agent=parent_agent,
            )
        )

    async def run_workflow_payload(
        self,
        *,
        mode: str,
        parent_session_id: str,
        payload: Mapping[str, object],
        parent_agent: Mapping[str, object] | None = None,
        desc: str | None = None,
    ) -> AgentWorkflowResult:
        """按 mode 执行通用 workflow payload，输入为完整策略 payload，输出为 workflow 结果。"""
        return await self.run_workflow(
            WorkflowRunRequest(
                mode=mode,
                parent_session_id=parent_session_id,
                payload=payload,
                source="run_workflow_payload",
                desc=desc,
                parent_agent=parent_agent,
            )
        )

    def prepare_subagent_tasks(
        self,
        *,
        workflow_dir: Path,
        tasks: list[SubAgentTask],
        parent_agent: Mapping[str, object] | None = None,
    ) -> list[SubAgentTask]:
        """为策略任务绑定运行目录，输入为 workflow 目录和任务，输出为带 metadata 的任务列表。"""
        agents_dir = workflow_dir / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        return [
            self._resolve_task_runtime(
                self._with_agent_workdir(
                    task,
                    agents_dir / _task_run_id(index, task.task_id),
                    task_run_id=_task_run_id(index, task.task_id),
                ),
                parent_agent=parent_agent,
            )
            for index, task in enumerate(tasks, 1)
        ]

    async def run_subagent_task(
        self,
        *,
        context: WorkflowExecutionContext,
        task: SubAgentTask,
        display_order: int,
    ) -> _RunOutcome:
        """运行策略子任务，输入为上下文和已分配任务，输出为运行结果和报告摘要。"""
        return await self._run_one(
            workflow_id=context.workflow_id,
            parent_session_id=context.parent_session_id,
            workflow_dir=context.workflow_dir,
            task=self._resolve_task_runtime(task, parent_agent=context.parent_agent),
            display_order=display_order,
            audit_writer=context.audit_writer,
            parent_agent=context.parent_agent,
        )

    def record_subagent_failure(
        self,
        *,
        context: WorkflowExecutionContext,
        task: SubAgentTask,
        display_order: int,
        error: Exception,
        elapsed_ms: int,
    ) -> _RunOutcome:
        """记录策略层子任务失败，输入为失败异常和任务，输出为统一报告结果。"""
        run = _failed_run(
            workflow_id=context.workflow_id,
            parent_session_id=context.parent_session_id,
            task=task,
            error=error,
        )
        payload = {**_run_payload(run), "elapsed_ms": elapsed_ms}
        self._append_audit(context.workflow_dir, action="agent_failed", payload=payload)
        self._record_single_task_progress(
            parent_session_id=context.parent_session_id,
            workflow_id=context.workflow_id,
            task=task,
            runtime_status=RuntimeTaskProgressStatus.FAILED,
            error_message=run.error_message,
        )
        self._write_json(_agent_result_path(context.workflow_dir, task), payload)
        report = self._write_subagent_report(
            context.workflow_dir,
            workflow_id=context.workflow_id,
            run=run,
            display_order=display_order,
        )
        return _RunOutcome(run=run, report=report)

    def write_workflow_manifest(
        self,
        *,
        context: WorkflowExecutionContext,
        tasks: list[SubAgentTask],
        status: str,
        finished_at: str | None = None,
    ) -> None:
        """写入 workflow 清单，输入为策略上下文和任务，输出为 workflow.json。"""
        previous_manifest = _read_json_dict(context.workflow_dir / "workflow.json")
        already_initialized_runtime_progress = (
            previous_manifest.get("workflow_id") == context.workflow_id
            and previous_manifest.get("status") == "running"
        )
        self._write_workflow_manifest(
            context.workflow_dir,
            workflow_id=context.workflow_id,
            mode=context.mode,
            parent_session_id=context.parent_session_id,
            started_at=context.started_at,
            desc=context.desc,
            tasks=tasks,
            status=status,
            finished_at=finished_at,
            resolved_runtime=(
                _workflow_runtime_payload(tasks) or context.audit_writer.resolved_runtime_payload
            ),
        )
        if (
            status == "running"
            and context.mode != "task_flow"
            and not already_initialized_runtime_progress
        ):
            self._open_runtime_task_progress(context=context, tasks=tasks)

    def write_report_index(
        self,
        *,
        context: WorkflowExecutionContext,
        status: str,
        reports: tuple[SubAgentReportProjection, ...],
    ) -> Path:
        """写入 workflow 报告索引，输入为策略上下文和报告，输出为 index.json 路径。"""
        return self._write_report_index(
            context.workflow_dir,
            workflow_id=context.workflow_id,
            mode=context.mode,
            parent_session_id=context.parent_session_id,
            status=status,
            reports=reports,
            desc=context.desc,
        )

    def write_workflow_result(
        self,
        *,
        context: WorkflowExecutionContext,
        finished_at: str,
        completed: bool,
        report_index_path: Path,
        reports: tuple[SubAgentReportProjection, ...],
        runs: tuple[SubAgentRun, ...],
        extra: Mapping[str, object] | None = None,
    ) -> None:
        """写入 root result.json，输入为公共结果和策略扩展数据，输出为结果文件。"""
        payload: dict[str, object] = {
            "workflow_id": context.workflow_id,
            "mode": context.mode,
            "parent_session_id": context.parent_session_id,
            "workflow_dir": str(context.workflow_dir),
            "started_at": context.started_at,
            "finished_at": finished_at,
            "desc": context.desc,
            "completed": completed,
            "report_index_path": str(report_index_path),
            "resolved_runtime": (
                _workflow_runtime_payload([run.task for run in runs])
                or context.audit_writer.resolved_runtime_payload
            ),
            "reports": [_report_projection_payload(report) for report in reports],
            "runs": [_run_payload(run) for run in runs],
        }
        if extra:
            payload.update(to_jsonable(extra))
        self._write_json(context.workflow_dir / "result.json", payload)
        self._record_completed_workflow_progress(
            parent_session_id=context.parent_session_id,
            workflow_id=context.workflow_id,
            runs=runs,
        )

    def _build_workflow_context(self, request: WorkflowRunRequest) -> WorkflowExecutionContext:
        """创建策略执行上下文，输入为运行请求，输出为 workflow ID、目录和审计 writer。"""
        # 优先使用 run_workflow 注入到当前 task context 的 id；catalog-only 等路径回退到自行生成。
        workflow_id = _WORKFLOW_ID_CONTEXT.get() or self._generate_workflow_id()
        started_at = _now_iso()
        workflow_dir = self._workflow_dir(
            parent_session_id=request.parent_session_id,
            workflow_id=workflow_id,
        )
        desc = _normalize_workflow_desc(
            request.desc if request.desc is not None else request.payload.get("desc")
        )
        default_runtime = self._resolve_default_runtime(parent_agent=request.parent_agent)
        return WorkflowExecutionContext(
            workflow_id=workflow_id,
            parent_session_id=request.parent_session_id,
            mode=request.mode,
            workflow_dir=workflow_dir,
            started_at=started_at,
            desc=desc,
            audit_writer=AgentWorkflowAuditWriter(
                workflow_dir,
                resolved_runtime=resolved_runtime_payload(default_runtime),
            ),
            runtime=self,
            parent_agent=request.parent_agent,
            max_concurrency=None,
            workflow_timeout_seconds=None,
        )

    async def _run_one(
        self,
        *,
        workflow_id: str,
        parent_session_id: str,
        workflow_dir: Path,
        task: SubAgentTask,
        display_order: int,
        audit_writer: WorkflowAuditWriter,
        parent_agent: Mapping[str, object] | None = None,
    ) -> _RunOutcome:
        """运行单个子 agent，输入为任务和审计上下文，输出为子任务运行结果和报告摘要。"""
        started = time.perf_counter()
        self._record_single_task_progress(
            parent_session_id=parent_session_id,
            workflow_id=workflow_id,
            task=task,
            runtime_status=RuntimeTaskProgressStatus.RUNNING,
        )
        try:
            run = await self._spawn_and_wait_workflow_task(
                workflow_id=workflow_id,
                parent_session_id=parent_session_id,
                workflow_dir=workflow_dir,
                task=task,
                parent_agent=parent_agent,
                audit_writer=audit_writer,
            )
        except Exception as exc:
            run = _failed_run(
                workflow_id=workflow_id,
                parent_session_id=parent_session_id,
                task=task,
                error=exc,
            )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        payload = {**_run_payload(run), "elapsed_ms": elapsed_ms}
        action = "agent_completed" if run.status == "completed" else "agent_failed"
        self._append_audit(workflow_dir, action=action, payload=payload)
        self._record_single_task_progress(
            parent_session_id=parent_session_id,
            workflow_id=workflow_id,
            task=task,
            runtime_status=(
                RuntimeTaskProgressStatus.COMPLETED
                if run.status == "completed"
                else RuntimeTaskProgressStatus.FAILED
            ),
            error_message=run.error_message,
        )
        self._update_subagent_record_with_usage(task, run)
        self._write_json(_agent_result_path(workflow_dir, task), payload)
        report = self._write_subagent_report(
            workflow_dir,
            workflow_id=workflow_id,
            run=run,
            display_order=display_order,
        )
        return _RunOutcome(run=run, report=report)

    async def _spawn_and_wait_workflow_task(
        self,
        *,
        workflow_id: str,
        parent_session_id: str,
        workflow_dir: Path,
        task: SubAgentTask,
        parent_agent: Mapping[str, object] | None,
        audit_writer: WorkflowAuditWriter,
    ) -> SubAgentRun:
        """通过 AgentManager 派生 workflow 子 agent，输入为任务，输出兼容旧报告链路的 run。"""
        agent_manager = self._current_agent_manager()
        if agent_manager is None:
            raise RuntimeError("agent manager is not bound")
        parent_agent_id = parent_agent_id_from_snapshot(parent_agent)
        if parent_agent_id is None:
            raise RuntimeError("parent agent_id is required for workflow spawn")
        parent_cell = agent_manager.get_agent(parent_agent_id)
        if parent_cell is None:
            raise RuntimeError(f"parent agent not found for agent_id={parent_agent_id!r}")
        child_session_id = _build_child_session_id(
            workflow_id=workflow_id,
            parent_session_id=parent_session_id,
            task_id=_metadata_task_run_id(task),
        )
        run_overrides = self._workflow_spawn_run_overrides(
            workflow_id=workflow_id,
            parent_session_id=parent_session_id,
            child_session_id=child_session_id,
            task=task,
            audit_writer=audit_writer,
        )
        request = build_spawn_request_from_workflow_task(
            parent_agent_id=parent_agent_id,
            workflow_task=task,
            cwd=_workflow_task_cwd(task, fallback=self._workspace_root),
            child_session_id=child_session_id,
            parent_task_id=_parent_task_id_from_snapshot(parent_agent),
            metadata={
                "workflow_id": workflow_id,
                "parent_session_id": parent_session_id,
                "workflow_dir": str(workflow_dir),
                "task_run_id": _metadata_task_run_id(task),
            },
            **run_overrides,
        )
        spawn_result = agent_manager.spawn(request)
        timeout_seconds = _workflow_task_timeout_seconds(
            task,
            fallback=self._runtime_resolver.default_model_config.timeout,
        )
        demux = self._child_result_demuxes.get(workflow_id)
        if demux is None:
            demux = _ChildResultMailboxDemux(parent_cell.mailbox)
            self._child_result_demuxes[workflow_id] = demux
        try:
            mail = await demux.wait(
                task_id=spawn_result.task_id,
                timeout_seconds=timeout_seconds,
            )
        except asyncio.CancelledError:
            demux.ignore(spawn_result.task_id)
            cancel_agent_run = getattr(agent_manager, "cancel_agent_run", None)
            if callable(cancel_agent_run):
                await cancel_agent_run(spawn_result.child_id)
            raise
        except TimeoutError:
            demux.ignore(spawn_result.task_id)
            cancel_agent_run = getattr(agent_manager, "cancel_agent_run", None)
            if callable(cancel_agent_run):
                await cancel_agent_run(spawn_result.child_id)
            if workflow_id not in self._active_workflows:
                direct_demux = self._child_result_demuxes.pop(workflow_id, None)
                if direct_demux is not None:
                    await direct_demux.close()
            raise
        child_cell = agent_manager.get_agent(spawn_result.child_id)
        child_session_id = (
            child_cell.session_id
            if child_cell is not None
            else f"{parent_session_id}-{spawn_result.child_id}"
        )
        if workflow_id not in self._active_workflows:
            demux = self._child_result_demuxes.pop(workflow_id, None)
            if demux is not None:
                await demux.close()
        return _subagent_run_from_child_mail(
            task=task,
            session_id=child_session_id,
            run_id=f"spawn-{spawn_result.task_id}",
            mail=mail,
        )

    def _workflow_spawn_run_overrides(
        self,
        *,
        workflow_id: str,
        parent_session_id: str,
        child_session_id: str,
        task: SubAgentTask,
        audit_writer: WorkflowAuditWriter,
    ) -> dict[str, Any]:
        """构造 workflow child run 覆盖项，输入为任务上下文，输出 request 可展开参数。"""
        runtime = task.runtime
        if runtime is None:
            raise ValueError(f"subagent task {task.task_id!r} has no resolved runtime")
        parent_runtime = self._runtime
        enabled_tools: tuple[Tool, ...] = ()
        if task.requested_tool_names is not None:
            if parent_runtime is None:
                raise RuntimeError("subagent runtime is required to resolve workflow tools")
            enabled_tools = tuple(_resolve_enabled_tools(parent_runtime.tools, task))
        lifecycle_hooks: tuple[LifecycleHook, ...] = ()
        scope_allowed_tool_names: tuple[str, ...] | None = None
        if task.permission is not None:
            if parent_runtime is None:
                raise RuntimeError("subagent runtime is required for scoped workflow task")
            validate_scoped_tool_names(task.requested_tool_names or ())
            if task.requested_tool_names is None:
                parent_tools = parent_runtime.enabled_tools_snapshot
                if parent_tools is None:
                    raise RuntimeError(
                        "parent runtime enabled tool snapshot is unavailable "
                        "for scoped workflow task"
                    )
                enabled_tools = tuple(
                    clip_child_tool_snapshot(
                        parent_tools=parent_tools,
                        requested_tool_names=None,
                        scope_allowed_tool_names=ALLOWED_SCOPED_FILE_TOOLS,
                    )
                )
            grant = _create_workflow_spawn_grant(
                workflow_id=workflow_id,
                parent_session_id=parent_session_id,
                child_session_id=child_session_id,
                task=task,
                enabled_tools=enabled_tools,
            )
            creation_record = _create_workflow_spawn_creation_record(
                grant=grant,
                task=task,
                enabled_tools=enabled_tools,
                audit_writer=audit_writer,
                child_session_log_path=_child_session_log_path(parent_runtime, child_session_id),
            )
            audit_writer.write_subagent_creation(creation_record)
            enabled_tools = tuple(wrap_scoped_file_tools(list(enabled_tools), grant))
            scope_allowed_tool_names = tuple(sorted(ALLOWED_SCOPED_FILE_TOOLS))
            lifecycle_hooks = (SubAgentToolAuditHook(grant=grant, audit_writer=audit_writer),)
        return {
            "enabled_tools": enabled_tools,
            "scope_allowed_tool_names": scope_allowed_tool_names,
            "lifecycle_hooks": lifecycle_hooks,
            "max_tokens": runtime.max_tokens,
            "temperature": runtime.temperature,
            "timeout_seconds": runtime.timeout_seconds,
            "llm_request_metadata": {"resolved_runtime": runtime.to_payload()},
        }

    def _resolve_default_runtime(
        self,
        *,
        parent_agent: Mapping[str, object] | None,
    ) -> ResolvedSubAgentRuntime:
        """解析默认子 agent runtime，输入为父 agent 快照，输出无 role 的运行参数。"""
        return self._runtime_resolver.resolve(
            agent_role_id=None,
            task_metadata={},
            parent_agent=parent_agent,
        )

    def _resolve_task_runtime(
        self,
        task: SubAgentTask,
        *,
        parent_agent: Mapping[str, object] | None,
    ) -> SubAgentTask:
        """给任务绑定已解析 runtime，输入为任务和父 agent 快照，输出任务副本。"""
        if task.runtime is not None:
            return task
        runtime = self._runtime_resolver.resolve(
            agent_role_id=task.agent_role_id,
            task_metadata=task.metadata,
            parent_agent=parent_agent,
        )
        return replace(task, runtime=runtime)

    def _update_subagent_record_with_usage(self, task: SubAgentTask, run: SubAgentRun) -> None:
        """回写子 agent 创建记录，输入为任务和运行结果，输出为 subagent.json usage 更新。"""
        task_run_dir = task.metadata.get("task_run_dir")
        if not isinstance(task_run_dir, str) or not task_run_dir.strip():
            return
        path = Path(task_run_dir) / "subagent.json"
        if not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        payload["usage"] = dict(run.usage)
        payload["completed_run_id"] = run.run_id
        payload["completed_status"] = run.status
        payload["completed_turn_count"] = run.turn_count
        self._write_json(path, payload)

    def _with_agent_workdir(
        self,
        task: SubAgentTask,
        task_run_dir: Path,
        *,
        task_run_id: str,
    ) -> SubAgentTask:
        """绑定子 agent 工作目录，输入为任务和运行目录，输出为带 metadata 的任务副本。"""
        workdir = task_run_dir / "work" if task.permission is not None else task_run_dir
        task_run_dir.mkdir(parents=True, exist_ok=True)
        workdir.mkdir(parents=True, exist_ok=True)
        metadata = dict(task.metadata)
        metadata["working_dir"] = str(workdir)
        metadata["task_run_dir"] = str(task_run_dir)
        metadata["task_run_id"] = task_run_id
        return SubAgentTask(
            task_id=task.task_id,
            task_name=task.task_name,
            prompt=task.prompt,
            context=task.context,
            tool_names=task.tool_names,
            requested_tool_names=task.requested_tool_names,
            skill_names=task.skill_names,
            agent_role_id=task.agent_role_id,
            permission=task.permission,
            runtime=task.runtime,
            metadata=metadata,
        )

    def _workflow_dir(self, *, parent_session_id: str, workflow_id: str) -> Path:
        """计算 workflow 目录，输入为父会话和 workflow ID，输出为 session 同目录审计路径。"""
        sessions_root = resolve_kongming_path(self._config.session.file_store_path).resolve()
        workflow_dir = (
            sessions_root / parent_session_id / "agent-workflows" / workflow_id
        ).resolve()
        if not _is_relative_to(workflow_dir, sessions_root):
            raise ValueError(
                "agent workflow directory must stay inside session root: "
                f"{workflow_dir} is outside {sessions_root}"
            )
        return workflow_dir

    def _write_workflow_manifest(
        self,
        workflow_dir: Path,
        *,
        workflow_id: str,
        mode: WorkflowMode,
        parent_session_id: str,
        started_at: str,
        desc: str | None,
        tasks: list[SubAgentTask],
        status: str,
        finished_at: str | None = None,
        resolved_runtime: dict[str, object] | None = None,
    ) -> None:
        """写入 workflow 清单，输入为任务和状态，输出为 workflow.json。"""
        payload: dict[str, object] = {
            "workflow_id": workflow_id,
            "mode": mode,
            "parent_session_id": parent_session_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "desc": desc,
            "status": status,
            "resolved_runtime": resolved_runtime or _workflow_runtime_payload(tasks),
            "assigned_agents": [_task_payload(task) for task in tasks],
        }
        self._write_json(workflow_dir / "workflow.json", payload)

    def _write_subagent_report(
        self,
        workflow_dir: Path,
        *,
        workflow_id: str,
        run: SubAgentRun,
        display_order: int,
    ) -> SubAgentReportProjection:
        """写入子 agent 报告，输入为单个 run，输出为报告摘要和报告文件。"""
        reported_at = _now_iso()
        content = run.content.strip() or (run.error_message or "").strip()
        digest = _content_digest(content)
        working_dir = _optional_string(run.task.metadata.get("working_dir"))
        detail = SubAgentReportDetail(
            task_id=run.task.task_id,
            task_name=run.task.task_name,
            session_id=run.session_id,
            run_id=run.run_id,
            status=run.status,
            summary=_summary(content),
            content=content,
            error_message=run.error_message,
            working_dir=working_dir,
            content_digest=digest,
            reported_at=reported_at,
            resolved_runtime=resolved_runtime_payload(run.task.runtime) or {},
            usage=dict(run.usage),
        )
        task_run_id = _task_run_id(display_order, run.task.task_id)
        report_path = workflow_dir / "reports" / f"{task_run_id}.json"
        self._write_json(report_path, _report_detail_payload(detail))
        projection = SubAgentReportProjection(
            display_order=display_order,
            task_id=detail.task_id,
            task_name=detail.task_name,
            status=detail.status,
            summary=detail.summary,
            error_message=detail.error_message,
            report_path=str(report_path),
            working_dir=detail.working_dir,
            session_id=detail.session_id,
            run_id=detail.run_id,
            reported_at=detail.reported_at,
            usage=dict(detail.usage),
        )
        self._append_audit(
            workflow_dir,
            action="subagent_reported",
            payload={
                "workflow_id": workflow_id,
                "task_id": detail.task_id,
                "task_run_id": task_run_id,
                "session_id": detail.session_id,
                "run_id": detail.run_id,
                "status": detail.status,
                "reported_at": detail.reported_at,
                "report_path": str(report_path),
                "content_digest": detail.content_digest,
                "error_message": detail.error_message,
                "resolved_runtime": detail.resolved_runtime,
                "usage": dict(detail.usage),
            },
        )
        return projection

    def _write_report_index(
        self,
        workflow_dir: Path,
        *,
        workflow_id: str,
        mode: WorkflowMode,
        parent_session_id: str,
        status: str,
        reports: tuple[SubAgentReportProjection, ...],
        desc: str | None,
    ) -> Path:
        """写入报告索引，输入为报告摘要列表，输出为 reports/index.json 路径。"""
        reports_dir = workflow_dir / "reports"
        index_path = reports_dir / "index.json"
        self._write_json(
            index_path,
            {
                "workflow_id": workflow_id,
                "parent_session_id": parent_session_id,
                "mode": mode,
                "desc": desc,
                "status": status,
                "reports_dir": str(reports_dir),
                "reports": [_report_projection_payload(report) for report in reports],
            },
        )
        return index_path

    def _append_audit(self, workflow_dir: Path, *, action: str, payload: dict[str, object]) -> None:
        """追加 workflow 审计事件，输入为 action 和 payload，输出为 audit.jsonl 新记录。"""
        payload = _audit_payload_with_runtime(payload)
        record = {
            "ts": _now_iso(),
            "action": action,
            "payload": payload,
        }
        workflow_dir.mkdir(parents=True, exist_ok=True)
        with open(workflow_dir / "audit.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def append_audit(
        self,
        *,
        context: WorkflowExecutionContext,
        action: str,
        payload: Mapping[str, object],
    ) -> None:
        """公开 workflow 审计入口（WorkflowRuntime 协议），引擎层自动补齐 resolved_runtime。

        strategy 不需要关心运行时配置快照，resolved_runtime 由本方法从
        context.audit_writer 持有的快照统一注入，保持与历史审计 payload 一致。
        """
        runtime_payload = getattr(context.audit_writer, "resolved_runtime_payload", None)
        self._append_audit(
            context.workflow_dir,
            action=action,
            payload=_audit_payload_with_runtime(dict(payload), resolved_runtime=runtime_payload),
        )

    def record_assigned_task_progress(
        self,
        *,
        context: WorkflowExecutionContext,
        tasks: list[SubAgentTask],
    ) -> None:
        """保留协议入口，任务骨架已由 running manifest 阶段一次性创建。"""
        del context, tasks

    def initialize_task_flow_progress(
        self,
        *,
        context: WorkflowExecutionContext,
        tasks: list[SubAgentTask],
        title: str,
    ) -> None:
        """初始化 task-flow 的 LLM 步骤快照，输入为计划任务和标题，输出为前台状态替换。"""
        self._open_task_progress(
            context=context,
            tasks=tasks,
            title=title,
            control_mode=TaskProgressControlMode.LLM_STEPS,
            use_task_flow_dependencies=True,
        )

    def _open_runtime_task_progress(
        self,
        *,
        context: WorkflowExecutionContext,
        tasks: list[SubAgentTask],
    ) -> None:
        """初始化 runtime workflow 快照，输入为上下文和已分配任务，输出为前台状态替换。"""
        self._open_task_progress(
            context=context,
            tasks=tasks,
            title=context.desc or f"{context.mode} 工作流",
            control_mode=TaskProgressControlMode.RUNTIME_LIFECYCLE,
            use_task_flow_dependencies=False,
        )

    def _open_task_progress(
        self,
        *,
        context: WorkflowExecutionContext,
        tasks: list[SubAgentTask],
        title: str,
        control_mode: TaskProgressControlMode,
        use_task_flow_dependencies: bool,
    ) -> None:
        """一次性创建 workflow 任务骨架，输入为上下文与控制模式，输出为 Manager 落盘结果。"""
        if not tasks:
            return
        definitions = [
            TaskProgressTaskDefinition(
                task_id=task.task_id,
                task_run_id=_metadata_task_run_id(task),
                desc=task.task_name,
                depends_on=(_task_flow_dependencies(task) if use_task_flow_dependencies else ()),
                display_order=index,
            )
            for index, task in enumerate(tasks)
        ]
        try:
            self._task_progress_manager.open_workflow(
                session_id=context.parent_session_id,
                workflow_id=context.workflow_id,
                title=title,
                control_mode=control_mode,
                tasks=definitions,
            )
        except Exception as exc:
            self._record_task_progress_failure(
                parent_session_id=context.parent_session_id,
                workflow_id=context.workflow_id,
                error=exc,
            )

    def _record_single_task_progress(
        self,
        *,
        parent_session_id: str,
        workflow_id: str,
        task: SubAgentTask,
        runtime_status: RuntimeTaskProgressStatus,
        error_message: str | None = None,
    ) -> None:
        """记录单个 runtime 生命周期事实，输入为任务与枚举状态，输出为状态机迁移。"""
        try:
            self._task_progress_manager.record_runtime_transition(
                session_id=parent_session_id,
                workflow_id=workflow_id,
                task_id=task.task_id,
                runtime_status=runtime_status,
                error_message=error_message,
            )
        except Exception as exc:
            self._record_task_progress_failure(
                parent_session_id=parent_session_id,
                workflow_id=workflow_id,
                error=exc,
            )

    def _record_completed_workflow_progress(
        self,
        *,
        parent_session_id: str,
        workflow_id: str,
        runs: tuple[SubAgentRun, ...],
    ) -> None:
        """记录 workflow 最终进度，输入为子运行结果，输出为最终快照同步。"""
        for run in runs:
            runtime_status = (
                RuntimeTaskProgressStatus.COMPLETED
                if run.status == "completed"
                else RuntimeTaskProgressStatus.CANCELLED
                if run.status == "cancelled"
                else RuntimeTaskProgressStatus.FAILED
            )
            self._record_single_task_progress(
                parent_session_id=parent_session_id,
                workflow_id=workflow_id,
                task=run.task,
                runtime_status=runtime_status,
                error_message=run.error_message,
            )

    def _record_task_progress_failure(
        self,
        *,
        parent_session_id: str,
        workflow_id: str,
        error: Exception,
    ) -> None:
        """写入进度同步失败审计，输入为 workflow 坐标和异常，输出为审计记录。"""
        try:
            self._append_audit(
                self._workflow_dir(
                    parent_session_id=parent_session_id,
                    workflow_id=workflow_id,
                ),
                action="task_progress_sync_failed",
                payload={
                    "workflow_id": workflow_id,
                    "parent_session_id": parent_session_id,
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                },
            )
        except Exception:
            return

    def _write_json(self, path: Path, payload: dict[str, object]) -> None:
        """原子写入 JSON 文件，输入为目标路径和 payload，输出为目标文件更新。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)


def _task_payload(task: SubAgentTask) -> dict[str, object]:
    """序列化任务信息，输入为 SubAgentTask，输出为审计和 manifest 可写入的字典。"""
    payload: dict[str, object] = {
        "task_id": task.task_id,
        "task_name": task.task_name,
        "tool_names": list(task.tool_names),
        "requested_tool_names": (
            list(task.requested_tool_names) if task.requested_tool_names is not None else None
        ),
        "skill_names": list(task.skill_names),
        "agent_role_id": task.agent_role_id,
        "permission": to_jsonable(task.permission) if task.permission is not None else None,
        "task_run_id": task.metadata.get("task_run_id"),
        "task_run_dir": task.metadata.get("task_run_dir"),
        "working_dir": task.metadata.get("working_dir"),
    }
    runtime = resolved_runtime_payload(task.runtime)
    if runtime is not None:
        payload["resolved_runtime"] = runtime
    return payload


def _read_json_dict(path: Path) -> dict[str, object]:
    """读取 JSON 对象，输入为路径，输出为 dict；文件缺失或非对象时返回空字典。"""
    if not path.is_file():
        return {}
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items()}


def _tasks_from_workflow_manifest(manifest: Mapping[str, object]) -> list[SubAgentTask]:
    """从 workflow manifest 恢复任务，输入为 manifest，输出可用于取消收口的任务列表。"""
    raw_agents = manifest.get("assigned_agents")
    if not isinstance(raw_agents, list):
        return []
    tasks: list[SubAgentTask] = []
    for index, raw in enumerate(raw_agents, 1):
        if not isinstance(raw, Mapping):
            continue
        task_id = _optional_string(raw.get("task_id")) or f"agent-{index}"
        task_name = _optional_string(raw.get("task_name")) or task_id
        metadata: dict[str, object] = {}
        for key in ("task_run_id", "task_run_dir", "working_dir"):
            value = _optional_string(raw.get(key))
            if value is not None:
                metadata[key] = value
        tasks.append(
            SubAgentTask(
                task_id=task_id,
                task_name=task_name,
                prompt="",
                tool_names=_string_tuple(raw.get("tool_names")),
                requested_tool_names=(
                    _string_tuple(raw.get("requested_tool_names"))
                    if "requested_tool_names" in raw and raw.get("requested_tool_names") is not None
                    else None
                ),
                skill_names=_string_tuple(raw.get("skill_names")),
                agent_role_id=_optional_string(raw.get("agent_role_id")),
                metadata=metadata,
            )
        )
    return tasks


def _string_tuple(value: object) -> tuple[str, ...]:
    """读取字符串列表，输入为任意值，输出去空白后的字符串元组。"""
    if not isinstance(value, list | tuple):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _mapping_or_none(value: object) -> dict[str, object] | None:
    """读取映射值，输入为任意值，输出 dict 或 None。"""
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _workflow_runtime_payload(tasks: list[SubAgentTask]) -> dict[str, object] | None:
    """提取 workflow runtime 摘要，输入为任务列表，输出为首个任务的 runtime payload。"""
    if not tasks:
        return None
    return resolved_runtime_payload(tasks[0].runtime)


def _audit_payload_with_runtime(
    payload: Mapping[str, object],
    resolved_runtime: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """补齐审计 runtime 字段，输入为原始 payload 和可选 runtime，输出为审计 payload。"""
    normalized = dict(payload)
    if "resolved_runtime" in normalized:
        return normalized
    runtime = resolved_runtime or _runtime_payload_from_audit_payload(normalized)
    if runtime is not None:
        normalized["resolved_runtime"] = dict(runtime)
    return normalized


def _runtime_payload_from_audit_payload(
    payload: Mapping[str, object],
) -> dict[str, object] | None:
    """从审计 payload 推断 runtime，输入为 payload，输出为 runtime 摘要或 None。"""
    for key in ("resolved_runtime",):
        raw = payload.get(key)
        if isinstance(raw, Mapping):
            return dict(raw)
    return None


def _normalize_workflow_desc(value: object) -> str | None:
    """归一化 workflow 短描述，输入为任意值，输出为裁剪后的字符串或 None。"""
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    return normalized[:_WORKFLOW_DESC_MAX_CHARS]


def _run_payload(run: SubAgentRun) -> dict[str, object]:
    """序列化子 agent 运行结果，输入为 SubAgentRun，输出为 result/audit 可写入的字典。"""
    return {
        "task_id": run.task.task_id,
        "task_name": run.task.task_name,
        "session_id": run.session_id,
        "run_id": run.run_id,
        "status": run.status,
        "content": run.content,
        "error_message": run.error_message,
        "turn_count": run.turn_count,
        "usage": dict(run.usage),
        "resolved_runtime": resolved_runtime_payload(run.task.runtime),
        "task_run_id": run.task.metadata.get("task_run_id"),
        "task_run_dir": run.task.metadata.get("task_run_dir"),
        "working_dir": run.task.metadata.get("working_dir"),
    }


async def _wait_for_child_result_mail(
    mailbox: asyncio.Queue[Any],
    *,
    task_id: str,
    timeout_seconds: float,
) -> Any:
    """等待匹配子 agent 结果，输入为父 mailbox 和 spawn task_id，输出匹配 Mail。"""
    buffered: list[Any] = []
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for child_result task_id={task_id}")
            mail = await asyncio.wait_for(mailbox.get(), timeout=remaining)
            if (
                getattr(mail, "kind", None) == "child_result"
                and getattr(mail, "task_id", None) == task_id
            ):
                return mail
            buffered.append(mail)
    finally:
        for mail in buffered:
            mailbox.put_nowait(mail)


def _subagent_run_from_child_mail(
    *,
    task: SubAgentTask,
    session_id: str,
    run_id: str,
    mail: Any,
) -> SubAgentRun:
    """把 child_result Mail 转成旧 SubAgentRun，输入为 Mail，输出 workflow 报告结构。"""
    payload = getattr(mail, "payload", None)
    content = getattr(payload, "content", None)
    metadata = getattr(payload, "metadata", {})
    text = content if isinstance(content, str) else ""
    child_error = _optional_metadata_string(metadata, "child_error_reason")
    child_cancel = _optional_metadata_string(metadata, "child_cancel_reason")
    if child_error is not None:
        status = "failed"
        error_message = child_error
    elif child_cancel is not None:
        status = "cancelled"
        error_message = child_cancel
    else:
        status = "completed"
        error_message = None
    return SubAgentRun(
        task=task,
        session_id=session_id,
        run_id=run_id,
        status=status,
        content=text,
        error_message=error_message,
        turn_count=_metadata_non_negative_int(metadata, "turn_count"),
        usage=_usage_from_message_metadata(metadata),
    )


def _workflow_task_cwd(task: SubAgentTask, *, fallback: Path) -> str:
    """解析 workflow 子 agent cwd，输入为任务和兜底目录，输出可传入 spawn request 的路径。"""
    for key in ("working_dir", "task_run_dir"):
        value = task.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(fallback)


def _workflow_task_timeout_seconds(task: SubAgentTask, *, fallback: float) -> float:
    """解析 workflow 子 agent 超时，输入为任务 runtime 和兜底秒数，输出正数秒。"""
    if task.runtime is not None and task.runtime.timeout_seconds > 0:
        return float(task.runtime.timeout_seconds)
    if fallback > 0:
        return float(fallback)
    return 60.0


def _resolve_enabled_tools(tool_lookup: Any, task: SubAgentTask) -> list[Any]:
    """解析 workflow 子 agent 工具，输入为工具查找面和任务，输出工具实例列表。"""
    resolved: list[Any] = []
    for name in task.requested_tool_names or ():
        if name not in tool_lookup:
            raise ValueError(f"subagent task {task.task_id!r} requested unknown tool {name!r}")
        resolved.append(tool_lookup[name])
    return resolved


def _create_workflow_spawn_grant(
    *,
    workflow_id: str,
    parent_session_id: str,
    child_session_id: str,
    task: SubAgentTask,
    enabled_tools: tuple[Tool, ...],
) -> SubAgentGrant:
    """创建 workflow spawn 授权单，输入为任务上下文，输出 SubAgentGrant。"""
    task_run_id = _metadata_task_run_id(task)
    task_run_dir = _metadata_path(task, "task_run_dir")
    working_dir = _metadata_path(task, "working_dir")
    if task_run_dir is None or working_dir is None:
        raise ValueError("scoped subagent task requires task_run_dir and working_dir")
    created_at = _now_iso()
    return SubAgentGrant(
        grant_id=f"grant-{workflow_id}-{task_run_id}",
        workflow_id=workflow_id,
        parent_session_id=parent_session_id,
        task_id=task.task_id,
        task_run_id=task_run_id,
        task_name=task.task_name,
        session_id=child_session_id,
        task_run_dir=task_run_dir,
        working_dir=working_dir,
        workflow_dir=task_run_dir.parent.parent,
        allowed_tools=frozenset(tool.name for tool in enabled_tools),
        allowed_skills=frozenset(task.skill_names),
        created_at=created_at,
    )


def _create_workflow_spawn_creation_record(
    *,
    grant: SubAgentGrant,
    task: SubAgentTask,
    enabled_tools: tuple[Tool, ...],
    audit_writer: WorkflowAuditWriter,
    child_session_log_path: Path,
) -> SubAgentCreationRecord:
    """创建子 agent 审计记录，输入为 grant/task，输出 SubAgentCreationRecord。"""
    if task.permission is None:
        raise ValueError("scoped subagent task requires permission")
    return SubAgentCreationRecord(
        version=1,
        workflow_id=grant.workflow_id,
        task_run_id=grant.task_run_id,
        session_id=grant.session_id,
        task_id=task.task_id,
        task_name=task.task_name,
        prompt=task.prompt,
        context=task.context,
        tool_names=tuple(tool.name for tool in enabled_tools),
        skill_names=task.skill_names,
        resolved_runtime=resolved_runtime_payload(task.runtime) or {},
        permission=task.permission,
        grant=grant,
        task_run_dir=grant.task_run_dir,
        working_dir=grant.working_dir,
        child_session_log_path=child_session_log_path,
        workflow_audit_log_path=audit_writer.audit_log_path,
        created_at=grant.created_at,
    )


def _child_session_log_path(runtime: Any, session_id: str) -> Path:
    """生成子 session 日志路径，输入为 runtime 和 session_id，输出 jsonl 路径。"""
    root = resolve_kongming_path(runtime.config.session.file_store_path)
    return root / session_id / f"{session_id}.jsonl"


def _metadata_path(task: SubAgentTask, key: str) -> Path | None:
    """读取任务 metadata 路径，输入为任务和 key，输出 Path 或 None。"""
    value = task.metadata.get(key)
    if isinstance(value, str) and value.strip():
        return Path(value).resolve()
    return None


def _parent_task_id_from_snapshot(parent_agent: Mapping[str, object] | None) -> str | None:
    """读取父 TaskRegistry task id，输入为父 agent 快照，输出可选 task id。"""
    if parent_agent is None:
        return None
    for key in ("task_id", "mail_task_id", "parent_task_id"):
        value = parent_agent.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _optional_metadata_string(metadata: object, key: str) -> str | None:
    """读取 metadata 字符串，输入为任意 metadata 和 key，输出字符串或 None。"""
    if isinstance(metadata, Mapping):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _metadata_non_negative_int(metadata: object, key: str) -> int:
    """读取 metadata 非负整数，输入为任意 metadata 和 key，缺失时输出 0。"""
    if isinstance(metadata, Mapping):
        value = metadata.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def _usage_from_message_metadata(metadata: object) -> dict[str, int]:
    """读取 canonical message usage，输入为任意 metadata，输出已知指标。"""
    if not isinstance(metadata, Mapping):
        return {}
    usage = metadata.get("usage")
    if not isinstance(usage, Mapping):
        return {}
    try:
        snapshot = ProviderUsageSnapshot.from_payload(usage)
    except ValueError:
        return {}
    return {
        metric_name.value: metric.value
        for metric_name, metric in snapshot.metric_items()
        if metric.value is not None
    }


def _report_detail_payload(report: SubAgentReportDetail) -> dict[str, object]:
    """序列化报告明细，输入为 SubAgentReportDetail，输出为报告 JSON payload。"""
    return {
        "task_id": report.task_id,
        "task_name": report.task_name,
        "session_id": report.session_id,
        "run_id": report.run_id,
        "status": report.status,
        "summary": report.summary,
        "content": report.content,
        "error_message": report.error_message,
        "working_dir": report.working_dir,
        "content_digest": report.content_digest,
        "reported_at": report.reported_at,
        "resolved_runtime": dict(report.resolved_runtime),
        "usage": dict(report.usage),
    }


def _report_projection_payload(report: SubAgentReportProjection) -> dict[str, object]:
    """序列化报告摘要，输入为 SubAgentReportProjection，输出为索引 JSON payload。"""
    return {
        "display_order": report.display_order,
        "task_id": report.task_id,
        "task_name": report.task_name,
        "status": report.status,
        "summary": report.summary,
        "error_message": report.error_message,
        "report_path": report.report_path,
        "working_dir": report.working_dir,
        "session_id": report.session_id,
        "run_id": report.run_id,
        "reported_at": report.reported_at,
        "usage": dict(report.usage),
    }


def _build_task_progress_manager(config: Config) -> SessionTaskProgressManager:
    """构造任务进度 Manager，输入为配置，输出为可写入 workflow 进度的 Manager。"""
    return SessionTaskProgressManager.from_config(config)


def _failed_run(
    *,
    workflow_id: str,
    parent_session_id: str,
    task: SubAgentTask,
    error: Exception,
) -> SubAgentRun:
    """构造失败运行记录，输入为任务和异常，输出为 failed 状态的 SubAgentRun。"""
    session_id = _build_child_session_id(
        workflow_id=workflow_id,
        parent_session_id=parent_session_id,
        task_id=_metadata_task_run_id(task),
    )
    return SubAgentRun(
        task=task,
        session_id=session_id,
        run_id=f"run-{session_id}-failed",
        status="failed",
        content="",
        error_message=str(error),
        turn_count=0,
    )


def _cancelled_run(
    *,
    workflow_id: str,
    parent_session_id: str,
    task: SubAgentTask,
) -> SubAgentRun:
    """构造取消运行记录，输入为任务和 workflow 上下文，输出 cancelled 状态的 SubAgentRun。"""
    session_id = _build_child_session_id(
        workflow_id=workflow_id,
        parent_session_id=parent_session_id,
        task_id=_metadata_task_run_id(task),
    )
    return SubAgentRun(
        task=task,
        session_id=session_id,
        run_id=f"run-{session_id}-cancelled",
        status="cancelled",
        content="",
        error_message="user_interrupt",
        turn_count=0,
    )


def _subagent_run_from_result_payload(
    task: SubAgentTask,
    payload: Mapping[str, object],
) -> SubAgentRun:
    """从 agent result.json 恢复运行记录，输入为任务和 payload，输出 SubAgentRun。"""
    status = _optional_string(payload.get("status")) or "cancelled"
    if status not in {"completed", "failed", "cancelled"}:
        status = "cancelled"
    turn_count = payload.get("turn_count")
    return SubAgentRun(
        task=task,
        session_id=_optional_string(payload.get("session_id")) or "unknown-session",
        run_id=_optional_string(payload.get("run_id")) or f"run-{_metadata_task_run_id(task)}",
        status=status,
        content=_optional_string(payload.get("content")) or "",
        error_message=_optional_string(payload.get("error_message")),
        turn_count=turn_count if isinstance(turn_count, int) and turn_count >= 0 else 0,
        usage=_usage_from_message_metadata(payload),
    )


def _content_digest(content: str) -> str:
    """计算内容摘要，输入为文本内容，输出为 sha256 摘要字符串。"""
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def _summary(content: str, *, max_chars: int = 500) -> str:
    """生成报告摘要，输入为内容和最大长度，输出为单行截断摘要。"""
    summary = " ".join(content.strip().split())
    if len(summary) <= max_chars:
        return summary
    return summary[: max_chars - 1] + "…"


def _optional_string(value: object) -> str | None:
    """读取可选字符串，输入为任意值，输出为字符串或 None。"""
    if isinstance(value, str):
        return value
    return None


def _agent_result_path(workflow_dir: Path, task: SubAgentTask) -> Path:
    """计算子 agent 结果路径，输入为 workflow 目录和任务，输出为 result.json 路径。"""
    task_run_dir = task.metadata.get("task_run_dir")
    if isinstance(task_run_dir, str) and task_run_dir.strip():
        return Path(task_run_dir) / "result.json"
    working_dir = task.metadata.get("working_dir")
    if isinstance(working_dir, str) and working_dir.strip():
        return Path(working_dir) / "result.json"
    return workflow_dir / "agents" / f"{_metadata_task_run_id(task)}" / "result.json"


def _now_iso() -> str:
    """生成当前时间，输入为空，输出为 UTC ISO 8601 字符串。"""
    return datetime.now(UTC).isoformat()


def _slug(value: str) -> str:
    """生成安全路径片段，输入为原始文本，输出为小写 slug。"""
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value.strip())
    safe = safe.strip("-_").lower()
    return safe or "task"


def _task_run_id(display_order: int, task_id: str) -> str:
    """生成任务运行 ID，输入为展示顺序和任务 ID，输出为带序号的稳定 ID。"""
    return f"{display_order:03d}-{_slug(task_id)}"


def _metadata_task_run_id(task: SubAgentTask) -> str:
    """读取任务运行 ID，输入为任务 metadata，输出为显式 task_run_id 或 slug 后的任务 ID。"""
    raw = task.metadata.get("task_run_id")
    if isinstance(raw, str) and raw.strip():
        return raw
    return _slug(task.task_id)


def _task_flow_dependencies(task: SubAgentTask) -> tuple[str, ...]:
    """读取 task-flow 依赖，输入为计划任务 metadata，输出为去重后的步骤 ID 元组。"""
    raw = task.metadata.get("task_flow_depends_on")
    if not isinstance(raw, list):
        return ()
    dependencies: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            continue
        dependency = value.strip()
        if dependency not in dependencies:
            dependencies.append(dependency)
    return tuple(dependencies)


def _build_child_session_id(
    *,
    workflow_id: str,
    parent_session_id: str,
    task_id: str,
) -> str:
    """生成子 agent 会话 ID，输入为 workflow、父会话和任务 ID，输出为隔离 session ID。"""
    parent = _slug(parent_session_id)[:32]
    workflow = _slug(workflow_id)[:32]
    task = _slug(task_id)[:32]
    return f"subagent-{parent}-{workflow}-{task}"


def _is_relative_to(path: Path, root: Path) -> bool:
    """判断路径归属，输入为候选路径和根目录，输出为是否位于根目录内。"""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


__all__ = [
    "AgentWorkflowManager",
    "AgentWorkflowResult",
    "SubAgentReportDetail",
    "SubAgentReportProjection",
    "WorkflowMode",
]
