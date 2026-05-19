"""ApprovalManager 单元测试。

覆盖：

- request → resolve 正常路径（allow / deny）
- request → cancel 路径
- request → timeout 路径（fail-closed）
- cancel_by_thread 批量取消
- _PendingApproval 12 字段对齐 spec 20-data-model#1
- make_manager_prompt_fn 二态映射
- _decision_to_action remember_for_session 三态分支
- 单例工厂 + reset
- R9 lock 安全（lock 内不 await）
- R10 timeout 清理（resolve / cancel / timeout 三退出路径都清）
- ApprovalRules classify 集成 + 多 EventSink fan-out
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.contracts import ApprovalDecision, ApprovalRequest
from safety.approval_manager import (
    ApprovalEventSink,
    ApprovalManager,
    _decision_to_action,
    _PendingApproval,
    get_approval_manager,
    make_manager_prompt_fn,
    reset_for_testing,
)
from safety.approval_rules import ApprovalRules
from tools.approval import ApprovalAction

# ============ Fixtures ============


@pytest.fixture
def manager() -> ApprovalManager:
    """裸 manager 实例（不走单例工厂，避免跨测污染）。"""
    return ApprovalManager(rules=ApprovalRules(), default_timeout_ms=10_000)


@pytest.fixture(autouse=True)
def _reset_singleton() -> Iterator[None]:
    """每测前后重置全局单例。"""
    reset_for_testing()
    yield
    reset_for_testing()


def _make_request(call_id: str = "call-1", **overrides: Any) -> ApprovalRequest:
    """构造测试用 ApprovalRequest（必填字段一次性补齐）。"""
    defaults: dict[str, Any] = {
        "run_id": "run-1",
        "session_id": "sess-1",
        "turn": 0,
        "call_id": call_id,
        "tool_name": "Bash",
        "arguments": {"cmd": "ls"},
        "metadata": {"cwd": "/tmp"},
    }
    defaults.update(overrides)
    return ApprovalRequest(**defaults)


# ============ _PendingApproval 字段对齐（spec 20-data-model#1）============


def test_pending_approval_has_all_12_fields() -> None:
    """spec 20-data-model#1 12 字段必须有（阶段 2 接入不再改 dataclass）。

    用 MagicMock(spec=asyncio.Future) 替代真 Future（避免 new_event_loop + close
    污染下游 test_instruction_loader 的 get_event_loop()）。
    """
    fut: MagicMock = MagicMock(spec=asyncio.Future)
    p = _PendingApproval(
        request_id="r1",
        channel="generic_chat",
        thread_id="t1",
        cwd="/tmp",
        tool_name="Bash",
        tool_input={},
        metadata={},
        severity="standard",
        matched_rule=None,
        auto_approve_at_ms=None,
        auto_reject_at_ms=None,
        future=fut,
    )
    # 12 字段：核心 11 + arrived_at_ms（默认）+ timeout_ms（默认 None）
    for fld in (
        "request_id",
        "channel",
        "thread_id",
        "cwd",
        "tool_name",
        "tool_input",
        "metadata",
        "severity",
        "matched_rule",
        "auto_approve_at_ms",
        "auto_reject_at_ms",
        "future",
        "arrived_at_ms",
        "timeout_ms",
    ):
        assert hasattr(p, fld), f"missing field: {fld}"
    # arrived_at_ms 默认填充（毫秒级时间戳）
    assert isinstance(p.arrived_at_ms, int)
    assert p.arrived_at_ms > 0


# ============ request → resolve 正常路径 ============


async def test_request_then_resolve_allow_returns_approved(
    manager: ApprovalManager,
) -> None:
    """request 阻塞等 resolve；resolve(allow=True) 返回 outcome='approved'。"""
    sink: Any = AsyncMock(spec=ApprovalEventSink)
    manager.register_event_sink(sink)

    async def resolve_later() -> None:
        await asyncio.sleep(0.05)
        req_id = next(iter(manager._pending.keys()))
        assert manager.resolve(req_id, {"allow": True})

    _ = asyncio.create_task(resolve_later())  # noqa: RUF006
    decision = await manager.request(
        channel="generic_chat",
        thread_id="t1",
        cwd="/",
        tool_name="Bash",
        tool_input={"cmd": "ls"},
    )
    # contract Literal 用 "approved"（spec 文档漂移用 "allowed"）
    assert decision.outcome == "approved"
    assert decision.metadata.get("source") == "user"
    # R10：pending + timeout task 都清空
    assert manager.pending_count == 0
    assert manager.timeout_task_count == 0
    # EventSink 收到了 required + removed
    sink.emit_approval_required.assert_called_once()
    sink.emit_approval_removed.assert_called_once()
    # removed reason
    args = sink.emit_approval_removed.call_args
    assert args.kwargs["reason"] == "user_decided"


async def test_request_then_resolve_deny_returns_rejected(
    manager: ApprovalManager,
) -> None:
    """resolve(allow=False) 返回 rejected + 透传 message。"""

    async def resolve_later() -> None:
        await asyncio.sleep(0.05)
        req_id = next(iter(manager._pending.keys()))
        manager.resolve(req_id, {"allow": False, "message": "user denied"})

    _ = asyncio.create_task(resolve_later())  # noqa: RUF006
    decision = await manager.request(
        channel="generic_chat",
        thread_id="t1",
        cwd="/",
        tool_name="Bash",
        tool_input={},
    )
    assert decision.outcome == "rejected"
    assert decision.metadata.get("message") == "user denied"
    assert decision.metadata.get("source") == "user"


# ============ timeout 路径（fail-closed，reviewer 必修 #3）============


async def test_request_timeout_returns_rejected_with_manager_timeout_source() -> None:
    """timeout 触发 → outcome='rejected', source='manager_timeout', reason='timeout'。"""
    m = ApprovalManager(rules=ApprovalRules(default_timeout_ms=100), default_timeout_ms=100)
    decision = await m.request(
        channel="generic_chat",
        thread_id="t1",
        cwd="/",
        tool_name="X",
        tool_input={},
    )
    assert decision.outcome == "rejected"
    assert decision.metadata.get("source") == "manager_timeout"
    assert decision.metadata.get("reason") == "timeout"
    # R10：timeout 退出后清理
    assert m.pending_count == 0
    assert m.timeout_task_count == 0


# ============ cancel 路径 ============


async def test_cancel_returns_rejected_with_reason() -> None:
    """cancel(request_id) 让 pending future set_result(rejected, source='manager_cancel')。"""
    m = ApprovalManager(rules=ApprovalRules(), default_timeout_ms=10_000)

    async def cancel_later() -> None:
        await asyncio.sleep(0.05)
        req_id = next(iter(m._pending.keys()))
        assert m.cancel(req_id, reason="ws_disconnect")

    _ = asyncio.create_task(cancel_later())  # noqa: RUF006
    decision = await m.request(
        channel="generic_chat",
        thread_id="t1",
        cwd="/",
        tool_name="X",
        tool_input={},
    )
    assert decision.outcome == "rejected"
    assert decision.metadata.get("source") == "manager_cancel"
    assert decision.metadata.get("reason") == "ws_disconnect"
    assert m.pending_count == 0
    assert m.timeout_task_count == 0


def test_cancel_returns_false_on_unknown_request_id(
    manager: ApprovalManager,
) -> None:
    """cancel 未知 request_id → False。"""
    assert manager.cancel("non-existent") is False


def test_resolve_returns_false_on_unknown_request_id(
    manager: ApprovalManager,
) -> None:
    """resolve 未知 request_id → False。"""
    assert manager.resolve("non-existent", {"allow": True}) is False


# ============ cancel_by_thread（reviewer 必修 #4）============


async def test_cancel_by_thread_cancels_all_pending_under_that_thread() -> None:
    """cancel_by_thread(thread_id) 取消该 thread 名下所有 pending（不影响其他 thread）。"""
    m = ApprovalManager(rules=ApprovalRules(), default_timeout_ms=10_000)

    async def make_request(tid: str) -> ApprovalDecision:
        return await m.request(
            channel="generic_chat",
            thread_id=tid,
            cwd="/",
            tool_name="X",
            tool_input={},
        )

    t1_a = asyncio.create_task(make_request("thread-A"))
    t1_b = asyncio.create_task(make_request("thread-A"))
    t2 = asyncio.create_task(make_request("thread-B"))

    await asyncio.sleep(0.05)
    assert m.pending_count == 3

    cancelled = m.cancel_by_thread("thread-A", reason="cell_evict")
    assert cancelled == 2  # A 的两条

    d1 = await t1_a
    d2 = await t1_b
    assert d1.outcome == "rejected"
    assert d2.outcome == "rejected"
    assert d1.metadata.get("reason") == "cell_evict"

    # thread-B 仍 pending：用 cancel 收尾以免 leak
    remaining = next(iter(m._pending.keys()))
    m.cancel(remaining, reason="test_cleanup")
    d3 = await t2
    assert d3.outcome == "rejected"
    assert m.pending_count == 0
    assert m.timeout_task_count == 0


def test_cancel_by_thread_returns_zero_when_no_match(
    manager: ApprovalManager,
) -> None:
    """cancel_by_thread 找不到匹配 thread → 0。"""
    assert manager.cancel_by_thread("non-existent-thread") == 0


# ============ make_manager_prompt_fn 二态映射（reviewer 必修 #2）============


async def test_make_manager_prompt_fn_returns_accept_once_on_allow() -> None:
    """make_manager_prompt_fn 映射 approved → ACCEPT_ONCE。"""
    m = ApprovalManager(rules=ApprovalRules(), default_timeout_ms=10_000)
    prompt_fn = make_manager_prompt_fn(m, thread_id="t1")

    async def resolve_later() -> None:
        await asyncio.sleep(0.05)
        req_id = next(iter(m._pending.keys()))
        m.resolve(req_id, {"allow": True})

    _ = asyncio.create_task(resolve_later())  # noqa: RUF006
    action = await prompt_fn(_make_request())
    assert action == ApprovalAction.ACCEPT_ONCE


async def test_make_manager_prompt_fn_returns_reject_on_deny() -> None:
    """make_manager_prompt_fn 映射 rejected → REJECT。"""
    m = ApprovalManager(rules=ApprovalRules(), default_timeout_ms=10_000)
    prompt_fn = make_manager_prompt_fn(m, thread_id="t1")

    async def resolve_later() -> None:
        await asyncio.sleep(0.05)
        req_id = next(iter(m._pending.keys()))
        m.resolve(req_id, {"allow": False})

    _ = asyncio.create_task(resolve_later())  # noqa: RUF006
    action = await prompt_fn(_make_request())
    assert action == ApprovalAction.REJECT


def test_make_manager_prompt_fn_marked_action_aware() -> None:
    """prompt_fn 必须打 __action_aware__ 标记，被 InteractiveApproval 识别。"""
    m = ApprovalManager(rules=ApprovalRules())
    prompt_fn = make_manager_prompt_fn(m, thread_id="t1")
    assert getattr(prompt_fn, "__action_aware__", False) is True


def test_decision_to_action_remember_for_session_maps_correctly() -> None:
    """_decision_to_action 的 remember_for_session 分支 → ACCEPT_FOR_SESSION。"""
    d = ApprovalDecision(outcome="approved", metadata={"remember_for_session": True})
    assert _decision_to_action(d) == ApprovalAction.ACCEPT_FOR_SESSION


def test_decision_to_action_remember_persistent_maps_correctly() -> None:
    """_decision_to_action 的 remember_persistent 分支 → ACCEPT_PERSIST。"""
    d = ApprovalDecision(outcome="approved", metadata={"remember_persistent": True})
    assert _decision_to_action(d) == ApprovalAction.ACCEPT_PERSIST


def test_decision_to_action_plain_approved_maps_to_accept_once() -> None:
    """普通 approved 无 remember 标记 → ACCEPT_ONCE。"""
    d = ApprovalDecision(outcome="approved", metadata={})
    assert _decision_to_action(d) == ApprovalAction.ACCEPT_ONCE


def test_decision_to_action_rejected_maps_to_reject() -> None:
    """rejected → REJECT。"""
    d = ApprovalDecision(outcome="rejected", metadata={"reason": "timeout"})
    assert _decision_to_action(d) == ApprovalAction.REJECT


# ============ 单例工厂 ============


def test_get_approval_manager_singleton_idempotent() -> None:
    """get_approval_manager 首次调用构造；之后返回同实例。"""
    m1 = get_approval_manager()
    m2 = get_approval_manager()
    assert m1 is m2


def test_reset_for_testing_clears_singleton() -> None:
    """reset_for_testing 后再 get → 新实例。"""
    m1 = get_approval_manager()
    reset_for_testing()
    m2 = get_approval_manager()
    assert m1 is not m2


def test_get_approval_manager_accepts_custom_rules() -> None:
    """首次调用传入的 rules / timeout 生效。"""
    custom_rules = ApprovalRules(default_timeout_ms=5_000)
    m = get_approval_manager(rules=custom_rules, default_timeout_ms=5_000)
    assert m._rules is custom_rules
    assert m._default_timeout_ms == 5_000


# ============ R10 timeout 清理 ============


async def test_no_leaked_timeout_tasks_after_resolve() -> None:
    """resolve 后 timeout task 应被 cancel + pop（R10）。"""
    m = ApprovalManager(rules=ApprovalRules(), default_timeout_ms=10_000)

    async def resolve_later() -> None:
        await asyncio.sleep(0.05)
        req_id = next(iter(m._pending.keys()))
        m.resolve(req_id, {"allow": True})

    _ = asyncio.create_task(resolve_later())  # noqa: RUF006
    await m.request(
        channel="generic_chat",
        thread_id="t",
        cwd="/",
        tool_name="X",
        tool_input={},
    )

    # R10 监控：pending + timeout_tasks 都清空
    assert m.pending_count == 0
    assert m.timeout_task_count == 0


async def test_no_leaked_timeout_tasks_after_cancel() -> None:
    """cancel 后 timeout task 应被 cancel + pop（R10）。"""
    m = ApprovalManager(rules=ApprovalRules(), default_timeout_ms=10_000)

    async def cancel_later() -> None:
        await asyncio.sleep(0.05)
        req_id = next(iter(m._pending.keys()))
        m.cancel(req_id, reason="test")

    _ = asyncio.create_task(cancel_later())  # noqa: RUF006
    await m.request(
        channel="generic_chat",
        thread_id="t",
        cwd="/",
        tool_name="X",
        tool_input={},
    )

    assert m.pending_count == 0
    assert m.timeout_task_count == 0


# ============ 多 EventSink fan-out + sink 失败容错 ============


async def test_multiple_event_sinks_all_fan_out() -> None:
    """多 sink 全部收到 required / removed 通知。"""
    m = ApprovalManager(rules=ApprovalRules(), default_timeout_ms=10_000)
    sink1: Any = AsyncMock(spec=ApprovalEventSink)
    sink2: Any = AsyncMock(spec=ApprovalEventSink)
    m.register_event_sink(sink1)
    m.register_event_sink(sink2)

    async def resolve_later() -> None:
        await asyncio.sleep(0.05)
        req_id = next(iter(m._pending.keys()))
        m.resolve(req_id, {"allow": True})

    _ = asyncio.create_task(resolve_later())  # noqa: RUF006
    await m.request(
        channel="generic_chat",
        thread_id="t",
        cwd="/",
        tool_name="X",
        tool_input={},
    )

    sink1.emit_approval_required.assert_called_once()
    sink2.emit_approval_required.assert_called_once()
    sink1.emit_approval_removed.assert_called_once()
    sink2.emit_approval_removed.assert_called_once()


async def test_sink_emit_failure_does_not_break_main_flow() -> None:
    """sink 抛异常不影响审批主流程（R9 安全设计）。"""
    m = ApprovalManager(rules=ApprovalRules(), default_timeout_ms=10_000)
    bad_sink: Any = AsyncMock(spec=ApprovalEventSink)
    bad_sink.emit_approval_required.side_effect = RuntimeError("sink broken")
    bad_sink.emit_approval_removed.side_effect = RuntimeError("sink broken")
    m.register_event_sink(bad_sink)

    async def resolve_later() -> None:
        await asyncio.sleep(0.05)
        req_id = next(iter(m._pending.keys()))
        m.resolve(req_id, {"allow": True})

    _ = asyncio.create_task(resolve_later())  # noqa: RUF006
    decision = await m.request(
        channel="generic_chat",
        thread_id="t",
        cwd="/",
        tool_name="X",
        tool_input={},
    )
    # 主流程仍能拿到 decision，未被 sink 异常打断
    assert decision.outcome == "approved"
    assert m.pending_count == 0
    assert m.timeout_task_count == 0


# ============ rules.classify 集成 ============


async def test_pending_inherits_classify_severity_and_rule_info(
    manager: ApprovalManager,
) -> None:
    """_PendingApproval 字段由 rules.classify 填充（阶段 1 默认值）。"""
    sink: Any = AsyncMock(spec=ApprovalEventSink)
    manager.register_event_sink(sink)

    async def resolve_later() -> None:
        await asyncio.sleep(0.05)
        req_id = next(iter(manager._pending.keys()))
        manager.resolve(req_id, {"allow": True})

    _ = asyncio.create_task(resolve_later())  # noqa: RUF006
    await manager.request(
        channel="generic_chat",
        thread_id="t",
        cwd="/",
        tool_name="X",
        tool_input={},
    )
    # 阶段 1 ApprovalRules 默认：standard severity / 无规则命中 / 无自动倒计时
    pending: _PendingApproval = sink.emit_approval_required.call_args.kwargs["pending"]
    assert pending.severity == "standard"
    assert pending.matched_rule is None
    assert pending.auto_approve_at_ms is None
    assert pending.auto_reject_at_ms is None
