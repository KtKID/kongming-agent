"""unit：grant matcher 细化 + TrustResolver 命令前缀匹配。

覆盖 P0 事故修复 DoD：
1. ls 的 session grant 不覆盖 git status（grant 按命令前缀隔离）
2. extract_command_base 正确提取各种命令格式
3. _maybe_write_session_grant 写入命令前缀而非 "*"
4. TrustResolver 步骤 2 用命令前缀匹配
"""

from __future__ import annotations

import time

import pytest

from core.contracts import ApprovalDecision, ApprovalRequest
from safety.chain import extract_command_base
from safety.grant_store import GrantStore
from safety.guards.trust import TrustResolver
from safety.types import (
    BoundaryDecision,
    BoundaryKind,
    BoundaryZone,
    DecisionSource,
    Grant,
    GrantKey,
    RuntimeBoundaryContext,
)

# ---------------------------------------------------------------------------
# extract_command_base 单元测试
# ---------------------------------------------------------------------------


class TestExtractCommandBase:
    def test_simple_command(self) -> None:
        assert extract_command_base("ls /tmp") == "ls"

    def test_git_command(self) -> None:
        assert extract_command_base("git status") == "git"

    def test_compound_command(self) -> None:
        """取第一段第一个 word。"""
        assert extract_command_base("cd /tmp && ls -la") == "cd"

    def test_pipe_command(self) -> None:
        assert extract_command_base("cat file.txt | grep foo") == "cat"

    def test_semicolon_command(self) -> None:
        assert extract_command_base("echo hello; ls") == "echo"

    def test_uv_run(self) -> None:
        assert extract_command_base("uv run pytest -v") == "uv"

    def test_python_module(self) -> None:
        assert extract_command_base("python -m pytest tests/") == "python"

    def test_rm_rf(self) -> None:
        assert extract_command_base("rm -rf foo") == "rm"

    def test_lowercase(self) -> None:
        """返回小写。"""
        assert extract_command_base("Git Status") == "git"

    def test_empty_string(self) -> None:
        assert extract_command_base("") == ""

    def test_whitespace_only(self) -> None:
        assert extract_command_base("   ") == ""

    def test_single_word(self) -> None:
        assert extract_command_base("pwd") == "pwd"


# ---------------------------------------------------------------------------
# TrustResolver 命令前缀隔离
# ---------------------------------------------------------------------------


class _StubBoundaryResolver:
    def __init__(self) -> None:
        pass

    def resolve(
        self, request: ApprovalRequest, runtime: RuntimeBoundaryContext
    ) -> BoundaryDecision:
        return BoundaryDecision(zone=BoundaryZone.APPROVAL)


def _make_trust_with_grant(
    *,
    capability: str,
    matcher: str,
    session_id: str = "s1",
) -> TrustResolver:
    """创建带一条 session grant 的 TrustResolver。"""
    boundary = _StubBoundaryResolver()
    store = GrantStore()
    grant = Grant(
        key=GrantKey(
            capability=capability,
            matcher=matcher,
            boundary_kind=BoundaryKind.HOST,
        ),
        scope="session",
        source=DecisionSource.SESSION,
        created_at=time.time(),
        session_id=session_id,
    )
    store.put_session(grant)
    return TrustResolver(boundary, store)  # type: ignore[arg-type]


def _req(
    *,
    tool_name: str = "run_shell",
    command: str = "",
    session_id: str = "s1",
) -> ApprovalRequest:
    return ApprovalRequest(
        run_id="r1",
        session_id=session_id,
        turn=1,
        call_id="c1",
        tool_name=tool_name,
        arguments={"command": command},
    )


def _runtime() -> RuntimeBoundaryContext:
    return RuntimeBoundaryContext(boundary_kind=BoundaryKind.HOST)


class TestTrustResolverCommandPrefixIsolation:
    """验证 grant 按命令前缀隔离。"""

    def test_ls_grant_matches_ls_command(self) -> None:
        """ls grant → ls 命令命中。"""
        trust = _make_trust_with_grant(capability="tool:run_shell", matcher="ls")
        decision = trust.evaluate(_req(command="ls /tmp"), _runtime())
        assert decision is not None
        assert decision.outcome == "approved"

    def test_ls_grant_does_not_match_git_command(self) -> None:
        """ls grant → git 命令不命中 → 返回 None。"""
        trust = _make_trust_with_grant(capability="tool:run_shell", matcher="ls")
        decision = trust.evaluate(_req(command="git status"), _runtime())
        assert decision is None

    def test_ls_grant_does_not_match_rm_command(self) -> None:
        """ls grant → rm 命令不命中 → 返回 None。"""
        trust = _make_trust_with_grant(capability="tool:run_shell", matcher="ls")
        decision = trust.evaluate(_req(command="rm file.txt"), _runtime())
        assert decision is None

    def test_git_grant_matches_git_diff(self) -> None:
        """git grant → git diff 命中。"""
        trust = _make_trust_with_grant(capability="tool:run_shell", matcher="git")
        decision = trust.evaluate(_req(command="git diff"), _runtime())
        assert decision is not None
        assert decision.outcome == "approved"

    def test_wildcard_grant_still_matches_everything(self) -> None:
        """旧的 "*" grant 仍然匹配所有命令（兼容 allow_tools_silent）。"""
        trust = _make_trust_with_grant(capability="tool:run_shell", matcher="*")
        decision = trust.evaluate(_req(command="rm -rf /tmp/x"), _runtime())
        assert decision is not None
        assert decision.outcome == "approved"

    def test_non_shell_wildcard_grant_not_reachable_for_unknown_tool(self) -> None:
        """已知限制：memory 等未在 _derive_capability_and_target 注册的工具，
        session grant 的 tool:* 通配不可达（_lookup_grant early return）。
        这是预留 bug，不在本次 P0 修复范围。"""
        trust = _make_trust_with_grant(capability="tool:memory", matcher="*")
        req = ApprovalRequest(
            run_id="r1",
            session_id="s1",
            turn=1,
            call_id="c1",
            tool_name="memory",
            arguments={"action": "write"},
        )
        decision = trust.evaluate(req, _runtime())
        # 预期 None：_derive_capability_and_target 对 memory 返回 (None, None)
        # 导致 early return，step 2 wildcard 不可达
        assert decision is None

    def test_session_isolation_between_sessions(self) -> None:
        """session grant 不跨 session。"""
        trust = _make_trust_with_grant(capability="tool:run_shell", matcher="ls", session_id="s1")
        decision = trust.evaluate(_req(command="ls /tmp", session_id="s2"), _runtime())
        assert decision is None


# ---------------------------------------------------------------------------
# _maybe_write_session_grant 写入侧测试（R3 P0 修复）
# ---------------------------------------------------------------------------


class TestMaybeWriteSessionGrantCommandPrefix:
    """验证 SafetyGatedApproval._maybe_write_session_grant 写入命令前缀而非 "*"。

    这是 R3 发现的 P0 缺口：之前只测了 TrustResolver 查询侧，
    没测 grant 写入侧是否真的产出了命令前缀 matcher。
    """

    @staticmethod
    def _make_gated_approval() -> tuple:
        """构造 SafetyGatedApproval + GrantStore，便于检查 grant 写入。"""
        from safety.chain import SafetyGatedApproval
        from safety.decision_engine import SafetyDecisionEngine
        from safety.grant_store import GrantStore

        store = GrantStore()

        # 构造 minimal stubs
        class _StubHardBlock:
            def evaluate(self, req, rt):
                return None

        class _StubBoundary:
            def resolve(self, req, rt):
                from safety.types import BoundaryDecision, BoundaryZone

                return BoundaryDecision(zone=BoundaryZone.APPROVAL)

        class _StubTrust:
            def evaluate(self, req, rt):
                return None

        class _StubConsent:
            async def evaluate(self, req, bd, rt):
                return ApprovalDecision(
                    outcome="approved",
                    reason="user confirmed (session grant)",
                    metadata={"grant_scope": "session"},
                )

        engine = SafetyDecisionEngine(
            hard_block=_StubHardBlock(),  # type: ignore[arg-type]
            boundary=_StubBoundary(),  # type: ignore[arg-type]
            trust=_StubTrust(),  # type: ignore[arg-type]
            consent=_StubConsent(),  # type: ignore[arg-type]
        )
        gated = SafetyGatedApproval(engine=engine, grant_store=store)
        return gated, store

    @pytest.mark.asyncio
    async def test_shell_command_writes_command_prefix_not_wildcard(self) -> None:
        """ls /tmp → grant matcher 应为 "ls"，不是 "*"。"""
        gated, store = self._make_gated_approval()
        req = ApprovalRequest(
            run_id="r1",
            session_id="s1",
            turn=1,
            call_id="c-ls",
            tool_name="run_shell",
            arguments={"command": "ls /tmp"},
        )
        decision = await gated.decide(req)
        assert decision.outcome == "approved"

        # 检查 GrantStore 中写入的 grant
        bucket = store._session_grants.get("s1", {})
        assert len(bucket) == 1
        grant = next(iter(bucket.values()))
        assert grant.key.capability == "tool:run_shell"
        assert grant.key.matcher == "ls"  # 命令前缀，不是 "*"

    @pytest.mark.asyncio
    async def test_git_command_writes_git_prefix(self) -> None:
        """git status → grant matcher 应为 "git"。"""
        gated, store = self._make_gated_approval()
        req = ApprovalRequest(
            run_id="r1",
            session_id="s1",
            turn=1,
            call_id="c-git",
            tool_name="run_shell",
            arguments={"command": "git status"},
        )
        await gated.decide(req)
        bucket = store._session_grants.get("s1", {})
        grant = next(iter(bucket.values()))
        assert grant.key.matcher == "git"

    @pytest.mark.asyncio
    async def test_non_shell_tool_writes_wildcard(self) -> None:
        """非 shell 工具（如 write_file）应保持 matcher="*"。"""
        gated, store = self._make_gated_approval()
        req = ApprovalRequest(
            run_id="r1",
            session_id="s1",
            turn=1,
            call_id="c-write",
            tool_name="write_file",
            arguments={"path": "/tmp/foo.txt", "content": "hello"},
        )
        await gated.decide(req)
        bucket = store._session_grants.get("s1", {})
        grant = next(iter(bucket.values()))
        assert grant.key.capability == "tool:write_file"
        assert grant.key.matcher == "*"  # 非 shell 保持通配

    @pytest.mark.asyncio
    async def test_empty_command_skips_grant(self) -> None:
        """空命令 → 跳过 grant 写入（避免 fallback 到 "*" 过度授权）。"""
        gated, store = self._make_gated_approval()
        req = ApprovalRequest(
            run_id="r1",
            session_id="s1",
            turn=1,
            call_id="c-empty",
            tool_name="run_shell",
            arguments={"command": ""},
        )
        await gated.decide(req)
        bucket = store._session_grants.get("s1", {})
        assert len(bucket) == 0  # 空命令不写 grant

    @pytest.mark.asyncio
    async def test_whitespace_only_command_skips_grant(self) -> None:
        """纯空白命令 → 同样跳过 grant 写入。"""
        gated, store = self._make_gated_approval()
        req = ApprovalRequest(
            run_id="r1",
            session_id="s1",
            turn=1,
            call_id="c-ws",
            tool_name="run_shell",
            arguments={"command": "   "},
        )
        await gated.decide(req)
        bucket = store._session_grants.get("s1", {})
        assert len(bucket) == 0  # 纯空白命令不写 grant
