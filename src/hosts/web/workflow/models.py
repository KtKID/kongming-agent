"""Workflow Dashboard 四对象模型 + 共享类型。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

# ── 共享枚举 ────────────────────────────────────────────


class StageStatus(StrEnum):
    DONE = "done"
    NOT_STARTED = "not_started"


class NodeCategory(StrEnum):
    ANALYSIS = "analysis"
    PLANNING = "planning"
    EXECUTION = "execution"
    GATE = "gate"
    REPAIR = "repair"


class RunStatus(StrEnum):
    UNBOUND = "unbound"
    ACTIVE = "active"
    COMPLETED = "completed"


class EdgeTrigger(StrEnum):
    ON_COMPLETED = "on_completed"
    ON_FAILED = "on_failed"


class EventSource(StrEnum):
    SCANNER = "scanner"
    MANUAL = "manual"


# ── Scanner 产出 ────────────────────────────────────────


@dataclass
class SpecSnapshot:
    spec_id: str
    name: str
    path: str
    has_goals: bool = False
    has_module_breakdown: bool = False
    has_task_map: bool = False
    has_architecture: bool = False


@dataclass
class TaskSnapshot:
    task_id: str
    name: str
    path: str
    spec_ref: str | None = None
    stages: dict[str, StageStatus] = field(default_factory=dict)
    pinned: bool = False


@dataclass
class ScanResult:
    specs: list[SpecSnapshot]
    tasks: list[TaskSnapshot]
    scanned_at: str = field(default_factory=lambda: _now_iso())


# ── 四对象模型 ──────────────────────────────────────────


@dataclass(frozen=True)
class NodeTemplate:
    id: str
    name: str
    category: NodeCategory
    detection_file: str | None = None


@dataclass
class WorkflowNode:
    id: str
    template_id: str
    position: int


@dataclass
class WorkflowEdge:
    id: str
    from_node_id: str
    to_node_id: str
    trigger: EdgeTrigger = EdgeTrigger.ON_COMPLETED


@dataclass
class WorkflowDefinition:
    id: str
    name: str
    description: str
    nodes: list[WorkflowNode]
    edges: list[WorkflowEdge]
    is_preset: bool = False
    created_at: str = field(default_factory=lambda: _now_iso())


@dataclass
class WorkflowRun:
    id: str
    workflow_id: str
    task_id: str
    spec_id: str | None = None
    node_states: dict[str, StageStatus] = field(default_factory=dict)
    status: RunStatus = RunStatus.ACTIVE
    created_at: str = field(default_factory=lambda: _now_iso())
    updated_at: str = field(default_factory=lambda: _now_iso())


@dataclass
class WorkflowEvent:
    event_id: str
    run_id: str
    node_id: str
    old_status: StageStatus
    new_status: StageStatus
    source: EventSource = EventSource.SCANNER
    timestamp: str = field(default_factory=lambda: _now_iso())


# ── helpers ─────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:12]
