"""unit：safety.approval.chain v0.1.4 薄壳形态 + build_safety_chain 装配。

验证：

- :class:`SafetyGatedApproval` 调 :class:`SafetyDecisionEngine` 后装饰
  ``metadata.stage`` 兼容字段。
- :func:`build_safety_chain` 函数签名向上游零变更，传 capability_policy /
  permission_policy 也不抛异常。
- v0.1.3 → v0.1.4 stage 映射规则：``hard_block→capability`` /
  ``silent_allow→无`` / ``explicit_consent+standard→permission`` /
  ``explicit_consent+elevated→approval``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.contracts import ApprovalDecision, ApprovalRequest
from infrastructure.config import load_config
from infrastructure.config.models import ApprovalConfig, Config, ModelConfig
from safety.approval.chain import (
    SafetyChainError,
    SafetyGatedApproval,
    _decorate_stage_compat,
    build_safety_chain,
)
from safety.approval.decision_engine import SafetyDecisionEngine
from safety.approval.types import ApprovalMetadataKeys, BoundaryKind, DecisionSource
from safety.grants.store import GrantStore
from safety.policies.capability import CapabilityPolicy, CapabilitySet
from safety.policies.permission import PermissionPolicy


class _FixedApproval:
    """底层占位 ApprovalProvider：按构造参数返回固定 outcome。"""

    def __init__(self, outcome: str = "approved") -> None:
        self._outcome = outcome
        self.requests: list[ApprovalRequest] = []

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(outcome=self._outcome, reason=f"fixed-{self._outcome}")  # type: ignore[arg-type]


def _req(tool_name: str = "read_file", path: str | None = None) -> ApprovalRequest:
    args: dict[str, object] = {}
    if path is not None:
        args["path"] = path
    return ApprovalRequest(
        run_id="r",
        session_id="s",
        turn=1,
        call_id="c",
        tool_name=tool_name,
        arguments=args,
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


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SETTING_YAML = _REPO_ROOT / "config" / "setting.yaml"


# ---------------------------------------------------------------------------
# build_safety_chain：函数签名向上游零变更
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_safety_chain_returns_gated_approval() -> None:
    underlying = _FixedApproval("approved")
    chain = build_safety_chain(_cfg(), interactive_approval=underlying)
    assert isinstance(chain, SafetyGatedApproval)


@pytest.mark.unit
def test_build_safety_chain_accepts_legacy_policy_kwargs() -> None:
    """v0.1.3 兼容期：仍可显式传 capability / permission policy，不抛异常。"""
    underlying = _FixedApproval("approved")
    cap = CapabilityPolicy(CapabilitySet(deny=frozenset({"shell"})))
    perm = PermissionPolicy(rules=())
    chain = build_safety_chain(
        _cfg(),
        interactive_approval=underlying,
        capability_policy=cap,
        permission_policy=perm,
    )
    assert isinstance(chain, SafetyGatedApproval)


@pytest.mark.unit
async def test_build_safety_chain_smoke_decide_does_not_raise() -> None:
    """装配出来的 chain 能完成一次 decide 闭环（不验证具体 outcome）。"""
    underlying = _FixedApproval("approved")
    chain = build_safety_chain(_cfg(), interactive_approval=underlying)
    decision = await chain.decide(_req(tool_name="read_file", path="/tmp/test.txt"))
    assert decision.outcome in {"approved", "rejected"}


@pytest.mark.unit
@pytest.mark.parametrize("tool_name", ["list_agent_roles", "create_agent_role"])
async def test_agent_role_tools_are_silent_allowed(tool_name: str) -> None:
    """验证角色工具在仓库默认配置中静默放行，输入为 tool 名，输出 silent_allow。"""
    underlying = _FixedApproval("rejected")
    cfg = load_config(_SETTING_YAML, load_env_file=False)
    assert tool_name in cfg.safety.allow_tools_silent
    chain = build_safety_chain(cfg, interactive_approval=underlying)

    decision = await chain.decide(_req(tool_name=tool_name))

    assert decision.outcome == "approved"
    assert decision.metadata[ApprovalMetadataKeys.DECISION_CLASS] == "silent_allow"
    assert underlying.requests == []


# ---------------------------------------------------------------------------
# stage 兼容映射
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stage_compat_hard_block_maps_to_capability() -> None:
    """hard_block 不带 stage 时，装饰器补 stage='capability'。"""
    decision = ApprovalDecision(
        outcome="rejected",
        metadata={
            ApprovalMetadataKeys.DECISION_CLASS: "hard_block",
        },
    )
    decorated = _decorate_stage_compat(decision)
    assert decorated.metadata[ApprovalMetadataKeys.STAGE] == "capability"


@pytest.mark.unit
def test_stage_compat_explicit_consent_standard_maps_to_permission() -> None:
    decision = ApprovalDecision(
        outcome="approved",
        metadata={
            ApprovalMetadataKeys.DECISION_CLASS: "explicit_consent",
            ApprovalMetadataKeys.DECISION_SOURCE: "standard",
        },
    )
    decorated = _decorate_stage_compat(decision)
    assert decorated.metadata[ApprovalMetadataKeys.STAGE] == "permission"


@pytest.mark.unit
def test_stage_compat_explicit_consent_elevated_maps_to_approval() -> None:
    decision = ApprovalDecision(
        outcome="approved",
        metadata={
            ApprovalMetadataKeys.DECISION_CLASS: "explicit_consent",
            ApprovalMetadataKeys.DECISION_SOURCE: "elevated",
        },
    )
    decorated = _decorate_stage_compat(decision)
    assert decorated.metadata[ApprovalMetadataKeys.STAGE] == "approval"


@pytest.mark.unit
def test_stage_compat_silent_allow_does_not_set_stage() -> None:
    """silent_allow 是 v0.1.4 新概念，v0.1.3 没有对应 stage，不写 stage。"""
    decision = ApprovalDecision(
        outcome="approved",
        metadata={
            ApprovalMetadataKeys.DECISION_CLASS: "silent_allow",
            ApprovalMetadataKeys.DECISION_SOURCE: "intrinsic",
        },
    )
    decorated = _decorate_stage_compat(decision)
    assert ApprovalMetadataKeys.STAGE not in decorated.metadata


@pytest.mark.unit
def test_stage_compat_existing_stage_not_overwritten() -> None:
    """guard 自行写过 stage（HardBlockGuard / ConsentResolver）时，不覆盖。"""
    decision = ApprovalDecision(
        outcome="rejected",
        metadata={
            ApprovalMetadataKeys.STAGE: "capability",
            ApprovalMetadataKeys.DECISION_CLASS: "hard_block",
        },
    )
    decorated = _decorate_stage_compat(decision)
    # 应保持不变（同一引用语义足够）
    assert decorated.metadata[ApprovalMetadataKeys.STAGE] == "capability"


# ---------------------------------------------------------------------------
# SafetyGatedApproval 薄壳：engine 异常包装
# ---------------------------------------------------------------------------


class _RaisingEngine:
    """模拟 SafetyDecisionEngine 抛异常的 stub。"""

    async def decide(self, request, runtime):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated engine failure")


@pytest.mark.unit
async def test_safety_gated_approval_wraps_engine_exception() -> None:
    """engine 抛异常时，门面包成 SafetyChainError。"""
    chain = SafetyGatedApproval(
        engine=_RaisingEngine(),  # type: ignore[arg-type]
        grant_store=GrantStore(),
    )
    with pytest.raises(SafetyChainError):
        await chain.decide(_req())


@pytest.mark.unit
def test_legacy_decide_raises_runtime_error() -> None:
    """v0.1.3 fallback 已删除，调用 _legacy_decide 必抛 RuntimeError。"""
    chain = SafetyGatedApproval(
        engine=_RaisingEngine(),  # type: ignore[arg-type]
        grant_store=GrantStore(),
    )
    with pytest.raises(RuntimeError, match=r"legacy v0\.1\.3 path removed"):
        chain._legacy_decide(_req())


# ---------------------------------------------------------------------------
# build_safety_chain：trace_emitter / event_sinks 装配
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_build_safety_chain_with_explicit_trace_emitter() -> None:
    """显式传 trace_emitter 时，event_sinks 被忽略，使用传入的 emitter。"""
    captured: list[tuple[str, str]] = []

    def _capture(event_kind, decision, request):  # type: ignore[no-untyped-def]
        captured.append((event_kind, request.tool_name))

    underlying = _FixedApproval("approved")
    chain = build_safety_chain(
        _cfg(),
        interactive_approval=underlying,
        trace_emitter=_capture,
    )
    # write_file → 走 boundary/consent 路径，会触发 emit
    await chain.decide(_req(tool_name="write_file", path="/tmp/some-test.txt"))
    # 至少应该有一个事件被 emit（具体类型由实际链路决定）
    assert len(captured) >= 1


@pytest.mark.unit
def test_safety_decision_engine_directly_wired() -> None:
    """SafetyDecisionEngine 类可被直接构造并注入 SafetyGatedApproval。"""
    from safety.boundaries.resolver import BoundaryResolver
    from safety.grants.store import GrantStore
    from safety.guards.consent import ConsentResolver
    from safety.guards.hard_block import HardBlockGuard
    from safety.guards.trust import TrustResolver

    cfg = _cfg()
    underlying = _FixedApproval("approved")
    boundary = BoundaryResolver.from_project_root()
    grants = GrantStore.from_config(cfg)
    engine = SafetyDecisionEngine(
        hard_block=HardBlockGuard.from_config(cfg),
        boundary=boundary,
        trust=TrustResolver(boundary, grants),
        consent=ConsentResolver.from_config(cfg, interactive_approval=underlying),
    )
    chain = SafetyGatedApproval(engine=engine, grant_store=grants)
    assert chain._engine is engine
    assert chain.grant_store is grants


# ---------------------------------------------------------------------------
# v0.1.6 ACCEPT_FOR_SESSION 写回 GrantStore（修 v0.1.4 死代码）
# ---------------------------------------------------------------------------


class _SessionGrantingEngine:
    """stub engine 直接返回 ``approved + grant_scope=session`` decision。

    用于隔离测试 :meth:`SafetyGatedApproval._maybe_write_session_grant`，
    不依赖 ConsentResolver / InteractiveApproval 完整链路。
    """

    async def decide(self, request, runtime):  # type: ignore[no-untyped-def]
        return ApprovalDecision(
            outcome="approved",
            reason="stub session grant",
            metadata={
                ApprovalMetadataKeys.GRANT_SCOPE: "session",
                ApprovalMetadataKeys.DECISION_CLASS: "explicit_consent",
                ApprovalMetadataKeys.DECISION_SOURCE: "standard",
            },
        )


def _session_request(
    *,
    session_id: str = "thread-A",
    tool_name: str = "read_file",
) -> ApprovalRequest:
    return ApprovalRequest(
        run_id="r",
        session_id=session_id,
        turn=1,
        call_id="c1",
        tool_name=tool_name,
        arguments={"path": "/tmp/x"},
    )


@pytest.mark.unit
async def test_session_grant_writeback_after_consent() -> None:
    """v0.1.6 修复：approved + grant_scope=session 触发 put_session 写回。"""
    grants = GrantStore()
    chain = SafetyGatedApproval(
        engine=_SessionGrantingEngine(),  # type: ignore[arg-type]
        grant_store=grants,
    )

    decision = await chain.decide(_session_request(session_id="thread-A"))
    assert decision.outcome == "approved"

    # 验证 grant 落到 thread-A 桶
    sess = grants.session_grants("thread-A")
    assert len(sess) == 1
    grant = sess[0]
    assert grant.session_id == "thread-A"
    assert grant.scope == "session"
    # 命名约定对齐 from_config 的 allow_tools_silent 路径
    assert grant.key.capability == "tool:read_file"
    assert grant.key.matcher == "*"
    assert grant.key.boundary_kind == BoundaryKind.HOST
    assert grant.source == DecisionSource.SESSION


@pytest.mark.unit
async def test_session_grant_writeback_isolates_by_session_id() -> None:
    """thread-A 写的 grant 不应落到 thread-B 桶（修 P1 跨 thread 泄漏）。"""
    grants = GrantStore()
    chain = SafetyGatedApproval(
        engine=_SessionGrantingEngine(),  # type: ignore[arg-type]
        grant_store=grants,
    )

    await chain.decide(_session_request(session_id="thread-A"))

    # thread-A 桶有；thread-B 桶空
    assert len(grants.session_grants("thread-A")) == 1
    assert grants.session_grants("thread-B") == ()


@pytest.mark.unit
async def test_session_grant_skipped_when_outcome_not_approved() -> None:
    """rejected decision 即使 metadata.grant_scope=session 也不写 GrantStore。"""

    class _RejectingEngine:
        async def decide(self, request, runtime):  # type: ignore[no-untyped-def]
            return ApprovalDecision(
                outcome="rejected",
                reason="user rejected",
                metadata={ApprovalMetadataKeys.GRANT_SCOPE: "session"},
            )

    grants = GrantStore()
    chain = SafetyGatedApproval(
        engine=_RejectingEngine(),  # type: ignore[arg-type]
        grant_store=grants,
    )

    await chain.decide(_session_request())

    assert grants.session_grants() == (), "rejected 决策不应写 session grant"


@pytest.mark.unit
async def test_session_grant_skipped_when_session_id_empty() -> None:
    """session_id 为空字符串时（防御性）不写 grant。"""
    grants = GrantStore()
    chain = SafetyGatedApproval(
        engine=_SessionGrantingEngine(),  # type: ignore[arg-type]
        grant_store=grants,
    )

    # 空字符串 session_id：不写 grant，不抛错
    await chain.decide(_session_request(session_id=""))

    assert grants.session_grants() == ()


@pytest.mark.unit
async def test_session_grant_skipped_when_grant_scope_not_session() -> None:
    """grant_scope 不是 session（如 once）时不应写 GrantStore。"""

    class _OnceEngine:
        async def decide(self, request, runtime):  # type: ignore[no-untyped-def]
            return ApprovalDecision(
                outcome="approved",
                reason="approved once",
                metadata={
                    # 不带 grant_scope，相当于 ACCEPT_ONCE 路径
                    ApprovalMetadataKeys.DECISION_CLASS: "explicit_consent",
                },
            )

    grants = GrantStore()
    chain = SafetyGatedApproval(
        engine=_OnceEngine(),  # type: ignore[arg-type]
        grant_store=grants,
    )

    await chain.decide(_session_request())

    assert grants.session_grants() == (), "ACCEPT_ONCE 不应写 session grant"
