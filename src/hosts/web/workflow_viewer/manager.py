"""Agent workflow run viewer 只读门面。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

from hosts.web.workflow_viewer.adapters import WorkflowViewerAdapterRegistry
from hosts.web.workflow_viewer.artifact_reader import (
    WorkflowArtifactReader,
    _is_relative_to,
)
from hosts.web.workflow_viewer.conversation_loader import ConversationLoader
from hosts.web.workflow_viewer.models import (
    ConversationDTO,
    SubAgentRecordRef,
    SubAgentReportSummaryDTO,
    WorkflowArtifactBundle,
    WorkflowArtifactContentDTO,
    WorkflowDetailDTO,
    WorkflowDiagnosticDTO,
    WorkflowListDTO,
    WorkflowListItemDTO,
    WorkflowTimelineEventDTO,
)
from hosts.web.workflow_viewer.usage_projector import UsageProjector
from infrastructure.config.paths import resolve_kongming_path

WORKFLOW_ID_RE: re.Pattern[str] = re.compile(r"^wf-[A-Za-z0-9_.:-]+$")
TASK_RUN_ID_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_.:-]+$")


class WorkflowRunViewerManager:
    """读取 thread-scoped workflow 产物并生成 Web DTO。"""

    def __init__(self, *, config: Any, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.session_root = self._resolve_session_root(config)
        self._registry = WorkflowViewerAdapterRegistry()
        self._usage = UsageProjector()
        self._conversation = ConversationLoader(
            session_root=self.session_root,
            workspace_root=self.workspace_root,
        )

    def list_workflows(self, thread_id: str) -> WorkflowListDTO:
        workflow_root = self._workflow_root(thread_id)
        if not workflow_root.exists():
            return WorkflowListDTO(thread_id=thread_id, workflows=[])
        items: list[WorkflowListItemDTO] = []
        for workflow_dir in sorted(workflow_root.iterdir(), key=_mtime_sort_key, reverse=True):
            if not workflow_dir.is_dir():
                continue
            if not WORKFLOW_ID_RE.match(workflow_dir.name):
                continue
            bundle = self._load_bundle(thread_id, workflow_dir.name)
            projection = self._registry.get(_mode(bundle)).project(bundle)
            items.append(self._list_item(bundle, projection.has_mode_panel))
        return WorkflowListDTO(thread_id=thread_id, workflows=items)

    def get_workflow_detail(self, thread_id: str, workflow_id: str) -> WorkflowDetailDTO:
        bundle = self._load_bundle(thread_id, workflow_id)
        projection = self._registry.get(_mode(bundle)).project(bundle)
        item = self._list_item(bundle, projection.has_mode_panel)
        diagnostics = [
            *bundle.diagnostics,
            *projection.diagnostics,
            *item.usage.diagnostics,
        ]
        return WorkflowDetailDTO(
            item=item,
            timeline=self._timeline(bundle),
            flow_nodes=list(projection.flow_nodes),
            flow_edges=list(projection.flow_edges),
            reports=self._report_summaries(bundle),
            panels=list(projection.panels),
            artifacts=list(bundle.artifacts),
            usage=item.usage,
            diagnostics=diagnostics,
        )

    def load_conversation(
        self,
        *,
        thread_id: str,
        workflow_id: str,
        task_run_id: str,
        cursor: int = 0,
        limit: int = 100,
    ) -> ConversationDTO:
        self._validate_task_run_id(task_run_id)
        bundle = self._load_bundle(thread_id, workflow_id)
        record = self._subagent_record(bundle, task_run_id)
        if record is None:
            return ConversationDTO(
                thread_id=thread_id,
                workflow_id=workflow_id,
                task_run_id=task_run_id,
                diagnostics=[
                    WorkflowDiagnosticDTO(
                        code="subagent.missing",
                        severity="warning",
                        message=f"找不到子 agent 记录: {task_run_id}",
                    )
                ],
            )
        return self._conversation.load(
            thread_id=thread_id,
            workflow_id=workflow_id,
            task_run_id=task_run_id,
            subagent_json=record.subagent_json,
            cursor=cursor,
            limit=limit,
        )

    def read_artifact(
        self, *, thread_id: str, workflow_id: str, artifact_id: str
    ) -> WorkflowArtifactContentDTO:
        workflow_dir = self._workflow_dir(thread_id, workflow_id)
        reader = WorkflowArtifactReader(workflow_dir)
        return reader.read_artifact_content(artifact_id)

    def _load_bundle(self, thread_id: str, workflow_id: str) -> WorkflowArtifactBundle:
        workflow_dir = self._workflow_dir(thread_id, workflow_id)
        reader = WorkflowArtifactReader(workflow_dir)
        diagnostics: list[WorkflowDiagnosticDTO] = []
        workflow_json, diag = reader.read_json("workflow.json")
        diagnostics.extend(diag)
        audit_events, diag = reader.read_jsonl("audit.jsonl")
        diagnostics.extend(diag)
        result_json, diag = reader.read_json("result.json")
        diagnostics.extend(diag)
        report_index, diag = reader.read_json("reports/index.json")
        diagnostics.extend(diag)
        reports, diag = self._read_reports(reader, report_index)
        diagnostics.extend(diag)
        subagents, diag = self._read_subagents(workflow_dir, reports)
        diagnostics.extend(diag)
        return WorkflowArtifactBundle(
            thread_id=thread_id,
            workflow_id=workflow_id,
            workflow_dir=workflow_dir,
            workflow_json=workflow_json,
            audit_events=tuple(audit_events),
            result_json=result_json,
            report_index=report_index,
            reports=tuple(reports),
            subagent_records=tuple(subagents),
            artifacts=tuple(reader.list_artifacts()),
            diagnostics=tuple(diagnostics),
        )

    def _list_item(
        self, bundle: WorkflowArtifactBundle, has_mode_panel: bool
    ) -> WorkflowListItemDTO:
        workflow_json = bundle.workflow_json or {}
        usage = self._usage.project(bundle)
        mode = _mode(bundle)
        return WorkflowListItemDTO(
            workflow_id=bundle.workflow_id,
            thread_id=bundle.thread_id,
            mode=mode,
            status=str(
                workflow_json.get("status") or (bundle.result_json or {}).get("status") or "unknown"
            ),
            started_at=_str_or_none(workflow_json.get("started_at")),
            finished_at=_str_or_none(workflow_json.get("finished_at")),
            desc=_desc(bundle),
            title=_title(bundle, mode),
            report_count=len(bundle.reports),
            has_mode_panel=has_mode_panel,
            usage=usage,
            diagnostics=[*bundle.diagnostics, *usage.diagnostics],
        )

    def _report_summaries(self, bundle: WorkflowArtifactBundle) -> list[SubAgentReportSummaryDTO]:
        summaries: list[SubAgentReportSummaryDTO] = []
        subagent_by_task_run = {record.task_run_id: record for record in bundle.subagent_records}
        subagent_by_session = {
            record.child_session_id: record
            for record in bundle.subagent_records
            if record.child_session_id is not None
        }
        for report in bundle.reports:
            task_run_id = _task_run_id_from_report(report)
            session_id = _str_or_none(report.get("session_id"))
            record = subagent_by_task_run.get(task_run_id)
            if record is None and session_id is not None:
                record = subagent_by_session.get(session_id)
            diagnostics: list[WorkflowDiagnosticDTO] = []
            available = False
            source = None
            if record is not None:
                available, source, diagnostics = self._conversation.conversation_available(
                    record.subagent_json
                )
            summaries.append(
                SubAgentReportSummaryDTO(
                    task_run_id=task_run_id,
                    task_id=_str_or_none(report.get("task_id")),
                    task_name=_str_or_none(report.get("task_name")),
                    status=_str_or_none(report.get("status")),
                    summary=_str_or_none(report.get("summary")),
                    error_message=_str_or_none(report.get("error_message")),
                    report_path=_relative_report_path(report),
                    working_dir=_str_or_none(report.get("working_dir")),
                    session_id=_str_or_none(report.get("session_id")),
                    run_id=_str_or_none(report.get("run_id")),
                    reported_at=_str_or_none(report.get("reported_at")),
                    usage=_numeric_usage(report.get("usage")),
                    conversation_available=available,
                    conversation_source=source,
                    diagnostics=diagnostics,
                )
            )
        for record in bundle.subagent_records:
            if record.task_run_id not in {summary.task_run_id for summary in summaries}:
                available, source, diagnostics = self._conversation.conversation_available(
                    record.subagent_json
                )
                summaries.append(
                    SubAgentReportSummaryDTO(
                        task_run_id=record.task_run_id,
                        task_id=_str_or_none(record.subagent_json.get("task_id")),
                        task_name=_str_or_none(record.subagent_json.get("task_name")),
                        status=_str_or_none(record.subagent_json.get("completed_status")),
                        session_id=record.child_session_id,
                        run_id=_str_or_none(record.subagent_json.get("completed_run_id")),
                        usage=_numeric_usage(record.subagent_json.get("usage")),
                        conversation_available=available,
                        conversation_source=source,
                        diagnostics=diagnostics,
                    )
                )
        return summaries

    @staticmethod
    def _timeline(bundle: WorkflowArtifactBundle) -> list[WorkflowTimelineEventDTO]:
        events: list[WorkflowTimelineEventDTO] = []
        for index, event in enumerate(bundle.audit_events):
            action = str(event.get("action") or "unknown")
            payload = event.get("payload")
            events.append(
                WorkflowTimelineEventDTO(
                    event_id=f"audit-{index}",
                    timestamp=_str_or_none(event.get("ts")),
                    action=action,
                    label=action.replace("_", " "),
                    payload=payload if isinstance(payload, dict) else {},
                )
            )
        return events

    @staticmethod
    def _read_reports(
        reader: WorkflowArtifactReader, report_index: dict[str, Any] | None
    ) -> tuple[list[dict[str, Any]], list[WorkflowDiagnosticDTO]]:
        diagnostics: list[WorkflowDiagnosticDTO] = []
        reports: list[dict[str, Any]] = []
        report_dir = reader.workflow_dir / "reports"
        if report_dir.is_dir():
            for path in sorted(report_dir.glob("*.json"), key=lambda p: p.name):
                if path.name == "index.json":
                    continue
                rel = path.relative_to(reader.workflow_dir).as_posix()
                payload, diag = reader.read_json(rel)
                diagnostics.extend(diag)
                if payload is not None:
                    payload.setdefault("task_run_id", path.stem)
                    payload.setdefault("report_path", rel)
                    reports.append(payload)
        if reports:
            return reports, diagnostics
        if isinstance(report_index, dict) and isinstance(report_index.get("reports"), list):
            for index, item in enumerate(report_index["reports"]):
                if isinstance(item, dict):
                    fallback = dict(item)
                    fallback.setdefault("task_run_id", _task_run_id_from_report(fallback, index))
                    reports.append(fallback)
        return reports, diagnostics

    @staticmethod
    def _read_subagents(
        workflow_dir: Path, reports: list[dict[str, Any]]
    ) -> tuple[list[SubAgentRecordRef], list[WorkflowDiagnosticDTO]]:
        diagnostics: list[WorkflowDiagnosticDTO] = []
        records: list[SubAgentRecordRef] = []
        reports_by_task_run = {
            _task_run_id_from_report(report): _path_or_none(report.get("report_path"))
            for report in reports
        }
        agents_dir = workflow_dir / "agents"
        if not agents_dir.is_dir():
            return records, diagnostics
        for subagent_path in sorted(agents_dir.glob("*/subagent.json"), key=lambda p: str(p)):
            try:
                payload = _read_json_file(subagent_path)
            except (OSError, ValueError) as exc:
                diagnostics.append(
                    WorkflowDiagnosticDTO(
                        code="subagent.read_failed",
                        severity="warning",
                        message=f"读取 subagent.json 失败: {type(exc).__name__}: {exc}",
                        path=subagent_path.relative_to(workflow_dir).as_posix(),
                    )
                )
                continue
            task_run_id = _str_or_none(payload.get("task_run_id")) or subagent_path.parent.name
            records.append(
                SubAgentRecordRef(
                    task_run_id=task_run_id,
                    subagent_json_path=subagent_path,
                    subagent_json=payload,
                    child_session_id=_str_or_none(payload.get("session_id")),
                    child_session_log_path_raw=_str_or_none(payload.get("child_session_log_path")),
                    report_path=reports_by_task_run.get(task_run_id),
                    usage=_numeric_usage(payload.get("usage")),
                )
            )
        return records, diagnostics

    def _subagent_record(
        self, bundle: WorkflowArtifactBundle, task_run_id: str
    ) -> SubAgentRecordRef | None:
        for record in bundle.subagent_records:
            if record.task_run_id == task_run_id:
                return record
        return None

    def _workflow_root(self, thread_id: str) -> Path:
        thread_root = (self.session_root / thread_id).resolve()
        if not _is_relative_to(thread_root, self.session_root):
            raise ValueError("thread root escapes session root")
        return thread_root / "agent-workflows"

    def _workflow_dir(self, thread_id: str, workflow_id: str) -> Path:
        self._validate_workflow_id(workflow_id)
        workflow_dir = (self._workflow_root(thread_id) / workflow_id).resolve()
        if not _is_relative_to(workflow_dir, self._workflow_root(thread_id)):
            raise ValueError("workflow dir escapes thread root")
        if not workflow_dir.is_dir():
            raise FileNotFoundError(f"workflow not found: {workflow_id}")
        return workflow_dir

    @staticmethod
    def _validate_workflow_id(workflow_id: str) -> None:
        if not WORKFLOW_ID_RE.match(workflow_id):
            raise ValueError(f"invalid workflow_id: {workflow_id!r}")

    @staticmethod
    def _validate_task_run_id(task_run_id: str) -> None:
        if not TASK_RUN_ID_RE.match(task_run_id):
            raise ValueError(f"invalid task_run_id: {task_run_id!r}")

    def _resolve_session_root(self, config: Any) -> Path:
        return resolve_kongming_path(config.session.file_store_path)


def _mode(bundle: WorkflowArtifactBundle) -> str:
    for payload in (bundle.workflow_json, bundle.result_json, bundle.report_index):
        if isinstance(payload, dict) and isinstance(payload.get("mode"), str):
            return str(payload["mode"])
    return "unknown"


def _title(bundle: WorkflowArtifactBundle, mode: str) -> str:
    desc = _desc(bundle)
    if desc:
        return desc
    result = bundle.result_json or {}
    mode_section = result.get(mode)
    if isinstance(mode_section, dict) and isinstance(mode_section.get("topic"), str):
        return cast(str, mode_section["topic"])
    if isinstance(result.get("title"), str):
        return cast(str, result["title"])
    return f"{mode} · {bundle.workflow_id}"


def _desc(bundle: WorkflowArtifactBundle) -> str | None:
    for payload in (bundle.workflow_json, bundle.result_json, bundle.report_index):
        if isinstance(payload, dict) and isinstance(payload.get("desc"), str):
            desc = " ".join(str(payload["desc"]).split())
            if desc:
                return desc
    return None


def _task_run_id_from_report(report: dict[str, Any], fallback_index: int | None = None) -> str:
    if isinstance(report.get("task_run_id"), str):
        return str(report["task_run_id"])
    path_value = report.get("report_path")
    if isinstance(path_value, str):
        stem = Path(path_value).stem
        if stem and stem != "index":
            return stem
    task_id = _str_or_none(report.get("task_id"))
    display_order = report.get("display_order")
    if isinstance(display_order, int) and task_id:
        return f"{display_order:03d}-{task_id}"
    if task_id:
        return task_id
    return f"report-{fallback_index or 0}"


def _relative_report_path(report: dict[str, Any]) -> str | None:
    path_value = _str_or_none(report.get("report_path"))
    if not path_value:
        return None
    path = Path(path_value)
    if path.is_absolute():
        return path.name
    return path_value.replace("\\", "/")


def _numeric_usage(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): int(value)
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
    }


def _read_json_file(path: Path) -> dict[str, Any]:
    import json

    payload = json.loads(path.read_text("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("json root must be object")
    return payload


def _path_or_none(value: Any) -> Path | None:
    return Path(value) if isinstance(value, str) else None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _mtime_sort_key(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
