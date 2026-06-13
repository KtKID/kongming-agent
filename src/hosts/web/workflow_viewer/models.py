"""Agent workflow viewer DTO 与内部投影模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class WorkflowDiagnosticDTO(BaseModel):
    code: str
    severity: Literal["info", "warning", "error"] = "warning"
    message: str
    path: str | None = None


class WorkflowArtifactRefDTO(BaseModel):
    artifact_id: str
    path: str
    kind: Literal["json", "jsonl", "markdown", "text", "directory"]
    title: str
    size_bytes: int | None = None
    available: bool = True
    missing_reason: str | None = None


class WorkflowArtifactContentDTO(BaseModel):
    artifact_id: str
    path: str
    kind: str
    title: str
    content: Any = None
    truncated: bool = False
    diagnostics: list[WorkflowDiagnosticDTO] = Field(default_factory=list)


class WorkflowUsageRecordDTO(BaseModel):
    task_run_id: str | None = None
    task_id: str | None = None
    task_name: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    status: str | None = None
    provider: str = "unknown"
    source: str
    usage: dict[str, int] = Field(default_factory=dict)


class WorkflowUsageDTO(BaseModel):
    source: str
    totals: dict[str, int] = Field(default_factory=dict)
    provider_totals: dict[str, dict[str, int]] = Field(default_factory=dict)
    records: list[WorkflowUsageRecordDTO] = Field(default_factory=list)
    diagnostics: list[WorkflowDiagnosticDTO] = Field(default_factory=list)


class WorkflowListItemDTO(BaseModel):
    workflow_id: str
    thread_id: str
    mode: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    desc: str | None = None
    title: str
    report_count: int = 0
    has_mode_panel: bool = False
    usage: WorkflowUsageDTO
    diagnostics: list[WorkflowDiagnosticDTO] = Field(default_factory=list)


class WorkflowListDTO(BaseModel):
    thread_id: str
    workflows: list[WorkflowListItemDTO]


class WorkflowTimelineEventDTO(BaseModel):
    event_id: str
    timestamp: str | None = None
    action: str
    label: str
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkflowFlowNodeDTO(BaseModel):
    id: str
    label: str
    kind: str
    status: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkflowFlowEdgeDTO(BaseModel):
    id: str
    source: str
    target: str
    label: str | None = None


class SubAgentActivityEventDTO(BaseModel):
    activity_id: str
    activity_type: str
    ts: str | None = None
    title: str
    task_run_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    status: str | None = None
    summary: str | None = None
    source: str
    source_action: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class SubAgentReportSummaryDTO(BaseModel):
    task_run_id: str
    task_id: str | None = None
    task_name: str | None = None
    status: str | None = None
    summary: str | None = None
    error_message: str | None = None
    report_path: str | None = None
    working_dir: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    reported_at: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    conversation_available: bool = False
    conversation_source: str | None = None
    snapshot: dict[str, Any] = Field(default_factory=dict)
    activity_events: list[SubAgentActivityEventDTO] = Field(default_factory=list)
    diagnostics: list[WorkflowDiagnosticDTO] = Field(default_factory=list)


class WorkflowPanelDTO(BaseModel):
    panel_id: str
    mode: str
    kind: Literal[
        "summary",
        "table",
        "markdown",
        "json",
        "timeline",
        "review_board",
        "map_reduce",
    ]
    title: str
    payload: dict[str, Any] = Field(default_factory=dict)
    available: bool = True
    missing_reason: str | None = None


class WorkflowDetailDTO(BaseModel):
    item: WorkflowListItemDTO
    timeline: list[WorkflowTimelineEventDTO] = Field(default_factory=list)
    flow_nodes: list[WorkflowFlowNodeDTO] = Field(default_factory=list)
    flow_edges: list[WorkflowFlowEdgeDTO] = Field(default_factory=list)
    reports: list[SubAgentReportSummaryDTO] = Field(default_factory=list)
    panels: list[WorkflowPanelDTO] = Field(default_factory=list)
    artifacts: list[WorkflowArtifactRefDTO] = Field(default_factory=list)
    usage: WorkflowUsageDTO
    diagnostics: list[WorkflowDiagnosticDTO] = Field(default_factory=list)


class ConversationMessageDTO(BaseModel):
    record_index: int
    role: str
    content: str
    created_at: float | str | None = None
    message_type: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ConversationDTO(BaseModel):
    thread_id: str
    workflow_id: str
    task_run_id: str
    child_session_id: str | None = None
    source_path: str | None = None
    messages: list[ConversationMessageDTO] = Field(default_factory=list)
    next_cursor: str | None = None
    diagnostics: list[WorkflowDiagnosticDTO] = Field(default_factory=list)


@dataclass(frozen=True)
class SubAgentRecordRef:
    task_run_id: str
    subagent_json_path: Path
    subagent_json: dict[str, Any]
    task_id: str | None
    completed_run_id: str | None
    child_session_id: str | None
    child_session_log_path_raw: str | None
    report_path: Path | None
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowArtifactBundle:
    thread_id: str
    workflow_id: str
    workflow_dir: Path
    workflow_json: dict[str, Any] | None
    audit_events: tuple[dict[str, Any], ...]
    result_json: dict[str, Any] | None
    report_index: dict[str, Any] | None
    reports: tuple[dict[str, Any], ...]
    subagent_records: tuple[SubAgentRecordRef, ...]
    artifacts: tuple[WorkflowArtifactRefDTO, ...]
    diagnostics: tuple[WorkflowDiagnosticDTO, ...]


@dataclass(frozen=True)
class WorkflowModeProjection:
    panels: tuple[WorkflowPanelDTO, ...] = ()
    flow_nodes: tuple[WorkflowFlowNodeDTO, ...] = ()
    flow_edges: tuple[WorkflowFlowEdgeDTO, ...] = ()
    diagnostics: tuple[WorkflowDiagnosticDTO, ...] = ()
    has_mode_panel: bool = False
