"""Deep Research jury 单元测试。

本脚本验证 AdversarialJury 的弃权、有效票、否决阈值和 tally 聚合。
作用是把 deep_research Crosscheck 阶段的可预测裁决规则固定为纯函数式边界。
关键执行流程：构造 FactGroup、DeepResearchLimits 和 JuryRuling 列表，调用聚合入口，断言 CheckedFactGroup。
关键函数：_aggregate 调用实现提供的聚合入口，_ruling 构造 juror 裁决，test_* 覆盖三类 quorum。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


def test_jury_all_abstain_rejects_for_insufficient_casts() -> None:
    """验证全弃权，输入为 3 个 abstain，输出为 rejected/insufficient_casts。"""
    checked = _aggregate(
        _group(),
        [
            _ruling("j1", reject=False, abstain=True),
            _ruling("j2", reject=False, abstain=True),
            _ruling("j3", reject=False, abstain=True),
        ],
        _limits(),
    )

    assert _field(checked, "status") == "rejected"
    assert _field(checked, "cast_count") == 0
    assert _field(checked, "reject_count") == 0
    assert _field(checked, "abstain_count") == 3
    assert _field(checked, "tally") == "0-0"
    assert _field(checked, "decision_reason") == "insufficient_casts"


def test_jury_partial_abstain_upholds_when_cast_quorum_passes() -> None:
    """验证部分弃权，输入为 2 个通过和 1 个弃权，输出为 upheld 和 2-0 tally。"""
    checked = _aggregate(
        _group(),
        [
            _ruling("j1", reject=False, abstain=False),
            _ruling("j2", reject=False, abstain=False),
            _ruling("j3", reject=False, abstain=True),
        ],
        _limits(),
    )

    assert _field(checked, "status") == "upheld"
    assert _field(checked, "cast_count") == 2
    assert _field(checked, "reject_count") == 0
    assert _field(checked, "abstain_count") == 1
    assert _field(checked, "tally") == "2-0"
    assert _field(checked, "decision_reason") == "upheld"


def test_jury_all_rejects_returns_rejected_with_reject_quorum() -> None:
    """验证全部否决，输入为 3 个 reject，输出为 rejected/reject_quorum。"""
    checked = _aggregate(
        _group(),
        [
            _ruling("j1", reject=True, abstain=False),
            _ruling("j2", reject=True, abstain=False),
            _ruling("j3", reject=True, abstain=False),
        ],
        _limits(),
    )

    assert _field(checked, "status") == "rejected"
    assert _field(checked, "cast_count") == 3
    assert _field(checked, "reject_count") == 3
    assert _field(checked, "abstain_count") == 0
    assert _field(checked, "tally") == "0-3"
    assert _field(checked, "decision_reason") == "reject_quorum"


def _contracts_module() -> Any:
    """读取 contracts 模块，输入为空，输出为模块对象。"""
    return import_module("application.agent_workflows.strategies.deep_research.contracts")


def _jury_module() -> Any:
    """读取 jury 模块，输入为空，输出为模块对象。"""
    return import_module("application.agent_workflows.strategies.deep_research.jury")


def _group() -> Any:
    """构造 FactGroup，输入为空，输出为单个事实组。"""
    group_cls = getattr(_contracts_module(), "FactGroup")
    return group_cls(
        group_id="G-001",
        canonical_statement="Kongming Deep Research has a deterministic jury.",
        member_fact_ids=("F-001",),
        source_ids=("S-001",),
        best_excerpt="The workflow uses an adversarial jury.",
        support_count=1,
    )


def _limits() -> Any:
    """构造 DeepResearchLimits，输入为空，输出为 3/2 jury 配置。"""
    limits_cls = getattr(_contracts_module(), "DeepResearchLimits")
    return limits_cls(jury_size=3, reject_quorum=2)


def _ruling(juror_id: str, *, reject: bool, abstain: bool) -> Any:
    """构造 JuryRuling，输入为 juror 和投票，输出为裁决对象。"""
    ruling_cls = getattr(_contracts_module(), "JuryRuling")
    return ruling_cls(
        ruling_id=f"R-{juror_id}",
        group_id="G-001",
        juror_id=juror_id,
        reject=reject,
        abstain=abstain,
        reason="deterministic unit vote",
        contradicting_evidence=(),
        source_coverage="checked",
    )


def _aggregate(group: Any, rulings: list[Any], limits: Any) -> Any:
    """调用 jury 聚合入口，输入为 group/rulings/limits，输出为 CheckedFactGroup。"""
    jury_module = _jury_module()
    aggregate = getattr(jury_module, "aggregate_jury_rulings", None)
    if aggregate is not None:
        return aggregate(group=group, rulings=rulings, limits=limits)

    jury_cls = getattr(jury_module, "AdversarialJury")
    jury = jury_cls()
    return jury.aggregate_rulings(group=group, rulings=rulings, limits=limits)


def _field(value: Any, name: str) -> Any:
    """读取对象字段，输入为 dataclass 或 dict，输出为字段值。"""
    if isinstance(value, dict):
        return value[name]
    return getattr(value, name)
