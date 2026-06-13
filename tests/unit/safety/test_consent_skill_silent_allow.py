"""unit：v0.1.6 skill-system 模块 C —— ConsentResolver.evaluate silent_allow 主流程分流。

覆盖目标：

- skill 命中 ``effect=allow`` → ``decision_class=silent_allow`` + 不调
  ``InteractiveApproval``
- skill 命中 ``effect=block`` / ``effect=elevated`` → 降到主流程，
  ``InteractiveApproval`` 收到 elevated severity
- skill 未命中任何 rule → ``InteractiveApproval`` 收到 standard severity（deny-by-default）
"""

from __future__ import annotations

from typing import Any

from core.contracts import ApprovalDecision, ApprovalRequest
from safety.approval.types import (
    ApprovalMetadataKeys,
    BoundaryDecision,
    BoundaryKind,
    BoundaryScope,
    BoundaryZone,
    RuntimeBoundaryContext,
    SkillCallRule,
)
from safety.guards.consent import ConsentResolver

# ---------------------------------------------------------------------------
# Fixtures / spies
# ---------------------------------------------------------------------------


def _runtime() -> RuntimeBoundaryContext:
    return RuntimeBoundaryContext(boundary_kind=BoundaryKind.HOST)


def _approval_zone() -> BoundaryDecision:
    return BoundaryDecision(zone=BoundaryZone.APPROVAL, matched_rule=None, reason="approval")


def _skill_request(
    *,
    skill: str,
    call_id: str = "call-1",
    extra_args: dict[str, Any] | None = None,
) -> ApprovalRequest:
    args: dict[str, Any] = {"skill": skill}
    if extra_args:
        args.update(extra_args)
    return ApprovalRequest(
        run_id="r1",
        session_id="s1",
        turn=1,
        call_id=call_id,
        tool_name="skill",
        arguments=args,
        metadata={},
    )


class _SpyApproval:
    """spy ApprovalProvider：记录每次 decide 的入参；默认 outcome=approved。"""

    def __init__(self) -> None:
        self.calls: list[ApprovalRequest] = []

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        self.calls.append(request)
        return ApprovalDecision(
            outcome="approved",
            reason="spy approved",
            metadata={"source": "spy"},
        )


def _resolver_with_rules(
    rules: tuple[SkillCallRule, ...],
    spy: _SpyApproval,
) -> ConsentResolver:
    return ConsentResolver(
        approval_required_commands=(),
        sensitive_paths=(),
        skill_call_rules=rules,
        interactive_approval=spy,
    )


# ---------------------------------------------------------------------------
# §1 allow → silent_allow，不调 InteractiveApproval
# ---------------------------------------------------------------------------


async def test_evaluate_skill_allow_returns_silent_allow() -> None:
    """配 allow 规则 → ApprovalDecision.outcome=approved + decision_class=silent_allow，
    InteractiveApproval.decide 不被调到。"""
    rules = (
        SkillCallRule(
            name="trust-doc",
            matcher="doc-builder",
            match_mode="exact",
            effect="allow",
            boundary_scope=BoundaryScope.ANY,
            reason="trusted documentation skill",
        ),
    )
    spy = _SpyApproval()
    resolver = _resolver_with_rules(rules, spy)

    decision = await resolver.evaluate(
        _skill_request(skill="doc-builder"),
        _approval_zone(),
        _runtime(),
    )

    # 决策正确
    assert decision.outcome == "approved"
    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "silent_allow"
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "config"
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "trust-doc"
    assert md[ApprovalMetadataKeys.REASON] == "trusted documentation skill"
    assert md[ApprovalMetadataKeys.BOUNDARY_KIND] == "host"
    # silent_allow 不带 confirm_token
    assert "confirm_token" not in md

    # InteractiveApproval 没被调到
    assert spy.calls == []


# ---------------------------------------------------------------------------
# §2 block → InteractiveApproval 收到 elevated severity（reason 带 block: 前缀）
# ---------------------------------------------------------------------------


async def test_evaluate_skill_block_routes_to_elevated_approval() -> None:
    rules = (
        SkillCallRule(
            name="block-net",
            matcher="net-tool",
            match_mode="exact",
            effect="block",
            boundary_scope=BoundaryScope.ANY,
            reason="network tool blocked",
        ),
    )
    spy = _SpyApproval()
    resolver = _resolver_with_rules(rules, spy)

    decision = await resolver.evaluate(
        _skill_request(skill="net-tool"),
        _approval_zone(),
        _runtime(),
    )

    # InteractiveApproval 被调到一次，且收到 elevated enriched request
    assert len(spy.calls) == 1
    enriched = spy.calls[0]
    assert enriched.metadata["policy_hint"] == "elevated"
    assert enriched.metadata["severity"] == "elevated"
    assert "confirm_token" in enriched.metadata

    # 最终 decision metadata 装饰为 explicit_consent / elevated
    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "explicit_consent"
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "elevated"
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "block-net"
    # block 规则的 reason 装饰带 "block:" 前缀
    assert md[ApprovalMetadataKeys.REASON].startswith("block:")
    assert "confirm_token" in md


# ---------------------------------------------------------------------------
# §3 elevated → InteractiveApproval 收到 elevated（reason 不带 block: 前缀）
# ---------------------------------------------------------------------------


async def test_evaluate_skill_elevated_routes_to_elevated_approval() -> None:
    rules = (
        SkillCallRule(
            name="elevated-deploy",
            matcher="deploy-",
            match_mode="prefix",
            effect="elevated",
            boundary_scope=BoundaryScope.ANY,
            reason="deploy needs elevated",
        ),
    )
    spy = _SpyApproval()
    resolver = _resolver_with_rules(rules, spy)

    decision = await resolver.evaluate(
        _skill_request(skill="deploy-prod"),
        _approval_zone(),
        _runtime(),
    )

    assert len(spy.calls) == 1
    enriched = spy.calls[0]
    assert enriched.metadata["policy_hint"] == "elevated"
    assert enriched.metadata["severity"] == "elevated"

    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "explicit_consent"
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "elevated"
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "elevated-deploy"
    # elevated 规则的 reason 不带 block: 前缀
    assert not md[ApprovalMetadataKeys.REASON].startswith("block:")
    assert md[ApprovalMetadataKeys.REASON] == "deploy needs elevated"


# ---------------------------------------------------------------------------
# §4 default → InteractiveApproval 收到 standard severity（deny-by-default）
# ---------------------------------------------------------------------------


async def test_evaluate_skill_default_routes_to_standard_approval() -> None:
    """无规则命中 → InteractiveApproval 收到 standard 强度，matched_rule=skill-call-default。"""
    spy = _SpyApproval()
    resolver = _resolver_with_rules((), spy)

    decision = await resolver.evaluate(
        _skill_request(skill="random-skill"),
        _approval_zone(),
        _runtime(),
    )

    assert len(spy.calls) == 1
    enriched = spy.calls[0]
    assert enriched.metadata["policy_hint"] == "standard"
    assert enriched.metadata["severity"] == "standard"
    # standard 不带 confirm_token
    assert "confirm_token" not in enriched.metadata

    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "explicit_consent"
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "standard"
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "skill-call-default"
    assert md[ApprovalMetadataKeys.STAGE] == "permission"
    assert "confirm_token" not in md
