"""Agent workflow manager.

The workflow manager is the boundary for multi-agent orchestration. Version 1
implements one mode: run N independent child-agent tasks in parallel and return
their reports to the parent agent.
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
from typing import Any, Literal

from config_loader.models import Config
from executors.agent_runtime.subagent_manager import SubAgentManager, SubAgentRun, SubAgentTask
from executors.agent_runtime.subagent_permissions import (
    SubAgentCreationRecord,
    WorkflowAuditWriter,
    parse_permission_spec,
    to_jsonable,
    validate_scoped_tool_names,
)

WorkflowMode = Literal["parallel"]


@dataclass(frozen=True)
class SubAgentReportDetail:
    """Auditable child-agent report stored under reports/<task_run_id>.json."""

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
    """Small report projection returned to the parent agent and future Web views."""

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
    """Final workflow result returned to the caller."""

    workflow_id: str
    mode: WorkflowMode
    parent_session_id: str
    workflow_dir: Path
    started_at: str
    finished_at: str
    runs: tuple[SubAgentRun, ...]
    reports: tuple[SubAgentReportProjection, ...]
    report_index_path: Path

    @property
    def completed(self) -> bool:
        return all(run.status == "completed" for run in self.runs)


@dataclass(frozen=True)
class _RunOutcome:
    run: SubAgentRun
    report: SubAgentReportProjection


class AgentWorkflowAuditWriter:
    """Writes workflow audit files owned by AgentWorkflowManager."""

    def __init__(self, workflow_dir: Path) -> None:
        self._workflow_dir = workflow_dir

    @property
    def audit_log_path(self) -> Path:
        return self._workflow_dir / "audit.jsonl"

    def write_event(self, event: Mapping[str, Any]) -> None:
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
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)


class AgentWorkflowManager:
    """Coordinates sub-agents and owns workflow audit files."""

    def __init__(
        self,
        *,
        subagents: SubAgentManager,
        config: Config,
        workspace_root: Path,
    ) -> None:
        self._subagents = subagents
        self._config = config
        self._workspace_root = workspace_root.resolve()

    async def run_parallel(
        self,
        *,
        parent_session_id: str,
        tasks: list[SubAgentTask],
    ) -> AgentWorkflowResult:
        """Run all tasks concurrently with independent child sessions."""
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
        """Parse public task specs and run the v1 parallel workflow."""
        tasks: list[SubAgentTask] = []
        for index, spec in enumerate(task_specs, 1):
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
                    task_name=str(spec["task_name"]),
                    prompt=str(spec["prompt"]),
                    context=str(spec.get("context", "")),
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
        """Run a workflow by mode.

        V1 exposes the mode boundary now and implements only ``parallel``.
        """
        if mode != "parallel":
            raise ValueError(f"unsupported agent workflow mode: {mode}")
        return await self.run_parallel_specs(
            parent_session_id=parent_session_id,
            task_specs=task_specs,
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
        sessions_root = Path(self._config.session.file_store_path)
        if not sessions_root.is_absolute():
            sessions_root = self._workspace_root / sessions_root
        sessions_root = sessions_root.resolve()
        if not _is_relative_to(sessions_root, self._workspace_root):
            raise ValueError(
                "agent workflow audit root must stay inside workspace: "
                f"{sessions_root} is outside {self._workspace_root}"
            )
        return sessions_root / parent_session_id / "agent-workflows" / workflow_id

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
        record = {
            "ts": _now_iso(),
            "action": action,
            "payload": payload,
        }
        workflow_dir.mkdir(parents=True, exist_ok=True)
        with open(workflow_dir / "audit.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _write_json(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)


def _task_payload(task: SubAgentTask) -> dict[str, object]:
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
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def _summary(content: str, *, max_chars: int = 500) -> str:
    summary = " ".join(content.strip().split())
    if len(summary) <= max_chars:
        return summary
    return summary[: max_chars - 1] + "…"


def _optional_string(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _agent_result_path(workflow_dir: Path, task: SubAgentTask) -> Path:
    task_run_dir = task.metadata.get("task_run_dir")
    if isinstance(task_run_dir, str) and task_run_dir.strip():
        return Path(task_run_dir) / "result.json"
    working_dir = task.metadata.get("working_dir")
    if isinstance(working_dir, str) and working_dir.strip():
        return Path(working_dir) / "result.json"
    return workflow_dir / "agents" / f"{_metadata_task_run_id(task)}" / "result.json"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _slug(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value.strip())
    safe = safe.strip("-_").lower()
    return safe or "task"


def _task_run_id(display_order: int, task_id: str) -> str:
    return f"{display_order:03d}-{_slug(task_id)}"


def _metadata_task_run_id(task: SubAgentTask) -> str:
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
    parent = _slug(parent_session_id)[:32]
    workflow = _slug(workflow_id)[:32]
    task = _slug(task_id)[:32]
    return f"subagent-{parent}-{workflow}-{task}"


def _is_relative_to(path: Path, root: Path) -> bool:
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
