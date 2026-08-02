"""Safety v0.6 审批元数据键真源。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ApprovalMetadataKeys:
    """审批决策、pending 与宿主投影共用的 metadata 键。"""

    DECISION_CLASS: str = "decision_class"
    DECISION_SOURCE: str = "decision_source"
    MATCHED_RULE: str = "matched_rule"
    REASON: str = "reason"
    BOUNDARY_KIND: str = "boundary_kind"
    DANGER: str = "danger"
    REMEMBER_ALLOWED: str = "remember_allowed"
    REMEMBER_RULE: str = "remember_rule"
    REMEMBER_THREAD_ID: str = "remember_thread_id"
    REMEMBER_REVISION: str = "remember_revision"
    SUGGESTED_ALTERNATIVES: str = "suggested_alternatives"


__all__ = ["ApprovalMetadataKeys"]
