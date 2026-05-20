"""unit：DestructiveForceAskGuard + SafetyDecisionEngine 四级决策链集成测试。

覆盖 P0 事故修复 DoD：
1. rm -rf <任意路径> 被第②层 DestructiveForceAsk 拦截（即使 session grant 存在）
2. cd /tmp && rm -rf foo 被正确拆段
3. 非 destructive 命令（ls、git）不被拦截
4. shred / srm 被拦截
5. 非 shell 工具不被拦截
6. 四级决策链集成：HardBlock → Destructive → Trust → Consent
"""

from __future__ import annotations

import pytest

from core.contracts import ApprovalDecision, ApprovalRequest
from safety.default_rules import DEFAULT_DESTRUCTIVE_ALWAYS_ASK
from safety.guards.destructive import DestructiveForceAskGuard
from safety.types import BoundaryKind, RuntimeBoundaryContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _guard() -> DestructiveForceAskGuard:
    return DestructiveForceAskGuard(DEFAULT_DESTRUCTIVE_ALWAYS_ASK)


def _req(
    *,
    tool_name: str = "run_shell",
    command: str = "",
) -> ApprovalRequest:
    return ApprovalRequest(
        run_id="r1",
        session_id="s1",
        turn=1,
        call_id="c1",
        tool_name=tool_name,
        arguments={"command": command},
    )


# ---------------------------------------------------------------------------
# DestructiveForceAskGuard.matches() 单元测试
# ---------------------------------------------------------------------------


class TestDestructiveGuardMatches:
    """测试 matches() 方法的段级匹配逻辑。"""

    def test_rm_rf_matches(self) -> None:
        assert _guard().matches(_req(command="rm -rf /tmp/foo"))

    def test_rm_r_matches(self) -> None:
        assert _guard().matches(_req(command="rm -r ./build"))

    def test_rm_R_uppercase_matches(self) -> None:
        assert _guard().matches(_req(command="rm -R node_modules"))

    def test_rm_rf_with_option_cluster_matches(self) -> None:
        assert _guard().matches(_req(command="rm -fR data/"))

    def test_rm_recursive_long_form_matches(self) -> None:
        assert _guard().matches(_req(command="rm --recursive old/"))

    def test_shred_matches(self) -> None:
        assert _guard().matches(_req(command="shred secret.key"))

    def test_srm_matches(self) -> None:
        assert _guard().matches(_req(command="srm confidential.txt"))

    def test_compound_cd_then_rm_rf_matches(self) -> None:
        """cd /tmp && rm -rf foo → 第二段命中。"""
        assert _guard().matches(_req(command="cd /tmp && rm -rf foo"))

    def test_pipe_to_rm_matches(self) -> None:
        """find . | rm -rf → 第二段命中。"""
        assert _guard().matches(_req(command="find . -name '*.tmp' | rm -rf"))

    def test_semicolon_rm_matches(self) -> None:
        assert _guard().matches(_req(command="echo hello; rm -rf /tmp/x"))

    def test_ls_does_not_match(self) -> None:
        assert not _guard().matches(_req(command="ls /tmp"))

    def test_git_status_does_not_match(self) -> None:
        assert not _guard().matches(_req(command="git status"))

    def test_rm_without_recursive_does_not_match(self) -> None:
        """rm file.txt（无 -r）不命中 destructive guard。"""
        assert not _guard().matches(_req(command="rm file.txt"))

    def test_cat_does_not_match(self) -> None:
        assert not _guard().matches(_req(command="cat README.md"))

    def test_non_shell_tool_does_not_match(self) -> None:
        assert not _guard().matches(_req(tool_name="read_file", command=""))

    def test_empty_command_does_not_match(self) -> None:
        assert not _guard().matches(_req(command=""))

    def test_whitespace_only_does_not_match(self) -> None:
        assert not _guard().matches(_req(command="   "))


class TestDestructiveGuardMatchedRule:
    """测试 matched_rule() 返回正确的规则对象。"""

    def test_rm_rf_returns_rm_recursive_rule(self) -> None:
        rule = _guard().matched_rule(_req(command="rm -rf /opt"))
        assert rule is not None
        assert rule.name == "rm-recursive"

    def test_shred_returns_shred_rule(self) -> None:
        rule = _guard().matched_rule(_req(command="shred /etc/shadow"))
        assert rule is not None
        assert rule.name == "shred"

    def test_ls_returns_none(self) -> None:
        assert _guard().matched_rule(_req(command="ls /tmp")) is None


# ---------------------------------------------------------------------------
# SafetyDecisionEngine 集成测试：四级决策链
# ---------------------------------------------------------------------------


class _StubHardBlock:
    def evaluate(
        self, request: ApprovalRequest, runtime: RuntimeBoundaryContext
    ) -> ApprovalDecision | None:
        return None  # 不拦截


class _StubBoundary:
    def resolve(self, request: ApprovalRequest, runtime: RuntimeBoundaryContext):
        from safety.types import BoundaryDecision, BoundaryZone

        return BoundaryDecision(zone=BoundaryZone.APPROVAL)


class _StubTrust:
    """模拟有 session grant 的 TrustResolver：对所有请求返回 silent_allow。"""

    def evaluate(
        self, request: ApprovalRequest, runtime: RuntimeBoundaryContext
    ) -> ApprovalDecision | None:
        return ApprovalDecision(
            outcome="approved",
            reason="[silent_allow:session] grant matched",
        )


class _StubConsent:
    """模拟 ConsentResolver：记录是否被调用。"""

    def __init__(self) -> None:
        self.called = False
        self.last_request: ApprovalRequest | None = None

    async def evaluate(self, request, boundary_decision, runtime):
        self.called = True
        self.last_request = request
        return ApprovalDecision(
            outcome="approved",
            reason="user confirmed (elevated consent)",
        )


@pytest.mark.asyncio
async def test_destructive_bypasses_trust_goes_to_consent() -> None:
    """rm -rf 命中第②层 → 跳过 Trust → 直接进 Consent。"""
    from safety.decision_engine import SafetyDecisionEngine

    consent = _StubConsent()
    engine = SafetyDecisionEngine(
        hard_block=_StubHardBlock(),  # type: ignore[arg-type]
        boundary=_StubBoundary(),  # type: ignore[arg-type]
        destructive=_guard(),
        trust=_StubTrust(),  # type: ignore[arg-type]
        consent=consent,  # type: ignore[arg-type]
    )
    runtime = RuntimeBoundaryContext(boundary_kind=BoundaryKind.HOST)
    req = _req(command="rm -rf .kongming/skills/musk-five-steps")

    decision = await engine.decide(req, runtime)

    # Consent 被调用（不是 Trust）
    assert consent.called
    assert decision.outcome == "approved"
    assert decision.reason == "user confirmed (elevated consent)"


@pytest.mark.asyncio
async def test_non_destructive_uses_trust() -> None:
    """ls 命令 → 不命中第②层 → 走 Trust → silent_allow。"""
    from safety.decision_engine import SafetyDecisionEngine

    consent = _StubConsent()
    engine = SafetyDecisionEngine(
        hard_block=_StubHardBlock(),  # type: ignore[arg-type]
        boundary=_StubBoundary(),  # type: ignore[arg-type]
        destructive=_guard(),
        trust=_StubTrust(),  # type: ignore[arg-type]
        consent=consent,  # type: ignore[arg-type]
    )
    runtime = RuntimeBoundaryContext(boundary_kind=BoundaryKind.HOST)
    req = _req(command="ls /tmp")

    decision = await engine.decide(req, runtime)

    # Trust 短路，Consent 没被调用
    assert not consent.called
    assert decision.outcome == "approved"
    assert "silent_allow" in (decision.reason or "")


@pytest.mark.asyncio
async def test_compound_cd_rm_destructive_force_ask() -> None:
    """cd /tmp && rm -rf foo → 第二段命中 → 强制 Consent。"""
    from safety.decision_engine import SafetyDecisionEngine

    consent = _StubConsent()
    engine = SafetyDecisionEngine(
        hard_block=_StubHardBlock(),  # type: ignore[arg-type]
        boundary=_StubBoundary(),  # type: ignore[arg-type]
        destructive=_guard(),
        trust=_StubTrust(),  # type: ignore[arg-type]
        consent=consent,  # type: ignore[arg-type]
    )
    runtime = RuntimeBoundaryContext(boundary_kind=BoundaryKind.HOST)
    req = _req(command="cd /tmp && rm -rf foo")

    await engine.decide(req, runtime)
    assert consent.called
