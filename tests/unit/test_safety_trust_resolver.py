"""unit：safety v0.1.4 M6 TrustResolver 三类证据消费。

覆盖任务 #30-#33：
1. intrinsic 证据（消费 ResolvedBoundary）
2. session/config 证据（消费 GrantStore）
3. capability + path_prefix + boundary_kind 三元组匹配
4. 短路顺序 intrinsic → session → config
5. boundary_kind=SANDBOX 不应命中（v0.1.4 hosts only）
6. HardBlock 不可被覆盖（依赖装配顺序保证；本测试通过 zone=BLOCKED 不会到达 trust 来体现）
"""

from __future__ import annotations

import time

import pytest

from core.contracts import ApprovalRequest
from safety.approval.types import (
    ApprovalMetadataKeys,
    BoundaryDecision,
    BoundaryKind,
    BoundaryZone,
    DecisionSource,
    Grant,
    GrantKey,
    RuntimeBoundaryContext,
)
from safety.grants.store import GrantStore
from safety.guards.trust import TrustResolver

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _runtime() -> RuntimeBoundaryContext:
    return RuntimeBoundaryContext(boundary_kind=BoundaryKind.HOST)


def _req(
    *,
    tool_name: str,
    arguments: dict[str, object],
) -> ApprovalRequest:
    return ApprovalRequest(
        run_id="r1",
        session_id="s1",
        turn=1,
        call_id=f"c-{tool_name}",
        tool_name=tool_name,
        arguments=arguments,
    )


class _StubBoundaryResolver:
    """轻量 stub：让测试直接控制 resolve 返回的 BoundaryDecision。"""

    def __init__(self, decision: BoundaryDecision) -> None:
        self.decision = decision
        self.calls: list[ApprovalRequest] = []

    def resolve(
        self,
        request: ApprovalRequest,
        runtime: RuntimeBoundaryContext,
    ) -> BoundaryDecision:
        self.calls.append(request)
        return self.decision


def _make_trust(
    *,
    boundary_zone: BoundaryZone = BoundaryZone.APPROVAL,
    boundary_rule: str | None = None,
) -> tuple[TrustResolver, _StubBoundaryResolver, GrantStore]:
    """构造 TrustResolver + 控制好 boundary stub + 空 GrantStore。"""
    boundary = _StubBoundaryResolver(
        BoundaryDecision(
            zone=boundary_zone,
            matched_rule=boundary_rule,
            reason="stub",
        ),
    )
    grants = GrantStore()
    trust = TrustResolver(
        boundary_resolver=boundary,  # type: ignore[arg-type]
        grant_store=grants,
    )
    return trust, boundary, grants


# ---------------------------------------------------------------------------
# §1 intrinsic（trusted zone 命中）
# ---------------------------------------------------------------------------


def test_trusted_zone_yields_silent_allow_with_intrinsic_source() -> None:
    trust, _b, _g = _make_trust(
        boundary_zone=BoundaryZone.TRUSTED, boundary_rule="trusted_write_trie"
    )
    decision = trust.evaluate(
        _req(tool_name="write_file", arguments={"path": "/proj/src/foo.py"}),
        _runtime(),
    )
    assert decision is not None
    assert decision.outcome == "approved"
    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "silent_allow"
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "intrinsic"
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "trusted_write_trie"
    assert md[ApprovalMetadataKeys.BOUNDARY_KIND] == "host"
    # intrinsic 不应携带 grant_scope（仅 session/config 带）
    assert ApprovalMetadataKeys.GRANT_SCOPE not in md


# ---------------------------------------------------------------------------
# §2 session grant 命中
# ---------------------------------------------------------------------------


def test_session_grant_yields_silent_allow_with_session_source() -> None:
    trust, _b, grants = _make_trust(boundary_zone=BoundaryZone.APPROVAL)
    grants.put_session(
        Grant(
            key=GrantKey(
                capability="file_write",
                matcher="tests/integration/",
                boundary_kind=BoundaryKind.HOST,
            ),
            scope="session",
            source=DecisionSource.SESSION,
            created_at=time.time(),
            session_id="s1",
        ),
    )
    decision = trust.evaluate(
        _req(
            tool_name="write_file",
            arguments={"path": "tests/integration/test_foo.py"},
        ),
        _runtime(),
    )
    assert decision is not None
    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "session"
    assert md[ApprovalMetadataKeys.GRANT_SCOPE] == "session"
    assert "tests/integration/" in md[ApprovalMetadataKeys.MATCHED_RULE]


# ---------------------------------------------------------------------------
# §3 config grant 命中（通过 from_config）
# ---------------------------------------------------------------------------


def test_config_grant_yields_silent_allow_with_config_source() -> None:
    from infrastructure.config.models import (
        Config,
        ModelConfig,
        SafetyConfig,
    )

    cfg = Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="m",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        safety=SafetyConfig(allow_writes=["~/scratch/"]),
    )
    grants = GrantStore.from_config(cfg)
    boundary = _StubBoundaryResolver(
        BoundaryDecision(zone=BoundaryZone.APPROVAL, matched_rule=None, reason="stub"),
    )
    trust = TrustResolver(boundary_resolver=boundary, grant_store=grants)  # type: ignore[arg-type]

    decision = trust.evaluate(
        _req(tool_name="write_file", arguments={"path": "~/scratch/foo.txt"}),
        _runtime(),
    )
    assert decision is not None
    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "config"
    assert md[ApprovalMetadataKeys.GRANT_SCOPE] == "config"


def test_config_allow_tools_silent_uses_tool_capability_wildcard() -> None:
    """allow_tools_silent 走 ``tool:<name>`` capability + ``*`` 通配。"""
    from infrastructure.config.models import Config, ModelConfig, SafetyConfig

    cfg = Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="m",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        safety=SafetyConfig(allow_tools_silent=["read_file"]),
    )
    grants = GrantStore.from_config(cfg)
    boundary = _StubBoundaryResolver(
        BoundaryDecision(zone=BoundaryZone.APPROVAL, matched_rule=None, reason="stub"),
    )
    trust = TrustResolver(boundary_resolver=boundary, grant_store=grants)  # type: ignore[arg-type]

    decision = trust.evaluate(
        _req(tool_name="read_file", arguments={"path": "/any/path/foo.json"}),
        _runtime(),
    )
    assert decision is not None
    assert decision.metadata[ApprovalMetadataKeys.DECISION_SOURCE] == "config"


# ---------------------------------------------------------------------------
# §4 短路顺序：intrinsic > session > config > None
# ---------------------------------------------------------------------------


def test_intrinsic_short_circuits_grant_lookup() -> None:
    """trusted zone 命中后不应查 GrantStore。"""
    trust, _b, grants = _make_trust(boundary_zone=BoundaryZone.TRUSTED, boundary_rule="trusted")
    # 故意放一个 path 与 request 完全无关的 session grant，
    # 来证明：即便不命中也无所谓 — 因为 intrinsic 短路了 lookup
    grants.put_session(
        Grant(
            key=GrantKey(
                capability="file_write",
                matcher="other/dir/",
                boundary_kind=BoundaryKind.HOST,
            ),
            scope="session",
            source=DecisionSource.SESSION,
            created_at=time.time(),
            session_id="s1",
        ),
    )
    decision = trust.evaluate(
        _req(tool_name="write_file", arguments={"path": "/proj/src/main.py"}),
        _runtime(),
    )
    assert decision is not None
    # 仍然是 intrinsic 命中，因为短路
    assert decision.metadata[ApprovalMetadataKeys.DECISION_SOURCE] == "intrinsic"


def test_session_overrides_config_when_both_match() -> None:
    """session > config 优先级（GrantStore.find_matching 已实现，TrustResolver 复用）。"""
    from infrastructure.config.models import Config, ModelConfig, SafetyConfig

    cfg = Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="m",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        safety=SafetyConfig(allow_writes=["tests/"]),
    )
    grants = GrantStore.from_config(cfg)
    grants.put_session(
        Grant(
            key=GrantKey(
                capability="file_write",
                matcher="tests/integration/",
                boundary_kind=BoundaryKind.HOST,
            ),
            scope="session",
            source=DecisionSource.SESSION,
            created_at=time.time(),
            session_id="s1",
        ),
    )
    boundary = _StubBoundaryResolver(
        BoundaryDecision(zone=BoundaryZone.APPROVAL, matched_rule=None, reason="stub"),
    )
    trust = TrustResolver(boundary_resolver=boundary, grant_store=grants)  # type: ignore[arg-type]

    decision = trust.evaluate(
        _req(
            tool_name="write_file",
            arguments={"path": "tests/integration/test_x.py"},
        ),
        _runtime(),
    )
    assert decision is not None
    # session 优先（GrantStore 内部保证：先查 session 再查 config）
    assert decision.metadata[ApprovalMetadataKeys.DECISION_SOURCE] == "session"


# ---------------------------------------------------------------------------
# §5 无证据 → None（让 ConsentResolver 接管）
# ---------------------------------------------------------------------------


def test_no_evidence_returns_none() -> None:
    """approval zone + 空 GrantStore → 无证据，返回 None。"""
    trust, _b, _g = _make_trust(boundary_zone=BoundaryZone.APPROVAL)
    decision = trust.evaluate(
        _req(tool_name="write_file", arguments={"path": "/tmp/random.txt"}),
        _runtime(),
    )
    assert decision is None


def test_blocked_zone_returns_none_for_trust_layer() -> None:
    """blocked zone：理论上 HardBlock 已拦截，TrustResolver 不应被调用。

    防御性测试：即便误调用，blocked zone 也应返回 None（让 ConsentResolver 抛 assert）。
    """
    trust, _b, _g = _make_trust(boundary_zone=BoundaryZone.BLOCKED)
    decision = trust.evaluate(
        _req(tool_name="write_file", arguments={"path": "/etc/hosts"}),
        _runtime(),
    )
    # blocked zone 不属于 trusted，trust 不应放行；返回 None
    assert decision is None


# ---------------------------------------------------------------------------
# §6 三元组匹配：boundary_kind 必填 host，sandbox grant 不命中
# ---------------------------------------------------------------------------


def test_sandbox_grant_does_not_match_host_runtime() -> None:
    """v0.1.4 boundary_kind 恒 host：GrantStore 拒绝 sandbox key 写入，间接保证不会命中。"""
    grants = GrantStore()
    # 直接写 sandbox grant 应抛 ValueError（GrantStore 的硬约束）
    with pytest.raises(ValueError, match="sandbox"):
        grants.put_session(
            Grant(
                key=GrantKey(
                    capability="file_write",
                    matcher="/anywhere/",
                    boundary_kind=BoundaryKind.SANDBOX,
                ),
                scope="session",
                source=DecisionSource.SESSION,
                created_at=time.time(),
            ),
        )


def test_unknown_tool_returns_none_no_lookup() -> None:
    """未识别工具不派生 capability，跳过 GrantStore 查询。"""
    trust, _b, grants = _make_trust(boundary_zone=BoundaryZone.APPROVAL)
    grants.put_session(
        Grant(
            key=GrantKey(
                capability="file_write",
                matcher="/anywhere/",
                boundary_kind=BoundaryKind.HOST,
            ),
            scope="session",
            source=DecisionSource.SESSION,
            created_at=time.time(),
            session_id="s1",
        ),
    )
    decision = trust.evaluate(
        _req(tool_name="unknown_tool", arguments={"x": 1}),
        _runtime(),
    )
    assert decision is None


def test_shell_tool_uses_command_first_token_as_action() -> None:
    """run_shell 的 grant 查询基于命令首 token（action profile 形式）。"""
    trust, _b, grants = _make_trust(boundary_zone=BoundaryZone.APPROVAL)
    grants.put_session(
        Grant(
            key=GrantKey(
                capability="shell",
                matcher="pytest",
                boundary_kind=BoundaryKind.HOST,
            ),
            scope="session",
            source=DecisionSource.SESSION,
            created_at=time.time(),
            session_id="s1",
        ),
    )
    decision = trust.evaluate(
        _req(tool_name="run_shell", arguments={"command": "pytest tests/ -v"}),
        _runtime(),
    )
    assert decision is not None
    assert decision.metadata[ApprovalMetadataKeys.DECISION_SOURCE] == "session"
