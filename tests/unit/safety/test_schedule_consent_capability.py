"""unit：v0.2 agent-cron-module 模块 6 —— ``schedule`` capability 默认规则。

覆盖 :mod:`safety.approval.default_rules` 新增的 :data:`DEFAULT_CONSENT_THEN_TRUST_TOOLS`
锚点常量与 ``schedule`` tool 走 ConsentResolver 的 standard ask 路径：

1. ``DEFAULT_CONSENT_THEN_TRUST_TOOLS`` 含 ``"memory"`` 与 ``"schedule"``（锚点）
2. ``schedule`` tool 调用走 ConsentResolver fallback → severity=standard
3. ``schedule`` tool 走 standard ask 不携带 confirm_token
4. ``schedule`` tool 走 standard ask 时 enriched request 含 policy_hint=standard
5. SafetyGatedApproval：``schedule`` tool 经 explicit_consent 路径返回
   ``grant_scope=session`` 时写 ``Grant(capability="tool:schedule")`` 到 GrantStore，
   下一轮 TrustResolver 通配查询命中 → silent_allow（"首次 consent → 后续 trust"
   闭环回归）

不破坏既有 capability 锚点：

6. ``DEFAULT_HARD_DENY_COMMANDS`` / ``DEFAULT_APPROVAL_REQUIRED_COMMANDS`` /
   ``DEFAULT_SENSITIVE_PATHS`` / ``DEFAULT_SKILL_CALL_RULES`` / 现有
   canonical consent-then-trust 清单保持稳定（回归锚点）
"""

from __future__ import annotations

from typing import Any

import pytest

from core.contracts import ApprovalDecision, ApprovalRequest
from infrastructure.config.models import ApprovalConfig, Config, ModelSelectionConfig
from safety.approval.default_rules import (
    DEFAULT_APPROVAL_REQUIRED_COMMANDS,
    DEFAULT_CONSENT_THEN_TRUST_TOOLS,
    DEFAULT_HARD_DENY_COMMANDS,
    DEFAULT_SENSITIVE_PATHS,
    DEFAULT_SKILL_CALL_RULES,
)
from safety.approval.types import (
    ApprovalMetadataKeys,
    BoundaryDecision,
    BoundaryKind,
    BoundaryZone,
    RuntimeBoundaryContext,
)
from safety.guards.consent import ConsentResolver

# ---------------------------------------------------------------------------
# Fixtures / helpers（参考 test_safety_consent_resolver.py 风格）
# ---------------------------------------------------------------------------


def _cfg() -> Config:
    return Config(
        model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"),
        approval=ApprovalConfig(mode="interactive"),
    )


def _runtime() -> RuntimeBoundaryContext:
    return RuntimeBoundaryContext(boundary_kind=BoundaryKind.HOST)


def _approval_zone() -> BoundaryDecision:
    return BoundaryDecision(zone=BoundaryZone.APPROVAL, matched_rule=None, reason="approval")


def _req(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    call_id: str = "call-sched-1",
    metadata: dict[str, Any] | None = None,
) -> ApprovalRequest:
    return ApprovalRequest(
        run_id="r1",
        session_id="s1",
        turn=1,
        call_id=call_id,
        tool_name=tool_name,
        arguments=arguments,
        metadata=dict(metadata or {}),
    )


class FakeApproval:
    """模拟 InteractiveApproval：记录 enriched request 并按预设返回。"""

    def __init__(
        self,
        *,
        outcome: str = "approved",
        downstream_metadata: dict[str, Any] | None = None,
    ) -> None:
        self._outcome = outcome
        self._downstream_metadata: dict[str, Any] = dict(downstream_metadata or {})
        self.decided_requests: list[ApprovalRequest] = []

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        self.decided_requests.append(request)
        return ApprovalDecision(
            outcome=self._outcome,  # type: ignore[arg-type]
            reason="fake decided",
            metadata=dict(self._downstream_metadata),
        )


def _make_resolver(
    *,
    fake: FakeApproval | None = None,
) -> tuple[ConsentResolver, FakeApproval]:
    fake = fake or FakeApproval()
    resolver = ConsentResolver.from_config(_cfg(), interactive_approval=fake)
    return resolver, fake


# ---------------------------------------------------------------------------
# §1 DEFAULT_CONSENT_THEN_TRUST_TOOLS 锚点
# ---------------------------------------------------------------------------


def test_default_consent_then_trust_tools_includes_memory_and_schedule() -> None:
    """``schedule`` 与 ``memory`` 同款风格登记到锚点常量。

    本常量是文档锚点：实际"首次 consent → 后续 trust"的语义由 ConsentResolver
    fallback + SafetyGatedApproval 写 grant + TrustResolver 通配三件套合作实现，
    本测试锁定登记不被无声破坏。
    """
    assert isinstance(DEFAULT_CONSENT_THEN_TRUST_TOOLS, tuple)
    assert "memory" in DEFAULT_CONSENT_THEN_TRUST_TOOLS
    assert "schedule" in DEFAULT_CONSENT_THEN_TRUST_TOOLS


def test_default_consent_then_trust_tools_str_only() -> None:
    """常量内容只放 tool_name 字符串（避免有人塞 dataclass 进来破坏锚点语义）。"""
    assert all(isinstance(t, str) and t.strip() for t in DEFAULT_CONSENT_THEN_TRUST_TOOLS)


# ---------------------------------------------------------------------------
# §2 schedule tool 走 ConsentResolver fallback → standard ask
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_schedule_tool_routes_to_standard_consent() -> None:
    """schedule capability 默认规则：fallback unknown-tool-default → severity=standard。

    ConsentResolver 不为 schedule 写专用分支；命中 ``unknown-tool-default``
    fallback 与 memory 一致。
    """
    resolver, fake = _make_resolver()
    decision = await resolver.evaluate(
        _req(
            tool_name="schedule",
            arguments={"action": "create", "name": "daily-report", "schedule": "every 1d"},
        ),
        _approval_zone(),
        _runtime(),
    )

    md = decision.metadata
    assert decision.outcome == "approved"
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "explicit_consent"
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "standard"
    assert md[ApprovalMetadataKeys.STAGE] == "permission"
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "unknown-tool-default"
    assert md[ApprovalMetadataKeys.BOUNDARY_KIND] == "host"
    # standard 不携带 confirm_token
    assert "confirm_token" not in md

    # InteractiveApproval 收到 enriched request
    assert len(fake.decided_requests) == 1
    enriched = fake.decided_requests[0]
    assert enriched.metadata["policy_hint"] == "standard"
    assert enriched.metadata["severity"] == "standard"
    assert "confirm_token" not in enriched.metadata


@pytest.mark.unit
@pytest.mark.asyncio
async def test_schedule_tool_consent_passes_through_rejection() -> None:
    """用户在 InteractiveApproval 里 reject ⇒ ConsentResolver 透传 outcome=rejected。

    schedule capability 没有 elevated 强度，severity 始终 standard；用户拒绝后
    决策 metadata 仍然挂上 explicit_consent / standard 元数据。
    """
    fake = FakeApproval(outcome="rejected")
    resolver, _ = _make_resolver(fake=fake)
    decision = await resolver.evaluate(
        _req(
            tool_name="schedule",
            arguments={"action": "remove", "name": "daily-report"},
        ),
        _approval_zone(),
        _runtime(),
    )

    md = decision.metadata
    assert decision.outcome == "rejected"
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "explicit_consent"
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "standard"


# ---------------------------------------------------------------------------
# §4 现有 capability 规则不受影响（回归锚点）
# ---------------------------------------------------------------------------


def test_existing_default_tables_unaffected_by_consent_then_trust_tools() -> None:
    """新增 ``DEFAULT_CONSENT_THEN_TRUST_TOOLS`` 不破坏现有四张默认规则表与
    canonical consent-then-trust 集合。

    锁定锚点：
    - hard_deny_commands 仍含 ``host-root-delete``
    - approval_required_commands 仍含 ``git-push`` profile
    - sensitive_paths 仍含 ``ssh-material`` block + ``project-env`` elevated
    - skill_call_rules 仍是空 tuple（deny-by-default）
    - allow_tools_silent 含低风险只读工具和 agent role 工具，没有把
      ``schedule`` / ``memory`` 误塞进去
    """
    hard_deny_names = {r.name for r in DEFAULT_HARD_DENY_COMMANDS}
    assert "host-root-delete" in hard_deny_names

    approval_required_names = {r.name for r in DEFAULT_APPROVAL_REQUIRED_COMMANDS}
    assert "git-push" in approval_required_names

    sensitive_block_matchers = {r.matcher for r in DEFAULT_SENSITIVE_PATHS if r.effect == "block"}
    assert "~/.ssh/" in sensitive_block_matchers
    sensitive_elevated_matchers = {
        r.matcher for r in DEFAULT_SENSITIVE_PATHS if r.effect == "elevated"
    }
    assert ".env*" in sensitive_elevated_matchers

    assert DEFAULT_SKILL_CALL_RULES == ()

    assert set(DEFAULT_CONSENT_THEN_TRUST_TOOLS) == {"schedule", "memory"}
