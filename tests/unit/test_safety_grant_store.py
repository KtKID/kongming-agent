"""unit：safety v0.1.4 M5.2 — :class:`safety.grant_store.GrantStore`。

覆盖：

1. ``from_config`` 把 :class:`SafetyConfig.allow_writes` /
   ``allow_tools_silent`` 转 :class:`Grant`。
2. session/config 双层查询语义（``get`` / ``find_matching``）。
3. ``put_session`` / ``put_persist`` / ``clear_session`` 行为。
4. boundary_kind=SANDBOX 的 GrantKey 不应被匹配。
5. ``take_pending_persist`` 队列管理。
"""

from __future__ import annotations

import pytest

from config_loader.models import (
    Config,
    ModelConfig,
    SafetyConfig,
)
from safety.grant_store import (
    GrantStore,
    grant_with_now,
    normalized_path_matcher,
    session_grant_from_config_template,
)
from safety.types import (
    BoundaryKind,
    DecisionSource,
    Grant,
    GrantKey,
)


def _build_config(
    *,
    allow_writes: list[str] | None = None,
    allow_tools_silent: list[str] | None = None,
) -> Config:
    """造一个最小可用 Config 给 GrantStore.from_config 用。"""
    return Config(
        model=ModelConfig(
            name="test-model",
            base_url="http://127.0.0.1:11434/v1",
            api_key="",
        ),
        safety=SafetyConfig(
            allow_writes=allow_writes or [],
            allow_tools_silent=allow_tools_silent or [],
        ),
    )


# ---------------------------------------------------------------------------
# from_config
# ---------------------------------------------------------------------------


class TestFromConfig:
    def test_loads_allow_writes_as_file_write_grants(self) -> None:
        cfg = _build_config(allow_writes=["~/scratch/", "/tmp/kongming-*/"])
        store = GrantStore.from_config(cfg)

        config_grants = store.config_grants()
        assert len(config_grants) == 2

        capabilities = {g.key.capability for g in config_grants}
        assert capabilities == {"file_write"}

        matchers = {g.key.matcher for g in config_grants}
        assert matchers == {"~/scratch/", "/tmp/kongming-*/"}

        for g in config_grants:
            assert g.key.boundary_kind == BoundaryKind.HOST
            assert g.scope == "config"
            assert g.source == DecisionSource.CONFIG

    def test_loads_allow_tools_silent_with_wildcard_matcher(self) -> None:
        cfg = _build_config(allow_tools_silent=["read_file", "list_dir"])
        store = GrantStore.from_config(cfg)

        config_grants = store.config_grants()
        assert len(config_grants) == 2

        capabilities = {g.key.capability for g in config_grants}
        assert capabilities == {"tool:read_file", "tool:list_dir"}

        for g in config_grants:
            assert g.key.matcher == "*"
            assert g.key.boundary_kind == BoundaryKind.HOST

    def test_empty_config_yields_empty_store(self) -> None:
        cfg = _build_config()
        store = GrantStore.from_config(cfg)
        assert store.config_grants() == ()
        assert store.session_grants() == ()


# ---------------------------------------------------------------------------
# get / put_session / put_persist / clear_session
# ---------------------------------------------------------------------------


class TestSessionWrite:
    def test_put_session_stores_and_retrievable(self) -> None:
        store = GrantStore()
        grant = grant_with_now(
            capability="file_write",
            matcher="tests/integration/",
            scope="session",
            source=DecisionSource.SESSION,
            session_id="s1",
        )
        store.put_session(grant)

        got = store.get(grant.key, session_id="s1")
        assert got is grant
        assert store.session_grants() == (grant,)

    def test_clear_session_removes_only_specified_session(self) -> None:
        cfg = _build_config(allow_writes=["~/scratch/"])
        store = GrantStore.from_config(cfg)
        session_grant = grant_with_now(
            capability="file_write",
            matcher="tests/",
            scope="session",
            source=DecisionSource.SESSION,
            session_id="s1",
        )
        store.put_session(session_grant)

        assert len(store.session_grants()) == 1
        assert len(store.config_grants()) == 1

        store.clear_session("s1")

        assert store.session_grants() == ()
        # config grants 不动
        assert len(store.config_grants()) == 1

    def test_put_session_rejects_non_session_scope(self) -> None:
        store = GrantStore()
        bad_grant = grant_with_now(
            capability="file_write",
            matcher="x/",
            scope="config",
            source=DecisionSource.CONFIG,
        )
        with pytest.raises(ValueError, match=r"put_session requires grant\.scope='session'"):
            store.put_session(bad_grant)

    def test_put_session_rejects_sandbox_grant(self) -> None:
        store = GrantStore()
        # 手工绕过 grant_with_now（强制 HOST），直接构造 sandbox grant
        bad_key = GrantKey(
            capability="file_write",
            matcher="x/",
            boundary_kind=BoundaryKind.SANDBOX,
        )
        bad_grant = Grant(
            key=bad_key,
            scope="session",
            source=DecisionSource.SESSION,
            created_at=0.0,
            session_id="s1",
        )
        with pytest.raises(ValueError, match="boundary_kind=HOST"):
            store.put_session(bad_grant)

    def test_session_grant_does_not_leak_across_sessions(self) -> None:
        """P1 bug 回归（reports/bug/bug-report-20260427-235232）。

        thread-A 在 GrantStore 写 session grant 后，thread-B 必须查不到。
        修复前 GrantStore 是平铺 dict，跨 thread 信任泄漏；修复后按 session_id
        分桶物理隔离。
        """
        store = GrantStore()
        thread_a_grant = grant_with_now(
            capability="tool:read_file",
            matcher="*",
            scope="session",
            source=DecisionSource.SESSION,
            session_id="thread-A",
        )
        store.put_session(thread_a_grant)

        # thread-A 自己能命中
        hit_a = store.find_matching(
            "tool:read_file", "/etc/passwd", BoundaryKind.HOST, session_id="thread-A"
        )
        assert hit_a is thread_a_grant, "thread-A 应能命中自己的 session grant"

        # thread-B 不应命中（核心断言）
        hit_b = store.find_matching(
            "tool:read_file", "/etc/passwd", BoundaryKind.HOST, session_id="thread-B"
        )
        assert hit_b is None, "session grant 不应跨 session 命中 — 这是 P1 bug 的核心断言"

        # get(key) 同样隔离
        assert store.get(thread_a_grant.key, session_id="thread-A") is thread_a_grant
        assert store.get(thread_a_grant.key, session_id="thread-B") is None

    def test_lookup_without_session_id_returns_none_for_session_grants(self) -> None:
        """不传 session_id 时，session 桶完全跳过；只 fallback 查 config。"""
        cfg = _build_config(allow_writes=["~/scratch/"])
        store = GrantStore.from_config(cfg)
        session_grant = grant_with_now(
            capability="tool:foo",
            matcher="*",
            scope="session",
            source=DecisionSource.SESSION,
            session_id="s1",
        )
        store.put_session(session_grant)

        # 不传 session_id：session 桶不查；session_grant 看不见
        assert store.get(session_grant.key) is None
        assert store.find_matching("tool:foo", "*", BoundaryKind.HOST) is None

        # 但 config grant 仍可命中（不依赖 session_id）
        cfg_hit = store.find_matching("file_write", "~/scratch/foo.md", BoundaryKind.HOST)
        assert cfg_hit is not None

    def test_clear_session_is_idempotent_for_unknown_session_id(self) -> None:
        store = GrantStore()
        # 不存在的 session_id 不抛错（pop with default）
        store.clear_session("never-existed")
        assert store.session_grants() == ()


class TestPersistQueue:
    def test_put_persist_appends_to_pending_and_config(self) -> None:
        store = GrantStore()
        grant = grant_with_now(
            capability="file_write",
            matcher="/tmp/kongming-x/",
            scope="config",
            source=DecisionSource.CONFIG,
        )
        store.put_persist(grant)

        # 已经在 config_grants 里
        assert grant in store.config_grants()
        # take_pending_persist 应消费队列
        pending = store.take_pending_persist()
        assert pending == (grant,)
        # 二次取应为空
        assert store.take_pending_persist() == ()

    def test_put_persist_rejects_session_scope(self) -> None:
        store = GrantStore()
        bad_grant = grant_with_now(
            capability="file_write",
            matcher="x/",
            scope="session",
            source=DecisionSource.SESSION,
            session_id="s1",  # session grant 必带 session_id（数据结构强制）
        )
        with pytest.raises(ValueError, match=r"put_persist requires grant\.scope='config'"):
            store.put_persist(bad_grant)


# ---------------------------------------------------------------------------
# get：精确查询 + sandbox 拒绝
# ---------------------------------------------------------------------------


class TestGet:
    def test_get_returns_session_before_config(self) -> None:
        cfg = _build_config(allow_writes=["~/scratch/"])
        store = GrantStore.from_config(cfg)

        # 用与 config 相同 key 的 session grant 覆盖
        same_key = GrantKey(
            capability="file_write",
            matcher="~/scratch/",
            boundary_kind=BoundaryKind.HOST,
        )
        session_grant = Grant(
            key=same_key,
            scope="session",
            source=DecisionSource.SESSION,
            created_at=999.0,
            session_id="s1",
        )
        store.put_session(session_grant)

        got = store.get(same_key, session_id="s1")
        assert got is session_grant
        assert got.scope == "session"

    def test_get_rejects_sandbox_key(self) -> None:
        store = GrantStore()
        sandbox_key = GrantKey(
            capability="file_write",
            matcher="anywhere",
            boundary_kind=BoundaryKind.SANDBOX,
        )
        assert store.get(sandbox_key) is None

    def test_unknown_capability_returns_none(self) -> None:
        cfg = _build_config(allow_writes=["~/scratch/"])
        store = GrantStore.from_config(cfg)

        unknown_key = GrantKey(
            capability="memory",
            matcher="~/scratch/",
            boundary_kind=BoundaryKind.HOST,
        )
        assert store.get(unknown_key) is None


# ---------------------------------------------------------------------------
# find_matching：path-prefix 语义 + sandbox 不匹配
# ---------------------------------------------------------------------------


class TestFindMatching:
    def test_path_prefix_match_with_trailing_slash(self) -> None:
        cfg = _build_config(allow_writes=["tests/integration/"])
        store = GrantStore.from_config(cfg)

        # 完全匹配
        got = store.find_matching("file_write", "tests/integration/", BoundaryKind.HOST)
        assert got is not None
        # 子路径
        got2 = store.find_matching(
            "file_write", "tests/integration/foo/bar.json", BoundaryKind.HOST
        )
        assert got2 is not None
        # 兄弟路径不应命中（避免 "tests/" 误命中 "tests/foobar/"）
        got3 = store.find_matching("file_write", "tests/unit/foo.py", BoundaryKind.HOST)
        assert got3 is None

    def test_path_prefix_without_trailing_slash_strict_segment(self) -> None:
        # matcher 不带尾斜杠：只允许"完全相等"或"prefix + '/'"
        cfg = _build_config(allow_writes=["src/foo"])
        store = GrantStore.from_config(cfg)

        assert store.find_matching("file_write", "src/foo", BoundaryKind.HOST) is not None
        assert store.find_matching("file_write", "src/foo/bar.py", BoundaryKind.HOST) is not None
        # 关键：不能误命中 "src/foobar"
        assert store.find_matching("file_write", "src/foobar", BoundaryKind.HOST) is None

    def test_session_grant_overrides_config_for_longer_prefix(self) -> None:
        cfg = _build_config(allow_writes=["tests/"])
        store = GrantStore.from_config(cfg)

        # session grant 更具体（更长 prefix）
        long_grant = grant_with_now(
            capability="file_write",
            matcher="tests/integration/",
            scope="session",
            source=DecisionSource.SESSION,
            session_id="s1",
        )
        store.put_session(long_grant)

        got = store.find_matching(
            "file_write", "tests/integration/foo.py", BoundaryKind.HOST, session_id="s1"
        )
        assert got is long_grant
        # session 即使没更长，也优先（同长度时按顺序，但本测试 long_grant 更长）
        assert got.scope == "session"

    def test_wildcard_tool_capability_match(self) -> None:
        cfg = _build_config(allow_tools_silent=["read_file"])
        store = GrantStore.from_config(cfg)

        got = store.find_matching("tool:read_file", "any-arg", BoundaryKind.HOST)
        assert got is not None
        assert got.key.matcher == "*"

        # 工具名不匹配
        got2 = store.find_matching("tool:write_file", "any", BoundaryKind.HOST)
        assert got2 is None

    def test_sandbox_boundary_does_not_match(self) -> None:
        cfg = _build_config(allow_writes=["tests/"])
        store = GrantStore.from_config(cfg)

        # 显式以 SANDBOX 查询：v0.1.4 host-only，应永远不命中
        got = store.find_matching("file_write", "tests/integration/foo.py", BoundaryKind.SANDBOX)
        assert got is None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_grant_with_now_forces_host_boundary(self) -> None:
        g = grant_with_now(
            capability="file_write",
            matcher="x/",
            scope="session",
            source=DecisionSource.SESSION,
            session_id="s1",
        )
        assert g.key.boundary_kind == BoundaryKind.HOST
        assert g.scope == "session"
        assert g.created_at > 0
        assert g.session_id == "s1"

    def test_grant_with_now_rejects_invalid_scope(self) -> None:
        with pytest.raises(ValueError, match="grant scope"):
            grant_with_now(
                capability="x",
                matcher="x",
                scope="forever",  # 非法
                source=DecisionSource.SESSION,
            )

    def test_session_grant_from_config_template_changes_scope_only(self) -> None:
        cfg = _build_config(allow_writes=["~/scratch/"])
        store = GrantStore.from_config(cfg)
        config_grant = store.config_grants()[0]

        session_copy = session_grant_from_config_template(config_grant, session_id="s1")
        assert session_copy.key == config_grant.key  # key 不动
        assert session_copy.scope == "session"
        assert session_copy.source == DecisionSource.SESSION
        assert session_copy.session_id == "s1"

    def test_grant_post_init_rejects_session_without_session_id(self) -> None:
        """Grant 数据结构强制：session scope 必须带 session_id。"""
        bad_key = GrantKey(
            capability="file_write",
            matcher="x/",
            boundary_kind=BoundaryKind.HOST,
        )
        with pytest.raises(ValueError, match="session_id"):
            Grant(
                key=bad_key,
                scope="session",
                source=DecisionSource.SESSION,
                created_at=0.0,
                session_id=None,
            )

    def test_grant_post_init_rejects_config_with_session_id(self) -> None:
        """Grant 数据结构强制：config(persist) scope 必须不带 session_id。"""
        bad_key = GrantKey(
            capability="file_write",
            matcher="x/",
            boundary_kind=BoundaryKind.HOST,
        )
        with pytest.raises(ValueError, match="must not carry session_id"):
            Grant(
                key=bad_key,
                scope="config",
                source=DecisionSource.CONFIG,
                created_at=0.0,
                session_id="s1",
            )

    def test_normalized_path_matcher_preserves_trailing_slash(self) -> None:
        assert normalized_path_matcher("./tests/integration/") == "tests/integration/"
        assert normalized_path_matcher("tests/foo") == "tests/foo"
        assert normalized_path_matcher("") == ""
