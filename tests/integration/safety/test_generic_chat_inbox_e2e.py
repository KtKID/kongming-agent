"""generic_chat → ApprovalManager → broadcaster → 模拟 ws resolve 全链路集成测。

任务来源：smart-approval-manager-stage1 task #6（dev-checklist 最关键项）。

覆盖 4 条路径（reviewer 必修 + R10 防护）：

1. ``test_full_path_user_allow_returns_accept_once``：正常用户决策路径
   （resolve allow → :class:`ApprovalAction.ACCEPT_ONCE`）
2. ``test_cell_evict_cancels_manager_pending_returns_reject``：关 tab cell evict 路径
   （:meth:`ApprovalManager.cancel_by_thread` → :class:`ApprovalAction.REJECT`）
3. ``test_timeout_returns_reject_fail_closed``：timeout 触发路径
   （fail-closed → :class:`ApprovalAction.REJECT`，``metadata.source='manager_timeout'``）
4. ``test_full_path_user_deny_returns_reject``：用户拒绝路径
   （resolve allow=False → :class:`ApprovalAction.REJECT`）

模拟 ws：不实际启 FastAPI TestClient（v2-inbox 集成测已覆盖 ws 端点路由），
本测试集中验证 manager + broadcaster + InboxEventSink + make_manager_prompt_fn
四件套的端到端集成（reviewer 必修 #6：与现网装配真实一致）。

装配点等价 :func:`web.run._make_runtime_factory` 的 ``_build_manager_and_inbox_sink``
helper（首次装配时把 InboxEventSink 注入 manager._event_sinks）。
"""

from __future__ import annotations

import asyncio

import pytest

from core.contracts import ApprovalAction, ApprovalRequest
from safety.approval.manager import (
    ApprovalManager,
    make_manager_prompt_fn,
    reset_for_testing,
)
from safety.approval.rules import ApprovalRules
from safety.inbox.event_sink import InboxEventSink
from web.approvals.global_inbox.broadcaster import (
    ApprovalInboxBroadcaster,
    reset_inbox_broadcaster_for_testing,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_singletons():
    """每个用例隔离 manager / broadcaster 全局单例（避免组合跑相互污染）。"""
    reset_for_testing()
    reset_inbox_broadcaster_for_testing()
    yield
    reset_for_testing()
    reset_inbox_broadcaster_for_testing()


def _make_integrated_setup(
    *,
    default_timeout_ms: int = 10_000,
) -> tuple[ApprovalManager, ApprovalInboxBroadcaster]:
    """装配 manager + broadcaster + InboxEventSink（等价于现网 ``web.run`` 装配）。

    本 helper 不走 ``get_approval_manager`` 单例（用 fresh 实例）：
    集成测 fixture 已 reset_for_testing，不需要再走 per-process 单例。
    """
    broadcaster = ApprovalInboxBroadcaster()
    manager = ApprovalManager(
        # approval-rules-unified：ApprovalRules 不再持 timeout；policy=None
        # 时走 fail-closed 60s（本 e2e 测覆盖 user 路径，不依赖 auto-approve）。
        rules=ApprovalRules(),
        default_timeout_ms=default_timeout_ms,
    )
    sink = InboxEventSink(broadcaster=broadcaster, manager=manager)
    manager.register_event_sink(sink)
    return manager, broadcaster


def _make_approval_request(
    *,
    tool_name: str = "Bash",
    arguments: dict[str, str] | None = None,
    call_id: str = "call-test-1",
) -> ApprovalRequest:
    """构造测试用 ApprovalRequest（填齐 frozen dataclass 必填字段）。"""
    return ApprovalRequest(
        run_id="run-test-1",
        session_id="sess-test-1",
        turn=1,
        call_id=call_id,
        tool_name=tool_name,
        arguments=arguments or {"command": "ls"},
        reason="integration-test",
        metadata={"cwd": "/tmp"},
    )


# ---------------------------------------------------------------------------
# 场景 1：正常用户决策 → ACCEPT_ONCE
# ---------------------------------------------------------------------------


async def test_full_path_user_allow_returns_accept_once() -> None:
    """场景 1：generic_chat prompt_fn → manager.request → broadcaster.emit_add →
    模拟前端 ws 收到帧 → broadcaster.resolve(allow=True) → manager.resolve →
    future set_result → prompt_fn 返回 ACCEPT_ONCE。

    断言点（spec 60-risk R10）：
    - prompt_fn 返回 ``ACCEPT_ONCE``
    - manager.pending_count == 0（cleanup 跑过）
    - manager.timeout_task_count == 0（timeout task 被 cancel）
    """
    manager, broadcaster = _make_integrated_setup()

    prompt_fn = make_manager_prompt_fn(manager, thread_id="t-allow")

    async def simulate_user_resolve() -> None:
        """模拟前端 ws 收到 ``approval.inbox.add`` 帧后用户点同意，发回 resolve。"""
        # 等 prompt_fn 把 pending 写进 manager._pending，并且 InboxEventSink
        # 已把 manager.resolve 注册进 broadcaster._resolve_targets（sink.emit_approval_required
        # 在 manager.request 的 lock 外 gather 里跑，与 pending 注册不严格同步）
        for _ in range(100):
            if manager.pending_count > 0 and "t-allow" in broadcaster._resolve_targets:
                break
            await asyncio.sleep(0.01)
        assert manager.pending_count > 0, "pending should be registered"
        assert "t-allow" in broadcaster._resolve_targets, (
            "InboxEventSink should have registered manager.resolve as target"
        )
        # 通过 broadcaster.resolve 走真实路由路径（thread_id → manager.resolve）
        req_id = next(iter(manager._pending.keys()))
        ok = broadcaster.resolve("t-allow", req_id, {"allow": True})
        assert ok, "broadcaster.resolve should find the target callback"

    resolve_task = asyncio.create_task(simulate_user_resolve())
    action = await prompt_fn(_make_approval_request())
    await resolve_task

    assert action == ApprovalAction.ACCEPT_ONCE
    assert manager.pending_count == 0
    assert manager.timeout_task_count == 0


# ---------------------------------------------------------------------------
# 场景 2：关 tab → cancel_by_thread → REJECT（reviewer 必修 #4 核心）
# ---------------------------------------------------------------------------


async def test_cell_evict_cancels_manager_pending_returns_reject() -> None:
    """场景 2：用户关 tab → :meth:`WebHostAdapter.close` 调
    :meth:`ApprovalManager.cancel_by_thread` → 所有该 thread 名下 pending → REJECT。

    reviewer 必修 #4 核心场景（避免用户关 tab 后 pending 卡 60s timeout / R10）。
    本测试直接调 ``manager.cancel_by_thread``（与 WebHostAdapter.close 内部行为一致），
    不依赖 WebHostAdapter 实例，避免 ws / fanout 干扰。
    """
    manager, _broadcaster = _make_integrated_setup()

    prompt_fn = make_manager_prompt_fn(manager, thread_id="t-evict")

    async def simulate_close_tab() -> None:
        """模拟 :meth:`WebHostAdapter.close` 在用户关 tab / cell evict 时调度。"""
        for _ in range(50):
            if manager.pending_count > 0:
                break
            await asyncio.sleep(0.01)
        assert manager.pending_count > 0, "pending should be registered"
        cancelled = manager.cancel_by_thread("t-evict", reason="cell_evict")
        assert cancelled >= 1, "cancel_by_thread should hit at least one pending"

    evict_task = asyncio.create_task(simulate_close_tab())
    action = await prompt_fn(_make_approval_request())
    await evict_task

    assert action == ApprovalAction.REJECT
    assert manager.pending_count == 0
    assert manager.timeout_task_count == 0


# ---------------------------------------------------------------------------
# 场景 3：timeout 触发 → fail-closed REJECT
# ---------------------------------------------------------------------------


async def test_timeout_returns_reject_fail_closed() -> None:
    """场景 3：超时未决策 → fail-closed REJECT
    （与 :meth:`WebHostAdapter.prompt_approval` 的 TimeoutError 分支行为一致）。

    reviewer 必修 #3：timeout 返回 ``ApprovalAction.REJECT`` 且不卡 pending。
    用极短 timeout（100ms）触发，避免测试阻塞。
    """
    manager, _broadcaster = _make_integrated_setup(default_timeout_ms=100)

    prompt_fn = make_manager_prompt_fn(manager, thread_id="t-timeout")

    action = await prompt_fn(_make_approval_request())

    assert action == ApprovalAction.REJECT
    assert manager.pending_count == 0
    assert manager.timeout_task_count == 0


# ---------------------------------------------------------------------------
# 场景 4：用户拒绝 → REJECT
# ---------------------------------------------------------------------------


async def test_full_path_user_deny_returns_reject() -> None:
    """场景 4：用户点拒绝 → manager.resolve(allow=False) → REJECT。

    与场景 1 路径对称，仅 decision dict allow=False；
    断言 prompt_fn 映射为 ``ApprovalAction.REJECT``。
    """
    manager, broadcaster = _make_integrated_setup()

    prompt_fn = make_manager_prompt_fn(manager, thread_id="t-deny")

    async def simulate_user_deny() -> None:
        for _ in range(100):
            if manager.pending_count > 0 and "t-deny" in broadcaster._resolve_targets:
                break
            await asyncio.sleep(0.01)
        assert manager.pending_count > 0
        assert "t-deny" in broadcaster._resolve_targets
        req_id = next(iter(manager._pending.keys()))
        ok = broadcaster.resolve("t-deny", req_id, {"allow": False, "message": "user denied"})
        assert ok

    deny_task = asyncio.create_task(simulate_user_deny())
    action = await prompt_fn(_make_approval_request(tool_name="Edit", arguments={"path": "/etc/x"}))
    await deny_task

    assert action == ApprovalAction.REJECT
    assert manager.pending_count == 0
    assert manager.timeout_task_count == 0
