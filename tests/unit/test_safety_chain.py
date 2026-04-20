"""unit：safety.chain 三层串接顺序。

验证 :class:`SafetyGatedApproval` 按 capability → permission → approval 顺序
给出判定，并在 metadata.stage 上标注决定来源。
"""

from __future__ import annotations

import pytest

from config_loader.models import ApprovalConfig, Config, ModelConfig
from core.contracts import ApprovalDecision, ApprovalRequest
from safety.capability_policy import CapabilityPolicy, CapabilitySet
from safety.chain import SafetyGatedApproval, build_safety_chain
from safety.permission_policy import PermissionPolicy, PermissionRule


class _FixedApproval:
    """底层占位 ApprovalProvider：按构造参数返回固定 outcome。"""

    def __init__(self, outcome: str = "approved") -> None:
        self._outcome = outcome
        self.requests: list[ApprovalRequest] = []

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(outcome=self._outcome, reason=f"fixed-{self._outcome}")  # type: ignore[arg-type]


def _req(tool_name: str = "read_file") -> ApprovalRequest:
    return ApprovalRequest(
        run_id="r",
        session_id="s",
        turn=1,
        call_id="c",
        tool_name=tool_name,
    )


def _cfg() -> Config:
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="m",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        approval=ApprovalConfig(mode="interactive"),
    )


# ---------------------------------------------------------------------------
# capability 层短路
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_capability_deny_short_circuits_to_rejected() -> None:
    cap = CapabilityPolicy(CapabilitySet(deny=frozenset({"file_read"})))
    perm = PermissionPolicy(default_outcome="allow")  # 不会被问到
    underlying = _FixedApproval("approved")

    chain = SafetyGatedApproval(
        capability_policy=cap,
        permission_policy=perm,
        interactive_approval=underlying,
    )
    decision = await chain.decide(_req("read_file"))

    assert decision.outcome == "rejected"
    assert decision.metadata.get("stage") == "capability"
    # 底层 approval 不应该被调用
    assert underlying.requests == []


# ---------------------------------------------------------------------------
# permission 层短路
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_permission_allow_short_circuits_to_approved() -> None:
    cap = CapabilityPolicy(CapabilitySet())  # 全开
    perm = PermissionPolicy(default_outcome="allow")
    underlying = _FixedApproval("rejected")  # 不应该被问到

    chain = SafetyGatedApproval(
        capability_policy=cap,
        permission_policy=perm,
        interactive_approval=underlying,
    )
    decision = await chain.decide(_req())

    assert decision.outcome == "approved"
    assert decision.metadata.get("stage") == "permission"
    assert underlying.requests == []


@pytest.mark.unit
async def test_permission_deny_short_circuits_to_rejected() -> None:
    cap = CapabilityPolicy(CapabilitySet())
    perm = PermissionPolicy(default_outcome="deny")
    underlying = _FixedApproval("approved")

    chain = SafetyGatedApproval(
        capability_policy=cap,
        permission_policy=perm,
        interactive_approval=underlying,
    )
    decision = await chain.decide(_req())

    assert decision.outcome == "rejected"
    assert decision.metadata.get("stage") == "permission"
    assert underlying.requests == []


# ---------------------------------------------------------------------------
# ask → 落到底层 approval
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_permission_ask_passes_to_underlying_approval() -> None:
    cap = CapabilityPolicy(CapabilitySet())
    perm = PermissionPolicy(default_outcome="ask")
    underlying = _FixedApproval("approved")

    chain = SafetyGatedApproval(
        capability_policy=cap,
        permission_policy=perm,
        interactive_approval=underlying,
    )
    decision = await chain.decide(_req())

    assert decision.outcome == "approved"
    assert decision.metadata.get("stage") == "approval"
    assert len(underlying.requests) == 1


@pytest.mark.unit
async def test_permission_ask_rejected_by_underlying() -> None:
    cap = CapabilityPolicy(CapabilitySet())
    perm = PermissionPolicy(default_outcome="ask")
    underlying = _FixedApproval("rejected")

    chain = SafetyGatedApproval(
        capability_policy=cap,
        permission_policy=perm,
        interactive_approval=underlying,
    )
    decision = await chain.decide(_req())

    assert decision.outcome == "rejected"
    assert len(underlying.requests) == 1


# ---------------------------------------------------------------------------
# build_safety_chain
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_build_safety_chain_returns_gated_approval() -> None:
    underlying = _FixedApproval("approved")
    chain = build_safety_chain(_cfg(), interactive_approval=underlying)
    assert isinstance(chain, SafetyGatedApproval)
    # 行为 smoke：从配置装出来的 chain 可用
    decision = await chain.decide(_req())
    assert decision.outcome in {"approved", "rejected"}


@pytest.mark.unit
async def test_build_safety_chain_respects_explicit_policies() -> None:
    cap = CapabilityPolicy(CapabilitySet(deny=frozenset({"file_read"})))
    underlying = _FixedApproval("approved")
    chain = build_safety_chain(
        _cfg(),
        interactive_approval=underlying,
        capability_policy=cap,
    )
    decision = await chain.decide(_req("read_file"))
    assert decision.outcome == "rejected"
    assert decision.metadata.get("stage") == "capability"


@pytest.mark.unit
async def test_permission_rule_match_gets_reflected_in_metadata() -> None:
    """命中 rule 时 metadata.matched_rule 应包含规则摘要。"""
    cap = CapabilityPolicy(CapabilitySet())
    rule = PermissionRule(tool_name="*", outcome="allow", reason="blanket")
    perm = PermissionPolicy([rule])
    chain = SafetyGatedApproval(
        capability_policy=cap,
        permission_policy=perm,
        interactive_approval=_FixedApproval("rejected"),
    )
    decision = await chain.decide(_req("anything"))
    assert decision.outcome == "approved"
    matched = decision.metadata.get("matched_rule")
    assert matched is not None
    assert matched["tool_name"] == "*"
    assert matched["outcome"] == "allow"
