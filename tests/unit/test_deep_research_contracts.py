"""Deep Research contracts 单元测试。

本脚本验证 DeepResearchContractParser 的 payload 校验、默认 limits、非法 quorum 和预算上限。
作用是把 deep_research 策略的输入边界固定在 contracts 层，避免 tool、strategy 和 artifact 各自解释 payload。
关键执行流程：动态读取 contracts 模块，解析最小 payload，再覆盖空字段、非法 quorum 和 MiMo 上限。
关键函数：_parser 构造 parser，_field 读取 dataclass 或 dict 字段，test_* 覆盖合同边界。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

import pytest


def test_parse_deep_research_spec_accepts_minimal_topic_and_defaults() -> None:
    """验证最小 payload，输入为 topic-only，输出为默认 limits/source_policy/output_contract。"""
    spec = _parser().parse({"topic": "Kongming Deep Research"})

    assert _field(spec, "topic") == "Kongming Deep Research"
    assert _field(spec, "objective") == "Kongming Deep Research"
    assert _field(spec, "output_contract") == "deep_research_report"

    limits = _field(spec, "limits")
    assert _field(limits, "jury_size") == 3
    assert _field(limits, "reject_quorum") == 2
    assert _field(limits, "source_budget") == 10
    assert _field(limits, "fact_cap") == 20
    assert _field(limits, "search_results_per_line") == 6
    assert _field(limits, "fetch_concurrency") == 4
    assert _field(limits, "jury_concurrency") == 6
    assert _field(limits, "workflow_timeout_seconds") == 2400

    source_policy = _field(spec, "source_policy")
    assert _field(source_policy, "language") == "zh-CN"
    assert _field(source_policy, "freshness_days") is None
    assert _sequence(_field(source_policy, "allowed_domains")) == []
    assert _sequence(_field(source_policy, "blocked_domains")) == []
    assert _field(source_policy, "prefer_primary_sources") is True
    assert _field(source_policy, "provider") == "internal"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"topic": ""}, "topic"),
        ({"topic": "   "}, "topic"),
        ({"topic": "ok", "objective": ""}, "objective"),
        ({"topic": "ok", "objective": "   "}, "objective"),
    ],
)
def test_parse_deep_research_spec_rejects_empty_topic_or_objective(
    payload: dict[str, object],
    message: str,
) -> None:
    """验证空 topic/objective，输入为空白字段，输出为合同错误。"""
    with pytest.raises(_contract_error(), match=message):
        _parser().parse(payload)


@pytest.mark.parametrize(
    "limits",
    [
        {"jury_size": 3, "reject_quorum": 0},
        {"jury_size": 3, "reject_quorum": 4},
        {"jury_size": 0, "reject_quorum": 1},
    ],
)
def test_parse_deep_research_spec_rejects_illegal_quorum(
    limits: dict[str, int],
) -> None:
    """验证非法 quorum，输入为越界 jury 配置，输出为合同错误。"""
    with pytest.raises(_contract_error(), match=r"reject_quorum|jury_size"):
        _parser().parse({"topic": "ok", "limits": limits})


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        ({"source_budget": 0}, "source_budget"),
        ({"source_budget": 16}, "source_budget"),
        ({"fact_cap": 0}, "fact_cap"),
        ({"fact_cap": 26}, "fact_cap"),
    ],
)
def test_parse_deep_research_spec_enforces_budget_bounds(
    limits: dict[str, int],
    message: str,
) -> None:
    """验证预算上限，输入为 0 或超过 MiMo 上限的预算，输出为合同错误。"""
    with pytest.raises(_contract_error(), match=message):
        _parser().parse({"topic": "ok", "limits": limits})


def test_parse_deep_research_spec_accepts_mimo_budget_caps() -> None:
    """验证 MiMo 上限，输入为 source_budget=15/fact_cap=25，输出为保留覆盖值。"""
    spec = _parser().parse(
        {
            "topic": "ok",
            "limits": {
                "source_budget": 15,
                "fact_cap": 25,
                "jury_size": 3,
                "reject_quorum": 2,
            },
        }
    )

    limits = _field(spec, "limits")
    assert _field(limits, "source_budget") == 15
    assert _field(limits, "fact_cap") == 25


@pytest.mark.parametrize("output_contract", ["", "   ", None, 123, "other_contract"])
def test_parse_deep_research_spec_rejects_invalid_output_contract(
    output_contract: object,
) -> None:
    """验证输出合同固定，输入为非法 output_contract，输出为合同错误。"""
    with pytest.raises(_contract_error(), match="output_contract"):
        _parser().parse({"topic": "ok", "output_contract": output_contract})


def _contracts_module() -> Any:
    """读取 contracts 模块，输入为空，输出为模块对象。"""
    return import_module("application.agent_workflows.strategies.deep_research.contracts")


def _parser() -> Any:
    """构造 DeepResearchContractParser，输入为空，输出为 parser 实例。"""
    parser_cls = getattr(_contracts_module(), "DeepResearchContractParser")
    return parser_cls()


def _contract_error() -> type[Exception]:
    """读取合同错误类型，输入为空，输出为异常类。"""
    return getattr(_contracts_module(), "DeepResearchContractError", ValueError)


def _field(value: Any, name: str) -> Any:
    """读取对象字段，输入为 dataclass 或 dict，输出为对应字段值。"""
    if isinstance(value, dict):
        return value[name]
    return getattr(value, name)


def _sequence(value: Any) -> list[Any]:
    """转为 list 便于比较，输入为 tuple/list，输出为 list。"""
    return list(value)
