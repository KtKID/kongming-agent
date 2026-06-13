"""Approval wrapper used by cron-triggered runs."""

from __future__ import annotations

import contextlib
import hashlib
import json as _json
from dataclasses import dataclass, field
from pathlib import Path

from core.contracts import (
    ApprovalDecision,
    ApprovalProvider,
    ApprovalRequest,
    Event,
    EventSink,
)
from infrastructure.config.models import SchedulerApprovalConfig
from scheduler.domain import ApprovalMode

_CONSENT_CLASS = "explicit_consent"
_HARD_BLOCK_CLASS = "hard_block"
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

    return not target.exists()


@dataclass(frozen=True)
class ScheduleApprovalProvider:
    """Wrap the app approval provider for scheduler-triggered runs.

    v0.5 新增字段（``mode`` / ``event_sink``）保持默认值向后兼容：现有
    ``ScheduleApprovalProvider(inner=..., task_id=...)`` 调用点不需要修改。

    决策分流：

    - ``hard_block`` 永远透传 rejected（安全不变量）。
    - ``explicit_consent``：
      - ``mode=TRUST``：转 approved + ``cron_trust_mode`` metadata，并 emit
        ``approval.cron.auto_allow`` 审计事件。
      - ``mode=FAIL_CLOSED``（默认）：保留现有 write_file 白名单（命中即放行），
        其余转 rejected + ``cron_fail_closed`` metadata。
    - ``silent_allow`` / ``standard_allow`` / 其他：透传。
    """

    inner: ApprovalProvider
    task_id: str
    mode: ApprovalMode = ApprovalMode.FAIL_CLOSED
    policy: SchedulerApprovalConfig = field(default_factory=SchedulerApprovalConfig)
    event_sink: EventSink | None = None

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        decision = await self.inner.decide(request)

        decision_class = str(decision.metadata.get("decision_class", ""))

        # 1. hard_block 永远透传 rejected（安全不变量；必须写在 explicit_consent 之前）
        if decision_class == _HARD_BLOCK_CLASS:
            return decision

        # 2. explicit_consent 按 mode 分流
        if decision_class == _CONSENT_CLASS:
            if self.mode is ApprovalMode.TRUST:
                return await self._wrap_trust_allow(decision, request)
            # mode == FAIL_CLOSED：保留现有 write_file 白名单 + 其他 rejected 逻辑
            return self._apply_fail_closed(decision, request)

        # 3. silent_allow / standard_allow / 其他：透传
        return decision

    async def _wrap_trust_allow(
        self,
        decision: ApprovalDecision,
        request: ApprovalRequest,
    ) -> ApprovalDecision:
        """trust 模式下把 explicit_consent decision 转 approved 并 emit event。"""
        new_meta = dict(decision.metadata)
        original_decision_class = new_meta.get("decision_class", _CONSENT_CLASS)
        original_decision_source = new_meta.get("decision_source", "")
        original_outcome = decision.outcome
        matched_rule = new_meta.get("matched_rule")
        original_reason = decision.reason

        new_meta["decision_class"] = "silent_allow"
        new_meta["decision_source"] = "cron_trust"
        new_meta["cron_trust_mode"] = True
        new_meta["original_decision_class"] = original_decision_class
        new_meta["original_decision_source"] = original_decision_source
        new_meta["original_outcome"] = original_outcome
        new_meta["cron_task_id"] = self.task_id

        new_decision = ApprovalDecision(
            outcome="approved",
            reason=f"cron trust auto-allow ({request.tool_name})",
            metadata=new_meta,
        )

        await self._emit_trust_event(
            request=request,
            original_decision_class=str(original_decision_class),
            original_decision_source=str(original_decision_source),
            matched_rule=matched_rule,
            original_reason=original_reason or "",
        )
        return new_decision

    async def _emit_trust_event(
        self,
        *,
        request: ApprovalRequest,
        original_decision_class: str,
        original_decision_source: str,
        matched_rule: object,
        original_reason: str,
    ) -> None:
        """trust 自动放行 emit Event(kind=approval.cron.auto_allow)。

        防御性约定：

        - ``event_sink is None``：静默跳过（老调用点不传 sink 不能挂）。
        - emit 抛异常：吞掉，不影响主决策（审计不应阻塞业务）。
        """
        if self.event_sink is None:
            return

        try:
            args_text = _json.dumps(request.arguments or {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            args_text = repr(request.arguments)
        arguments_digest = "sha256:" + hashlib.sha256(args_text[:200].encode("utf-8")).hexdigest()

        event = Event(
            kind="approval.cron.auto_allow",
            run_id=getattr(request, "run_id", "") or "",
            turn=getattr(request, "turn", None),
            payload={
                "task_id": self.task_id,
                "tool_name": request.tool_name,
                "original_decision_class": original_decision_class,
                "original_decision_source": original_decision_source,
                "matched_rule": matched_rule,
                "reason": original_reason,
                "arguments_digest": arguments_digest,
            },
        )
        # 审计 emit 失败不影响主决策（防御性）
        with contextlib.suppress(Exception):
            await self.event_sink.emit(event)

    def _apply_fail_closed(
        self,
        decision: ApprovalDecision,
        request: ApprovalRequest,
    ) -> ApprovalDecision:
        """fail_closed 模式：write_file create 白名单 + 其他 consent 转 rejected。"""
        if self.policy.allow_write_file_create_in_cwd and _is_allowed_cron_file_create(request):
            new_meta = dict(decision.metadata)
            new_meta["decision_class"] = _CRON_AUTO_ALLOW_CLASS
            new_meta["decision_source"] = "cron_whitelist"
            new_meta["cron_auto_allow"] = "write_file_create"
            new_meta["original_decision_class"] = _CONSENT_CLASS
            new_meta["original_outcome"] = decision.outcome
            new_meta["cron_task_id"] = self.task_id
            return ApprovalDecision(
                outcome="approved",
                reason=f"cron auto-allow: create file ({request.tool_name})",
                metadata=new_meta,
            )

        new_meta = dict(decision.metadata)
        new_meta["cron_fail_closed"] = True
        new_meta["original_decision_class"] = _CONSENT_CLASS
        new_meta["original_outcome"] = decision.outcome
        new_meta["cron_task_id"] = self.task_id
        return ApprovalDecision(
            outcome="rejected",
            reason=f"cron fail-closed: requires consent ({request.tool_name})",
            metadata=new_meta,
        )


__all__ = ["ScheduleApprovalProvider"]
