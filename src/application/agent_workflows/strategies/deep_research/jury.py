"""Deep Research 陪审裁决器。

本脚本负责把多个 juror vote 聚合成事实组裁决。
作用是固定 reject quorum、弃权和 tally 语义，让 Crosscheck 阶段可离线测试。
关键执行流程：decide 统计 uphold/reject/abstain，reject 达到 quorum 则否决，全部弃权则按 insufficient_casts 否决，其余保留事实。
关键函数：AdversarialJury.decide 聚合单组投票，build_fallback_votes 生成确定性兜底投票。
"""

from __future__ import annotations

from application.agent_workflows.strategies.deep_research.contracts import (
    CheckedFactGroup,
    DeepResearchLimits,
    FactGroup,
    JuryRuling,
)


class AdversarialJury:
    """聚合事实组陪审投票。"""

    def __init__(self, *, reject_quorum: int | None = None) -> None:
        """初始化裁决器，输入为 reject quorum，输出为可复用实例。"""
        if reject_quorum is not None and reject_quorum <= 0:
            raise ValueError("reject_quorum must be > 0")
        self._reject_quorum = reject_quorum

    def aggregate_rulings(
        self,
        *,
        group: FactGroup,
        rulings: list[JuryRuling] | tuple[JuryRuling, ...],
        limits: DeepResearchLimits,
    ) -> CheckedFactGroup:
        """聚合单组投票，输入为事实组和投票，输出为裁决记录。"""
        reject_quorum = self._reject_quorum or limits.reject_quorum
        uphold_count = sum(1 for ruling in rulings if not ruling.reject and not ruling.abstain)
        reject_count = sum(1 for ruling in rulings if ruling.reject and not ruling.abstain)
        abstain_count = sum(1 for ruling in rulings if ruling.abstain)
        cast_count = uphold_count + reject_count
        tally = f"{uphold_count}-{reject_count}"
        if cast_count == 0:
            return CheckedFactGroup(
                group_id=group.group_id,
                status="rejected",
                cast_count=cast_count,
                tally=tally,
                reject_count=reject_count,
                abstain_count=abstain_count,
                decision_reason="insufficient_casts",
            )
        if reject_count >= reject_quorum:
            return CheckedFactGroup(
                group_id=group.group_id,
                status="rejected",
                cast_count=cast_count,
                tally=tally,
                reject_count=reject_count,
                abstain_count=abstain_count,
                decision_reason="reject_quorum",
            )
        return CheckedFactGroup(
            group_id=group.group_id,
            status="upheld",
            cast_count=cast_count,
            tally=tally,
            reject_count=reject_count,
            abstain_count=abstain_count,
            decision_reason="upheld",
        )


def aggregate_jury_rulings(
    *,
    group: FactGroup,
    rulings: list[JuryRuling] | tuple[JuryRuling, ...],
    limits: DeepResearchLimits,
) -> CheckedFactGroup:
    """函数式聚合入口，输入为 group/rulings/limits，输出为 CheckedFactGroup。"""
    return AdversarialJury().aggregate_rulings(group=group, rulings=rulings, limits=limits)


def build_fallback_votes(*, jury_size: int, decision: str = "uphold") -> tuple[JuryRuling, ...]:
    """生成确定性兜底投票，输入为陪审数量和决策，输出为投票列表。"""
    if jury_size <= 0:
        raise ValueError("jury_size must be > 0")
    normalized = decision if decision in {"uphold", "reject", "abstain"} else "uphold"
    return tuple(
        JuryRuling(
            ruling_id=f"fallback-ruling-{index}",
            group_id="",
            juror_id=f"fallback-juror-{index}",
            reject=normalized == "reject",
            abstain=normalized == "abstain",
            reason="deterministic fallback",
        )
        for index in range(1, jury_size + 1)
    )


__all__ = ["AdversarialJury", "aggregate_jury_rulings", "build_fallback_votes"]
