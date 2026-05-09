from __future__ import annotations

from dataclasses import dataclass
from typing import Any

JsonValue = Any


@dataclass(frozen=True)
class SiTianObservation:
    id: str
    source_id: str
    source_kind: str
    observed_at: str
    entity_type: str
    entity_key: str
    payload: dict[str, JsonValue]
    evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "sourceId": self.source_id,
            "sourceKind": self.source_kind,
            "observedAt": self.observed_at,
            "entityType": self.entity_type,
            "entityKey": self.entity_key,
            "payload": dict(self.payload),
            "evidenceRefs": list(self.evidence_refs),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SiTianObservation:
        return cls(
            id=str(raw["id"]),
            source_id=str(raw["sourceId"]),
            source_kind=str(raw["sourceKind"]),
            observed_at=str(raw["observedAt"]),
            entity_type=str(raw["entityType"]),
            entity_key=str(raw["entityKey"]),
            payload=dict(raw.get("payload", {})),
            evidence_refs=tuple(str(item) for item in raw.get("evidenceRefs", [])),
        )


@dataclass(frozen=True)
class SiTianSourceRuntimeState:
    source_id: str
    scan_interval_sec: int
    retry_backoff_sec: int
    next_run_at: str
    status: str
    last_run_at: str | None = None
    last_success_at: str | None = None
    last_error_at: str | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {
            "sourceId": self.source_id,
            "scanIntervalSec": self.scan_interval_sec,
            "retryBackoffSec": self.retry_backoff_sec,
            "nextRunAt": self.next_run_at,
            "status": self.status,
        }
        if self.last_run_at is not None:
            data["lastRunAt"] = self.last_run_at
        if self.last_success_at is not None:
            data["lastSuccessAt"] = self.last_success_at
        if self.last_error_at is not None:
            data["lastErrorAt"] = self.last_error_at
        if self.last_error is not None:
            data["lastError"] = self.last_error
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SiTianSourceRuntimeState:
        return cls(
            source_id=str(raw["sourceId"]),
            scan_interval_sec=int(raw["scanIntervalSec"]),
            retry_backoff_sec=int(raw["retryBackoffSec"]),
            next_run_at=str(raw["nextRunAt"]),
            status=str(raw["status"]),
            last_run_at=_as_optional_str(raw.get("lastRunAt")),
            last_success_at=_as_optional_str(raw.get("lastSuccessAt")),
            last_error_at=_as_optional_str(raw.get("lastErrorAt")),
            last_error=_as_optional_str(raw.get("lastError")),
        )


@dataclass(frozen=True)
class SiTianWorkItem:
    id: str
    title: str
    status: str
    priority: str
    source_ids: tuple[str, ...]
    thread_ids: tuple[str, ...]
    project_paths: tuple[str, ...]
    blockers: tuple[str, ...]
    artifacts: tuple[str, ...]
    next_actions: tuple[str, ...]
    risks: tuple[str, ...]
    updated_at: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "priority": self.priority,
            "sourceIds": list(self.source_ids),
            "threadIds": list(self.thread_ids),
            "projectPaths": list(self.project_paths),
            "blockers": list(self.blockers),
            "artifacts": list(self.artifacts),
            "nextActions": list(self.next_actions),
            "risks": list(self.risks),
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SiTianWorkItem:
        return cls(
            id=str(raw["id"]),
            title=str(raw["title"]),
            status=str(raw["status"]),
            priority=str(raw["priority"]),
            source_ids=_tuple_of_str(raw.get("sourceIds", [])),
            thread_ids=_tuple_of_str(raw.get("threadIds", [])),
            project_paths=_tuple_of_str(raw.get("projectPaths", [])),
            blockers=_tuple_of_str(raw.get("blockers", [])),
            artifacts=_tuple_of_str(raw.get("artifacts", [])),
            next_actions=_tuple_of_str(raw.get("nextActions", [])),
            risks=_tuple_of_str(raw.get("risks", [])),
            updated_at=str(raw["updatedAt"]),
        )


@dataclass(frozen=True)
class SiTianWorkspaceSourcesSummary:
    total: int
    active: int

    def to_dict(self) -> dict[str, JsonValue]:
        return {"total": self.total, "active": self.active}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SiTianWorkspaceSourcesSummary:
        return cls(total=int(raw.get("total", 0)), active=int(raw.get("active", 0)))


@dataclass(frozen=True)
class SiTianWorkspaceBlocker:
    work_item_id: str
    summary: str
    severity: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "workItemId": self.work_item_id,
            "summary": self.summary,
            "severity": self.severity,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SiTianWorkspaceBlocker:
        return cls(
            work_item_id=str(raw["workItemId"]),
            summary=str(raw["summary"]),
            severity=str(raw["severity"]),
        )


@dataclass(frozen=True)
class SiTianPendingApproval:
    thread_id: str
    summary: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {"threadId": self.thread_id, "summary": self.summary}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SiTianPendingApproval:
        return cls(thread_id=str(raw["threadId"]), summary=str(raw["summary"]))


@dataclass(frozen=True)
class SiTianWorkspaceRisk:
    category: str
    summary: str
    evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "category": self.category,
            "summary": self.summary,
            "evidenceRefs": list(self.evidence_refs),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SiTianWorkspaceRisk:
        return cls(
            category=str(raw["category"]),
            summary=str(raw["summary"]),
            evidence_refs=_tuple_of_str(raw.get("evidenceRefs", [])),
        )


@dataclass(frozen=True)
class SiTianWorkspaceState:
    version: str
    updated_at: str
    sources: SiTianWorkspaceSourcesSummary
    work_items: tuple[SiTianWorkItem, ...]
    blockers: tuple[SiTianWorkspaceBlocker, ...]
    pending_approvals: tuple[SiTianPendingApproval, ...]
    risks: tuple[SiTianWorkspaceRisk, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "version": self.version,
            "updatedAt": self.updated_at,
            "sources": self.sources.to_dict(),
            "workItems": [item.to_dict() for item in self.work_items],
            "blockers": [item.to_dict() for item in self.blockers],
            "pendingApprovals": [item.to_dict() for item in self.pending_approvals],
            "risks": [item.to_dict() for item in self.risks],
        }

    @classmethod
    def empty(cls, *, updated_at: str) -> SiTianWorkspaceState:
        return cls(
            version="v1",
            updated_at=updated_at,
            sources=SiTianWorkspaceSourcesSummary(total=0, active=0),
            work_items=(),
            blockers=(),
            pending_approvals=(),
            risks=(),
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SiTianWorkspaceState:
        return cls(
            version=str(raw.get("version", "v1")),
            updated_at=str(raw["updatedAt"]),
            sources=SiTianWorkspaceSourcesSummary.from_dict(dict(raw.get("sources", {}))),
            work_items=tuple(
                SiTianWorkItem.from_dict(dict(item)) for item in raw.get("workItems", [])
            ),
            blockers=tuple(
                SiTianWorkspaceBlocker.from_dict(dict(item))
                for item in raw.get("blockers", [])
            ),
            pending_approvals=tuple(
                SiTianPendingApproval.from_dict(dict(item))
                for item in raw.get("pendingApprovals", [])
            ),
            risks=tuple(SiTianWorkspaceRisk.from_dict(dict(item)) for item in raw.get("risks", [])),
        )


def _tuple_of_str(values: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in values or ())


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


__all__ = [
    "JsonValue",
    "SiTianObservation",
    "SiTianPendingApproval",
    "SiTianSourceRuntimeState",
    "SiTianWorkItem",
    "SiTianWorkspaceBlocker",
    "SiTianWorkspaceRisk",
    "SiTianWorkspaceSourcesSummary",
    "SiTianWorkspaceState",
]
