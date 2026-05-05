"""unit：v0.2 agent-cron-module 模块 6 —— ``schedule`` capability 默认规则。

覆盖 :mod:`safety.default_rules` 新增的 :data:`DEFAULT_CONSENT_THEN_TRUST_TOOLS`
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
   ``DEFAULT_ALLOW_TOOLS_SILENT`` 不被新增常量影响（回归锚点）
"""

from __future__ import annotations

from typing import Any

import pytest

from config_loader.models import ApprovalConfig, Config, ModelConfig
from core.contracts import ApprovalDecision, ApprovalRequest
from safety.default_rules import (
    DEFAULT_ALLOW_TOOLS_SILENT,
    DEFAULT_APPROVAL_REQUIRED_COMMANDS,
    DEFAULT_CONSENT_THEN_TRUST_TOOLS,
    DEFAULT_HARD_DENY_COMMANDS,
    DEFAULT_SENSITIVE_PATHS,
    DEFAULT_SKILL_CALL_RULES,
)
from safety.guards.consent import ConsentResolver
from safety.types import (
    ApprovalMetadataKeys,
    BoundaryDecision,
    BoundaryKind,
    BoundaryZone,
    RuntimeBoundaryContext,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers（参考 test_safety_consent_resolver.py 风格）
# ---------------------------------------------------------------------------


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
# §3 SafetyGatedApproval 闭环：consent → grant 写回 GrantStore（capability=tool:schedule）
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_schedule_tool_consent_writes_session_grant(tmp_path: Any) -> None:
    """schedule capability 闭环第一段：首次 standard consent + 用户授予
    session grant 后，:class:`SafetyGatedApproval._maybe_write_session_grant`
    把一条 ``GrantKey(capability='tool:schedule', matcher='*')`` 写入
    GrantStore 的 session 桶。

    与 memory tool 同款（Grant capability 命名约定参考 ``chain.py``
    ``SafetyGatedApproval._maybe_write_session_grant``）。

    Known gap：当前 :class:`TrustResolver._lookup_grant` 在
    schedule/memory 这类"非 file/shell"工具上会因 ``_derive_capability_and_target``
    返回 ``(None, None)`` 而提前 return None，未触达第 2 步的 ``tool:`` 通配
    查询；该缺口与 memory tool 共有，由 trust.py 修复（不在本任务范围）。
    本测试只锁定 grant **写入**正确，不假设 trust 第二轮 silent_allow。
    """
    from safety.boundary_resolver import BoundaryResolver
    from safety.chain import SafetyGatedApproval
    from safety.decision_engine import SafetyDecisionEngine
    from safety.grant_store import GrantStore
    from safety.guards.hard_block import HardBlockGuard
    from safety.guards.trust import TrustResolver
    from safety.types import BoundaryKind, GrantKey

    cfg = _cfg()
    # FakeApproval 模拟用户首次点"本 session 同意"：返回 grant_scope=session
    fake = FakeApproval(
        outcome="approved",
        downstream_metadata={ApprovalMetadataKeys.GRANT_SCOPE: "session"},
    )
    boundary = BoundaryResolver.from_project_root(project_root=tmp_path)
    grants = GrantStore.from_config(cfg)
    hard_block = HardBlockGuard.from_config(cfg)
    trust = TrustResolver(boundary, grants)
    consent = ConsentResolver.from_config(cfg, interactive_approval=fake)
    engine = SafetyDecisionEngine(
        hard_block=hard_block,
        boundary=boundary,
        trust=trust,
        consent=consent,
    )
    gated = SafetyGatedApproval(engine=engine, grant_store=grants)

    first_request = _req(
        tool_name="schedule",
        arguments={"action": "create", "name": "daily", "schedule": "every 1h"},
        call_id="call-1",
    )
    first_decision = await gated.decide(first_request)
    # 首次走 explicit_consent / standard
    assert first_decision.outcome == "approved"
    md1 = first_decision.metadata
    assert md1[ApprovalMetadataKeys.DECISION_CLASS] == "explicit_consent"
    assert md1[ApprovalMetadataKeys.DECISION_SOURCE] == "standard"
    assert md1[ApprovalMetadataKeys.GRANT_SCOPE] == "session"
    assert len(fake.decided_requests) == 1

    # 关键断言：grant 已被写入 session 桶，capability=tool:schedule，matcher=*
    expected_key = GrantKey(
        capability="tool:schedule",
        matcher="*",
        boundary_kind=BoundaryKind.HOST,
    )
    grant = grants.get(expected_key, session_id="s1")
    assert grant is not None, "session grant for schedule should be written by SafetyGatedApproval"
    assert grant.scope == "session"
    assert grant.session_id == "s1"
    # 跨 session 严格隔离：另一 session_id 不应命中
    assert grants.get(expected_key, session_id="other") is None


# ---------------------------------------------------------------------------
# §4 现有 capability 规则不受影响（回归锚点）
# ---------------------------------------------------------------------------


def test_existing_default_tables_unaffected_by_consent_then_trust_tools() -> None:
    """新增 ``DEFAULT_CONSENT_THEN_TRUST_TOOLS`` 不破坏现有四张默认规则表与
    ``DEFAULT_ALLOW_TOOLS_SILENT`` 集合。

    锁定锚点：
    - hard_deny_commands 仍含 ``host-root-delete``
    - approval_required_commands 仍含 ``git-push`` profile
    - sensitive_paths 仍含 ``ssh-material`` block + ``project-env`` elevated
    - skill_call_rules 仍是空 tuple（deny-by-default）
    - allow_tools_silent 仍只含 ``read_file`` / ``list_dir``，没有把
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

    assert set(DEFAULT_ALLOW_TOOLS_SILENT) == {"read_file", "list_dir"}
    # 关键回归：schedule / memory 不应被错误地放进 allow_tools_silent
    # （否则用户首次也不会被询问，破坏 consent-then-trust 语义）
    assert "schedule" not in DEFAULT_ALLOW_TOOLS_SILENT
    assert "memory" not in DEFAULT_ALLOW_TOOLS_SILENT
