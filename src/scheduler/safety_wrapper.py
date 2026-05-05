"""cron 运行期审批包装器。

v0.1 行为：fail-closed —— cron run 中需要 consent 的高风险动作直接拒绝，
整 run failed(failure_reason=needs_approval)，下次 trigger 重跑。

v0.2 升级位（不在 v0.1 实现）：lease 命中改判 silent_allow，其余仍 deny。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.contracts import ApprovalDecision, ApprovalProvider, ApprovalRequest

# 注：不直接 import safety.types.DecisionClass，因为 safety 模块不暴露给 scheduler。
# 改为读 metadata['decision_class'] 字符串字段（contracts.py:148 注释里的约定）。
_CONSENT_CLASS = "explicit_consent"


@dataclass(frozen=True)
class ScheduleApprovalProvider:
    """cron 模式下的 ApprovalProvider 包装器。

    Attributes:
        inner: 被包装的原 ApprovalProvider（通常是 SafetyGatedApproval）。
        task_id: 当前 cron 任务 id，仅作 metadata 标注用。
    """

    inner: ApprovalProvider
    task_id: str

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        decision = await self.inner.decide(request)

        decision_class = str(decision.metadata.get("decision_class", ""))

        if decision_class == _CONSENT_CLASS:
            # cron 模式下不调起交互式审批，直接拒绝
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

        # hard_block / silent_allow 原样透传
        return decision


__all__ = ["ScheduleApprovalProvider"]
