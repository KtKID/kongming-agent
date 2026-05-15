"""Approval wrapper used by cron-triggered runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from config_loader.models import SchedulerApprovalConfig
from core.contracts import ApprovalDecision, ApprovalProvider, ApprovalRequest

_CONSENT_CLASS = "explicit_consent"
_CRON_AUTO_ALLOW_CLASS = "silent_allow"


def _is_path_within(base: Path, target: Path) -> bool:
    try:
        target.relative_to(base)
    except ValueError:
        return False
    return True


def _is_allowed_cron_file_create(request: ApprovalRequest) -> bool:
    if request.tool_name != "write_file":
        return False

    raw_path = request.arguments.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return False

    if bool(request.arguments.get("append")):
        return False

    cwd = Path.cwd().resolve()
    target = Path(raw_path).expanduser().resolve()

    if not _is_path_within(cwd, target):
        return False

    if target.exists():
        return False

    return True


@dataclass(frozen=True)
class ScheduleApprovalProvider:
    """Wrap the app approval provider for scheduler-triggered runs."""

    inner: ApprovalProvider
    task_id: str
    policy: SchedulerApprovalConfig = field(default_factory=SchedulerApprovalConfig)

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        decision = await self.inner.decide(request)

        decision_class = str(decision.metadata.get("decision_class", ""))

        if decision_class == _CONSENT_CLASS:
            if (
                self.policy.allow_write_file_create_in_cwd
                and _is_allowed_cron_file_create(request)
            ):
                new_meta = dict(decision.metadata)
                new_meta["decision_class"] = _CRON_AUTO_ALLOW_CLASS
                new_meta["decision_source"] = "cron_whitelist"
                new_meta["cron_auto_allow"] = "write_file_create"
                new_meta["original_decision_class"] = decision_class
                new_meta["original_outcome"] = decision.outcome
                new_meta["cron_task_id"] = self.task_id
                return ApprovalDecision(
                    outcome="approved",
                    reason=f"cron auto-allow: create file ({request.tool_name})",
                    metadata=new_meta,
                )

            new_meta = dict(decision.metadata)
            new_meta["cron_fail_closed"] = True
            new_meta["original_decision_class"] = decision_class
            new_meta["original_outcome"] = decision.outcome
            new_meta["cron_task_id"] = self.task_id
            return ApprovalDecision(
                outcome="rejected",
                reason=f"cron fail-closed: requires consent ({request.tool_name})",
                metadata=new_meta,
            )

        return decision


__all__ = ["ScheduleApprovalProvider"]
