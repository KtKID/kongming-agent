"""
司天运行时数据模型——11 个 frozen dataclass，零外部依赖。

Role:
    定义扫描观察、运行时状态、工作项、建议、报告等全部运行时数据结构。
    所有模型都是纯数据容器（frozen dataclass），含 to_dict / from_dict 序列化。

Owns:
    - 11 个 frozen dataclass 的字段定义与不变量校验
    - to_dict / from_dict / empty 等序列化方法

Does not own:
    - 配置校验（在 config.py）
    - 持久化 IO（在 store.py）
    - 业务计算逻辑（在 scanners / suggestions / analyzer）

Called by:
    - sitian 包内几乎所有其他模块（作为数据交换格式）

Key outputs:
    - SiTianObservation     —— 单次扫描观察记录
    - SiTianSourceRuntimeState —— 每个 source 的运行时状态（next_run_at 等）
    - SiTianWorkItem        —— 归并后的工作项
    - SiTianWorkspaceState  —— 工作区全局状态（建议/阻塞/风险）
    - SiTianAlert           —— analyzer 产出的告警（V2 schema）
    - SiTianProjectSnapshot —— analyzer 产出的项目快照
    - SiTianReport          —— analyzer 最终报告

Change risks:
    - 字段增删会影响 store.py 序列化/反序列化和前端展示
    - frozen=True 保证不可变，但构造点分散，加必填字段时需逐一补齐
"""

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
                SiTianWorkspaceBlocker.from_dict(dict(item)) for item in raw.get("blockers", [])
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


@dataclass(frozen=True)
class SiTianAlert:
    """单个问题卡片。LLM 按 session 维度产出。"""

    alert_id: str  # f"{project_short}:{session_id_short}:{type_slug}"
    priority: str  # P0 / P1 / P2
    severity: str  # high / medium / low
    severity_reasons: tuple[str, ...]
    title: str  # 短标题
    display_message: str  # 司天角色嘴里说的话
    session_ref: dict[str, str]  # {"sessionId", "projectId", "projectName"}
    evidence: tuple[dict[str, str], ...]  # [{"threadId", "quote"}]
    recommendation: str
    dispatch: dict[str, str]  # {"targetThreadId", "instructionDraft"} — task 1 占位
    dedupe_key: str  # f"{project_short}:{type_slug}"

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "alertId": self.alert_id,
            "priority": self.priority,
            "severity": self.severity,
            "severityReasons": list(self.severity_reasons),
            "title": self.title,
            "displayMessage": self.display_message,
            "sessionRef": dict(self.session_ref),
            "evidence": [dict(item) for item in self.evidence],
            "recommendation": self.recommendation,
            "dispatch": dict(self.dispatch),
            "dedupeKey": self.dedupe_key,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SiTianAlert:
        return cls(
            alert_id=str(raw["alertId"]),
            priority=str(raw["priority"]),
            severity=str(raw["severity"]),
            severity_reasons=_tuple_of_str(raw.get("severityReasons", [])),
            title=str(raw["title"]),
            display_message=str(raw["displayMessage"]),
            session_ref={str(k): str(v) for k, v in raw.get("sessionRef", {}).items()},
            evidence=tuple(
                {str(k): str(v) for k, v in item.items()} for item in raw.get("evidence", [])
            ),
            recommendation=str(raw["recommendation"]),
            dispatch={str(k): str(v) for k, v in raw.get("dispatch", {}).items()},
            dedupe_key=str(raw["dedupeKey"]),
        )


@dataclass(frozen=True)
class SiTianProjectSnapshot:
    """项目级元数据（替换 V1 SiTianReportItem）。"""

    project_id: str
    project_name: str
    status_by_rule: str  # active / idle / dormant / empty —— 脚本算
    status_reason: str  # LLM 解释
    narrative: str  # 项目级总结（不再杂糅多 session）
    session_stats: dict[
        str, int
    ]  # {"total", "activeWithin1h", "activeWithin24h", "activeWithin3d"}
    alert_ids: tuple[str, ...]  # 关联 top_alerts

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "projectId": self.project_id,
            "projectName": self.project_name,
            "statusByRule": self.status_by_rule,
            "statusReason": self.status_reason,
            "narrative": self.narrative,
            "sessionStats": dict(self.session_stats),
            "alertIds": list(self.alert_ids),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SiTianProjectSnapshot:
        return cls(
            project_id=str(raw["projectId"]),
            project_name=str(raw["projectName"]),
            status_by_rule=str(raw["statusByRule"]),
            status_reason=str(raw["statusReason"]),
            narrative=str(raw["narrative"]),
            session_stats={str(k): int(v) for k, v in raw.get("sessionStats", {}).items()},
            alert_ids=_tuple_of_str(raw.get("alertIds", [])),
        )


@dataclass(frozen=True)
class SiTianReport:
    """顶层报告。"""

    report_id: str
    generated_at: str
    summary: str
    model_name: str
    errors: tuple[str, ...]
    top_alerts: tuple[SiTianAlert, ...]
    projects: tuple[SiTianProjectSnapshot, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "reportId": self.report_id,
            "generatedAt": self.generated_at,
            "summary": self.summary,
            "modelName": self.model_name,
            "errors": list(self.errors),
            "topAlerts": [alert.to_dict() for alert in self.top_alerts],
            "projects": [project.to_dict() for project in self.projects],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SiTianReport:
        return cls(
            report_id=str(raw["reportId"]),
            generated_at=str(raw["generatedAt"]),
            summary=str(raw["summary"]),
            model_name=str(raw["modelName"]),
            errors=_tuple_of_str(raw.get("errors", [])),
            top_alerts=tuple(SiTianAlert.from_dict(a) for a in raw.get("topAlerts", [])),
            projects=tuple(SiTianProjectSnapshot.from_dict(p) for p in raw.get("projects", [])),
        )


__all__ = [
    "JsonValue",
    "SiTianAlert",
    "SiTianObservation",
    "SiTianPendingApproval",
    "SiTianProjectSnapshot",
    "SiTianReport",
    "SiTianSourceRuntimeState",
    "SiTianWorkItem",
    "SiTianWorkspaceBlocker",
    "SiTianWorkspaceRisk",
    "SiTianWorkspaceSourcesSummary",
    "SiTianWorkspaceState",
]
