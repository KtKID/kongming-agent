"""智能体工作流管理器。

本脚本负责多 agent 编排的 facade、并行子任务执行、子 agent 审计文件写入和结果收口。
作用是把父 agent 选择的 workflow strategy 转换为可执行的子 agent 任务，并把运行结果物化为 workflow.json、audit.jsonl、result.json 和 reports/index.json。
关键执行流程：注册 workflow strategy，接收 mode 和 task_specs，创建 workflow 目录，分配子 agent 工作目录，并发运行子任务，写入审计与报告后返回 AgentWorkflowResult。
关键函数：run_workflow 负责策略注册表分发，run_parallel 执行并行编排，run_parallel_specs 把公开 task_specs 转成 SubAgentTask，_run_one 收口单个子 agent 结果。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from application.agent_workflows.context import WorkflowExecutionContext
from application.agent_workflows.strategies.base import WorkflowRunRequest
from application.agent_workflows.strategies.description import (
    WorkflowStrategyCatalogEntry,
    WorkflowStrategyDescription,
)
from application.agent_workflows.strategies.manager import (
    AgentWorkflowStrategyManager,
)
from application.agent_workflows.strategies.map_reduce.strategy import MapReduceStrategy
from application.agent_workflows.strategies.parallel import ParallelWorkflowStrategy
from application.subagents.manager import SubAgentManager, SubAgentRun, SubAgentTask
from application.subagents.permissions import (
    SubAgentCreationRecord,
    WorkflowAuditWriter,
    parse_permission_spec,
    to_jsonable,
    validate_scoped_tool_names,
)
from infrastructure.config.models import Config

WorkflowMode = str


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


@dataclass(frozen=True)
class SubAgentReportProjection:
    """返回给父 agent 和 Web 视图使用的子 agent 报告摘要。"""

    display_order: int
    task_id: str
    task_name: str
    status: str
    summary: str
    error_message: str | None
    report_path: str
    working_dir: str | None
    session_id: str
    run_id: str
    reported_at: str


@dataclass(frozen=True)
class AgentWorkflowResult:
    """返回给调用方的 workflow 最终结果。"""

    workflow_id: str
    mode: WorkflowMode
    parent_session_id: str
    workflow_dir: Path
    started_at: str
    finished_at: str
    runs: tuple[SubAgentRun, ...]
    reports: tuple[SubAgentReportProjection, ...]
    report_index_path: Path
    data: Mapping[str, object] | None = None
    completed_override: bool | None = None

    @property
    def completed(self) -> bool:
        """判断 workflow 是否全部完成，输入为当前 runs，输出为布尔完成状态。"""
        if self.completed_override is not None:
            return self.completed_override
        return all(run.status == "completed" for run in self.runs)


@dataclass(frozen=True)
class _RunOutcome:
    """单个子 agent 运行结果和报告摘要的内部聚合结构。"""

    run: SubAgentRun
    report: SubAgentReportProjection


class AgentWorkflowAuditWriter:
    """写入 AgentWorkflowManager 管辖的 workflow 审计文件。"""

    def __init__(self, workflow_dir: Path) -> None:
        """初始化审计写入器，输入为 workflow 目录，输出为绑定目录的 writer。"""
        self._workflow_dir = workflow_dir

    @property
    def audit_log_path(self) -> Path:
        """返回审计日志路径，输入为 workflow 目录，输出为 audit.jsonl 路径。"""
        return self._workflow_dir / "audit.jsonl"

    def write_event(self, event: Mapping[str, Any]) -> None:
        """写入审计事件，输入为事件映射，输出为追加到 audit.jsonl 的一行 JSON。"""
        action = event.get("action")
        if not isinstance(action, str) or not action:
            raise ValueError("workflow audit event requires non-empty action")
        payload_raw = event.get("payload", {})
        payload = payload_raw if isinstance(payload_raw, dict) else {"value": payload_raw}
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


class AgentWorkflowManager:
    """协调子 agent 执行并持有 workflow 审计产物。"""

    def __init__(
        self,
        *,
        subagents: SubAgentManager,
        config: Config,
        workspace_root: Path,
    ) -> None:
        """初始化管理器，输入为子 agent 管理器、配置和工作区，输出为带默认策略注册的 workflow facade。"""
        self._subagents = subagents
        self._config = config
        self._workspace_root = workspace_root.resolve()
        self._strategy_manager = AgentWorkflowStrategyManager(
            context_factory=self._build_workflow_context
        )
        self._strategy_manager.register(ParallelWorkflowStrategy(self))
        self._strategy_manager.register(MapReduceStrategy(self))

    @property
    def workspace_root(self) -> Path:
        """返回 workflow 运行的工作区根目录，输入为 manager 状态，输出为绝对路径。"""
        return self._workspace_root

    def list_workflow_strategies(self) -> tuple[WorkflowStrategyCatalogEntry, ...]:
        """列出已注册策略，输入为当前策略注册表，输出为父 agent 可查看的策略目录。"""
        return self._strategy_manager.list_strategies()

    def describe_workflow_strategy(self, mode: str) -> WorkflowStrategyDescription:
        """查询策略详情，输入为策略 mode，输出为面向 LLM 的中文策略说明。"""
        return self._strategy_manager.describe_strategy(mode)

    async def run_workflow(self, request: WorkflowRunRequest) -> AgentWorkflowResult:
        """通过策略注册表执行 workflow，输入为运行请求，输出为 AgentWorkflowResult。"""
        result = await self._strategy_manager.run_strategy(request)
        if not isinstance(result, AgentWorkflowResult):
            raise TypeError("agent workflow strategy returned an invalid result")
        return result

    async def run_parallel(
        self,
        *,
        parent_session_id: str,
        tasks: list[SubAgentTask],
    ) -> AgentWorkflowResult:
        """并发执行子 agent 任务，输入为父会话和任务列表，输出为完整 workflow 结果。"""
        if not tasks:
            raise ValueError("parallel workflow requires at least one task")

        workflow_id = f"wf-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        started_at = _now_iso()
        workflow_dir = self._workflow_dir(
            parent_session_id=parent_session_id, workflow_id=workflow_id
        )
        agents_dir = workflow_dir / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        audit_writer = AgentWorkflowAuditWriter(workflow_dir)

        assigned_tasks = [
            self._with_agent_workdir(
                task,
                agents_dir / _task_run_id(index, task.task_id),
                task_run_id=_task_run_id(index, task.task_id),
            )
            for index, task in enumerate(tasks, 1)
        ]
        self._write_workflow_manifest(
            workflow_dir,
            workflow_id=workflow_id,
            mode="parallel",
            parent_session_id=parent_session_id,
            started_at=started_at,
            tasks=assigned_tasks,
            status="running",
        )
        self._append_audit(
            workflow_dir,
            action="workflow_started",
            payload={
                "workflow_id": workflow_id,
                "mode": "parallel",
                "parent_session_id": parent_session_id,
                "task_count": len(assigned_tasks),
            },
        )
        for task in assigned_tasks:
            self._append_audit(
                workflow_dir,
                action="agent_assigned",
                payload=_task_payload(task),
            )

        outcomes = await asyncio.gather(
            *[
                self._run_one(
                    workflow_id=workflow_id,
                    parent_session_id=parent_session_id,
                    workflow_dir=workflow_dir,
                    task=task,
                    display_order=index,
                    audit_writer=audit_writer,
                )
                for index, task in enumerate(assigned_tasks, 1)
            ]
        )
        runs = tuple(outcome.run for outcome in outcomes)
        reports = tuple(outcome.report for outcome in outcomes)
        finished_at = _now_iso()
        completed = all(run.status == "completed" for run in runs)
        report_index_path = self._write_report_index(
            workflow_dir,
            workflow_id=workflow_id,
            mode="parallel",
            parent_session_id=parent_session_id,
            status="completed" if completed else "failed",
            reports=reports,
        )
        result = AgentWorkflowResult(
            workflow_id=workflow_id,
            mode="parallel",
            parent_session_id=parent_session_id,
            workflow_dir=workflow_dir,
            started_at=started_at,
            finished_at=finished_at,
            runs=runs,
            reports=reports,
            report_index_path=report_index_path,
        )
        self._append_audit(
            workflow_dir,
            action="workflow_completed",
            payload={
                "workflow_id": workflow_id,
                "completed": result.completed,
                "finished_at": finished_at,
                "run_count": len(runs),
                "report_index_path": str(report_index_path),
            },
        )
        self._write_workflow_manifest(
            workflow_dir,
            workflow_id=workflow_id,
            mode="parallel",
            parent_session_id=parent_session_id,
            started_at=started_at,
            tasks=assigned_tasks,
            status="completed" if result.completed else "failed",
            finished_at=finished_at,
        )
        self._write_json(
            workflow_dir / "result.json",
            {
                "workflow_id": workflow_id,
                "mode": "parallel",
                "parent_session_id": parent_session_id,
                "workflow_dir": str(workflow_dir),
                "started_at": started_at,
                "finished_at": finished_at,
                "completed": result.completed,
                "report_index_path": str(report_index_path),
                "reports": [_report_projection_payload(report) for report in reports],
                "runs": [_run_payload(run) for run in runs],
            },
        )
        return result

    async def run_parallel_specs(
        self,
        *,
        parent_session_id: str,
        task_specs: list[dict[str, object]],
    ) -> AgentWorkflowResult:
        """解析公开 task_specs，输入为父会话和任务规格，输出为并行 workflow 结果。"""
        if not task_specs:
            raise ValueError("parallel workflow requires at least one task spec")
        if len(task_specs) > 8:
            raise ValueError("parallel workflow supports at most 8 task specs")
        tasks: list[SubAgentTask] = []
        for index, spec in enumerate(task_specs, 1):
            if not isinstance(spec, dict):
                raise ValueError(f"task_specs[{index}] must be an object")
            task_name = spec.get("task_name")
            if not isinstance(task_name, str) or not task_name.strip():
                raise ValueError(f"task_specs[{index}].task_name must be a non-empty string")
            prompt = spec.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError(f"task_specs[{index}].prompt must be a non-empty string")
            context = spec.get("context", "")
            if context is None:
                context = ""
            if not isinstance(context, str):
                raise ValueError(f"task_specs[{index}].context must be a string")
            raw_tool_names = spec.get("tool_names", [])
            if not isinstance(raw_tool_names, list | tuple):
                raw_tool_names = []
            raw_skill_names = spec.get("skill_names", [])
            if not isinstance(raw_skill_names, list | tuple):
                raw_skill_names = []
            permission = None
            if "permission" in spec:
                permission = parse_permission_spec(spec["permission"])
                validate_scoped_tool_names(tuple(str(name) for name in raw_tool_names))
            tasks.append(
                SubAgentTask(
                    task_id=f"agent-{index}",
                    task_name=task_name.strip(),
                    prompt=prompt.strip(),
                    context=context.strip(),
                    tool_names=tuple(str(name) for name in raw_tool_names),
                    skill_names=tuple(str(name) for name in raw_skill_names),
                    permission=permission,
                )
            )
        return await self.run_parallel(parent_session_id=parent_session_id, tasks=tasks)

    async def run_workflow_specs(
        self,
        *,
        mode: str,
        parent_session_id: str,
        task_specs: list[dict[str, object]],
    ) -> AgentWorkflowResult:
        """按 mode 执行 workflow，输入为策略 ID、父会话和任务规格，输出为 workflow 结果。"""
        return await self.run_workflow(
            WorkflowRunRequest(
                mode=mode,
                parent_session_id=parent_session_id,
                payload={"task_specs": task_specs},
                source="run_workflow_specs",
            )
        )

    async def run_workflow_payload(
        self,
        *,
        mode: str,
        parent_session_id: str,
        payload: Mapping[str, object],
    ) -> AgentWorkflowResult:
        """按 mode 执行通用 workflow payload，输入为完整策略 payload，输出为 workflow 结果。"""
        return await self.run_workflow(
            WorkflowRunRequest(
                mode=mode,
                parent_session_id=parent_session_id,
                payload=payload,
                source="run_workflow_payload",
            )
        )

    def prepare_subagent_tasks(
        self,
        *,
        workflow_dir: Path,
        tasks: list[SubAgentTask],
    ) -> list[SubAgentTask]:
        """为策略任务绑定运行目录，输入为 workflow 目录和任务，输出为带 metadata 的任务列表。"""
        agents_dir = workflow_dir / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        return [
            self._with_agent_workdir(
                task,
                agents_dir / _task_run_id(index, task.task_id),
                task_run_id=_task_run_id(index, task.task_id),
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
            task=task,
            display_order=display_order,
            audit_writer=context.audit_writer,
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
        self._write_workflow_manifest(
            context.workflow_dir,
            workflow_id=context.workflow_id,
            mode=context.mode,
            parent_session_id=context.parent_session_id,
            started_at=context.started_at,
            tasks=tasks,
            status=status,
            finished_at=finished_at,
        )

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
            "completed": completed,
            "report_index_path": str(report_index_path),
            "reports": [_report_projection_payload(report) for report in reports],
            "runs": [_run_payload(run) for run in runs],
        }
        if extra:
            payload.update(to_jsonable(extra))
        self._write_json(context.workflow_dir / "result.json", payload)

    def _build_workflow_context(self, request: WorkflowRunRequest) -> WorkflowExecutionContext:
        """创建策略执行上下文，输入为运行请求，输出为 workflow ID、目录和审计 writer。"""
        workflow_id = f"wf-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        started_at = _now_iso()
        workflow_dir = self._workflow_dir(
            parent_session_id=request.parent_session_id,
            workflow_id=workflow_id,
        )
        return WorkflowExecutionContext(
            workflow_id=workflow_id,
            parent_session_id=request.parent_session_id,
            mode=request.mode,
            workflow_dir=workflow_dir,
            started_at=started_at,
            audit_writer=AgentWorkflowAuditWriter(workflow_dir),
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
    ) -> _RunOutcome:
        """运行单个子 agent，输入为任务和审计上下文，输出为子任务运行结果和报告摘要。"""
        started = time.perf_counter()
        try:
            run = await self._subagents.run_task(
                workflow_id=workflow_id,
                parent_session_id=parent_session_id,
                task=task,
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
        self._write_json(_agent_result_path(workflow_dir, task), payload)
        report = self._write_subagent_report(
            workflow_dir,
            workflow_id=workflow_id,
            run=run,
            display_order=display_order,
        )
        return _RunOutcome(run=run, report=report)

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
            skill_names=task.skill_names,
            permission=task.permission,
            metadata=metadata,
        )

    def _workflow_dir(self, *, parent_session_id: str, workflow_id: str) -> Path:
        """计算 workflow 目录，输入为父会话和 workflow ID，输出为工作区内审计目录路径。"""
        sessions_root = Path(self._config.session.file_store_path)
        if not sessions_root.is_absolute():
            sessions_root = self._workspace_root / sessions_root
        sessions_root = sessions_root.resolve()
        if not _is_relative_to(sessions_root, self._workspace_root):
            raise ValueError(
                "agent workflow audit root must stay inside workspace: "
                f"{sessions_root} is outside {self._workspace_root}"
            )
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
        tasks: list[SubAgentTask],
        status: str,
        finished_at: str | None = None,
    ) -> None:
        """写入 workflow 清单，输入为任务和状态，输出为 workflow.json。"""
        payload: dict[str, object] = {
            "workflow_id": workflow_id,
            "mode": mode,
            "parent_session_id": parent_session_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "status": status,
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
        )
        report_path = (
            workflow_dir / "reports" / f"{_task_run_id(display_order, run.task.task_id)}.json"
        )
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
        )
        self._append_audit(
            workflow_dir,
            action="subagent_reported",
            payload={
                "workflow_id": workflow_id,
                "task_id": detail.task_id,
                "session_id": detail.session_id,
                "run_id": detail.run_id,
                "status": detail.status,
                "reported_at": detail.reported_at,
                "report_path": str(report_path),
                "content_digest": detail.content_digest,
                "error_message": detail.error_message,
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
                "status": status,
                "reports_dir": str(reports_dir),
                "reports": [_report_projection_payload(report) for report in reports],
            },
        )
        return index_path

    def _append_audit(self, workflow_dir: Path, *, action: str, payload: dict[str, object]) -> None:
        """追加 workflow 审计事件，输入为 action 和 payload，输出为 audit.jsonl 新记录。"""
        record = {
            "ts": _now_iso(),
            "action": action,
            "payload": payload,
        }
        workflow_dir.mkdir(parents=True, exist_ok=True)
        with open(workflow_dir / "audit.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

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
    return {
        "task_id": task.task_id,
        "task_name": task.task_name,
        "tool_names": list(task.tool_names),
        "skill_names": list(task.skill_names),
        "permission": to_jsonable(task.permission) if task.permission is not None else None,
        "task_run_id": task.metadata.get("task_run_id"),
        "task_run_dir": task.metadata.get("task_run_dir"),
        "working_dir": task.metadata.get("working_dir"),
    }


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
        "task_run_id": run.task.metadata.get("task_run_id"),
        "task_run_dir": run.task.metadata.get("task_run_dir"),
        "working_dir": run.task.metadata.get("working_dir"),
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
    }


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
