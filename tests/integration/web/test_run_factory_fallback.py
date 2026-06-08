"""thread-cwd-fallback + approval-rules-unified 集成测：``src/web/run.py`` factory 装配链。

覆盖两件事：

1. ``_build_manager_and_inbox_sink`` 把 ``app.state.auto_approval_policy`` 完整
   注入 :class:`ApprovalRules._policy`——证明 generic_chat 通道与 claude_code
   共享同一份策略真源（24 规则 + per-cwd 总开关）。
   approval-rules-unified 删除了原 ``default_timeout_ms`` 串通逻辑（manager 走
   默认 60s 即可，``cfg.web.pending_approval_timeout_seconds`` 仅归 host_adapter
   用），本套用例同步去除老断言。
2. ``_resolve_default_cwd_for_thread`` 让 generic_chat 通道 prompt_fn 装配时
   把 ``thread.cwd → server cwd → 进程 cwd`` 三级 fallback 装入；空 thread.cwd
   场景下仍能命中 cwd 自动通过规则。

不依赖 FastAPI TestClient：构造 stub app + stub ThreadManager + 真实
``AutoApprovalPolicy + ConfigStore``，集中验证装配点行为。
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hosts.web.approvals.auto.config_store import ConfigStore, ProjectConfig
from hosts.web.run import (
    _build_manager_and_inbox_sink,
    _resolve_default_cwd_for_thread,
)
from hosts.web.threads.metadata import ThreadMetadata
from safety.approval.manager import (
    ApprovalManager,
    make_manager_prompt_fn,
    reset_for_testing,
)

# ---------------------------------------------------------------------------
# fixtures / 小工具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_manager_singleton() -> Iterator[None]:
    """每用例隔离 ApprovalManager 单例，避免组合跑相互污染。"""
    reset_for_testing()
    yield
    reset_for_testing()


def _make_thread_meta(thread_id: str = "thread-abc123def456", *, cwd: str = "") -> ThreadMetadata:
    """构造一份最小 ThreadMetadata（满足 id 正则 + 必填字段）。"""
    return ThreadMetadata(
        id=thread_id,
        name="t",
        preset_id="default",
        cwd=cwd,
        created_at=time.time(),
        updated_at=time.time(),
    )


class _StubThreadManager:
    """duck-typed ThreadManager：仅暴露 ``list_threads``。"""

    def __init__(self, metas: list[ThreadMetadata]) -> None:
        self._metas = metas

    def list_threads(self) -> list[ThreadMetadata]:
        return list(self._metas)


def _make_app(
    *,
    thread_metas: list[ThreadMetadata] | None = None,
    workspace_root: Path | None = None,
    policy: object | None = None,
) -> SimpleNamespace:
    """构造最小 stub app（带 ``state.thread_manager`` / ``state.workspace_root``
    / 可选 ``state.auto_approval_policy``）。"""
    tm = _StubThreadManager(thread_metas or [])
    state = SimpleNamespace(
        thread_manager=tm,
        workspace_root=workspace_root or Path("/proj/server-root"),
    )
    if policy is not None:
        state.auto_approval_policy = policy
    return SimpleNamespace(state=state)


# ---------------------------------------------------------------------------
# 用例 1：_build_manager_and_inbox_sink 装配契约
# ---------------------------------------------------------------------------


def test_manager_build_fail_safe_when_policy_missing() -> None:
    """approval-rules-unified：``app.state.auto_approval_policy`` 缺失
    （test 环境 / lifespan 漂移）→ ``ApprovalRules._policy is None``，
    走 fail-closed 默认 ask + 60s（不抛错）。

    生产路径正常装配时 policy 必就绪（由 ``create_app`` lifespan 写入
    ``app.state.auto_approval_policy``）；本测覆盖"安全网"路径。
    """
    app = _make_app(policy=None)  # 故意不挂 policy
    manager = _build_manager_and_inbox_sink(app=app)

    assert isinstance(manager, ApprovalManager)
    assert manager._rules._policy is None
    # manager 仍按默认 60s 运行
    assert manager._default_timeout_ms == 60_000


def test_manager_build_injects_policy_when_present() -> None:
    """approval-rules-unified 架构契约：policy 在场时，``ApprovalRules._policy``
    指向**同一份** policy 对象（generic_chat 与 claude_code 共享真源）。

    此前版本只注入 ``config_store``；改造后注入完整 policy，让 generic_chat
    通道走 ``policy.classify`` 的 24 规则匹配。
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        # 用真实 AutoApprovalPolicy（验证 duck typing 不漂移）
        from hosts.web.approvals.auto.policy import AutoApprovalPolicy
        from hosts.web.approvals.auto.rules import RuleSet

        store = ConfigStore(Path(tmp_dir))
        rule_set = RuleSet(version=1, default_timeout_ms=10_000, rules=())
        policy = AutoApprovalPolicy(rule_set, store)

        app = _make_app(policy=policy)
        manager = _build_manager_and_inbox_sink(app=app)

        # 关键断言：policy 完整对象注入，不是只拿 config_store
        assert manager._rules._policy is policy, (
            "ApprovalRules._policy must be the AutoApprovalPolicy instance "
            "(generic_chat 与 claude_code 共享同一份策略真源)"
        )
        # 历史断言已删：rules 不再有 _config_reader / _default_timeout_ms 字段
        assert not hasattr(manager._rules, "_config_reader")
        assert not hasattr(manager._rules, "_default_timeout_ms")


# ---------------------------------------------------------------------------
# 用例 2：_resolve_default_cwd_for_thread —— thread.cwd / fallback 链
# ---------------------------------------------------------------------------


def test_resolve_default_cwd_uses_thread_meta_when_set() -> None:
    """thread.cwd 非空 → 直接返回（不走 server fallback）。"""
    meta = _make_thread_meta(cwd="/proj/bound")
    app = _make_app(thread_metas=[meta], workspace_root=Path("/proj/server-root"))
    resolved = _resolve_default_cwd_for_thread(app, meta.id)
    assert resolved == "/proj/bound"


def test_resolve_default_cwd_falls_back_to_server_root_when_thread_cwd_empty() -> None:
    """thread.cwd 空 → fallback 到 server 启动目录（``app.state.workspace_root``）。

    覆盖最重要的 user-facing 场景：用户没绑 cwd 的"纯聊天"thread。
    """
    meta = _make_thread_meta(cwd="")
    app = _make_app(thread_metas=[meta], workspace_root=Path("/proj/server-root"))
    resolved = _resolve_default_cwd_for_thread(app, meta.id)
    assert resolved == "/proj/server-root"


def test_resolve_default_cwd_falls_back_when_thread_metadata_missing() -> None:
    """thread metadata 查不到（list_threads 返空） → 显式降级到 server cwd。"""
    app = _make_app(thread_metas=[], workspace_root=Path("/proj/server-root"))
    resolved = _resolve_default_cwd_for_thread(app, "thread-abc123def456")
    assert resolved == "/proj/server-root"


def test_resolve_default_cwd_falls_back_when_app_missing() -> None:
    """app=None（runtime_factory 尚未回挂时） → 退到进程 cwd。"""
    resolved = _resolve_default_cwd_for_thread(None, "thread-abc123def456")
    assert resolved  # 非空兜底
    assert Path(resolved) == Path.cwd()


# ---------------------------------------------------------------------------
# 用例 3：装配链端到端 —— 空 thread.cwd 通过 prompt_fn 命中 cwd 自动通过规则
# ---------------------------------------------------------------------------


def _make_real_policy(store: ConfigStore) -> Any:
    """构造真实 ``AutoApprovalPolicy``（空规则集，仅依赖 cwd 总开关 + auto_eligible）。

    端到端用例用真实 policy 验证 duck typing 不漂移 + cwd 总开关串通。
    本套测不验证 24 规则匹配（rm 命中走 ``tests/integration/safety/...``）。
    """
    from hosts.web.approvals.auto.policy import AutoApprovalPolicy
    from hosts.web.approvals.auto.rules import RuleSet

    rule_set = RuleSet(version=1, default_timeout_ms=10_000, rules=())
    return AutoApprovalPolicy(rule_set, store)


@pytest.mark.asyncio
async def test_prompt_fn_uses_default_cwd_when_thread_cwd_empty_end_to_end() -> None:
    """端到端：thread.cwd="" + workspace_root="/proj/server-root" →
    prompt_fn 调 manager.request 时用 "/proj/server-root" 作 cwd，
    命中 ConfigStore 中该 cwd 的 enabled=True 配置 → 自动通过。

    证据链：

    1. ``_resolve_default_cwd_for_thread`` 返回 "/proj/server-root"
    2. ``make_manager_prompt_fn(default_cwd="/proj/server-root")``
    3. ``req.metadata.cwd`` 空 → prompt_fn 用 default_cwd
    4. ConfigStore 已把 "/proj/server-root" 配 enabled=True →
       ``ApprovalRules.classify`` 触发 auto_allow → 自动通过
    """
    from core.contracts import ApprovalAction, ApprovalRequest

    with tempfile.TemporaryDirectory() as tmp_dir:
        store = ConfigStore(Path(tmp_dir))
        # 关键：把 server cwd 配为 auto-allow（短 timeout 保证不等真用户）
        store.set(ProjectConfig(cwd="/proj/server-root", enabled=True, timeout_ms=50))
        policy = _make_real_policy(store)

        meta = _make_thread_meta(cwd="")  # 空 thread cwd，触发 fallback
        app = _make_app(
            thread_metas=[meta],
            workspace_root=Path("/proj/server-root"),
            policy=policy,
        )

        # 模拟 factory 内的装配
        default_cwd = _resolve_default_cwd_for_thread(app, meta.id)
        assert default_cwd == "/proj/server-root"

        manager = _build_manager_and_inbox_sink(app=app)
        prompt_fn = make_manager_prompt_fn(
            manager,
            meta.id,
            default_cwd=default_cwd,
        )

        # 构造审批请求（metadata.cwd 不传，触发 default_cwd 走 fallback 链）
        req = ApprovalRequest(
            run_id="run-test",
            session_id=meta.id,
            turn=1,
            call_id="call-1",
            tool_name="Bash",
            arguments={"cmd": "ls"},
            metadata={},  # 故意不带 cwd
        )
        action = await prompt_fn(req)
        # 命中 auto_allow → ACCEPT_ONCE（不是 REJECT）
        assert action == ApprovalAction.ACCEPT_ONCE
        # manager 已清干净（pending / tasks 全清）
        assert manager.pending_count == 0
        assert manager.timeout_task_count == 0
        assert manager.auto_approve_task_count == 0


@pytest.mark.asyncio
async def test_prompt_fn_thread_cwd_takes_priority_over_default() -> None:
    """优先级：thread.cwd 非空时不走 server fallback。

    构造一个 thread.cwd="/proj/bound"，server cwd 是另一个，
    ConfigStore 只把 "/proj/bound" 配 enabled=True；
    走装配链后 prompt_fn 应命中 "/proj/bound"（而不是 server cwd）→ 自动通过。
    """
    from core.contracts import ApprovalAction, ApprovalRequest

    with tempfile.TemporaryDirectory() as tmp_dir:
        store = ConfigStore(Path(tmp_dir))
        store.set(ProjectConfig(cwd="/proj/bound", enabled=True, timeout_ms=50))
        # 关键：server cwd 不在 store 里，命中 server cwd 不会自动通过
        policy = _make_real_policy(store)

        meta = _make_thread_meta(cwd="/proj/bound")
        app = _make_app(
            thread_metas=[meta],
            workspace_root=Path("/proj/server-root"),
            policy=policy,
        )

        default_cwd = _resolve_default_cwd_for_thread(app, meta.id)
        assert default_cwd == "/proj/bound"  # 走 thread.cwd，不 fallback

        manager = _build_manager_and_inbox_sink(app=app)
        prompt_fn = make_manager_prompt_fn(
            manager,
            meta.id,
            default_cwd=default_cwd,
        )
        req = ApprovalRequest(
            run_id="run-test",
            session_id=meta.id,
            turn=1,
            call_id="call-1",
            tool_name="Bash",
            arguments={"cmd": "ls"},
            metadata={},
        )
        action = await prompt_fn(req)
        assert action == ApprovalAction.ACCEPT_ONCE
