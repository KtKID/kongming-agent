"""WebHostAdapter 单测（Phase 1 #6）。

覆盖 :class:`web.app_support.host_adapter.WebHostAdapter` 的全部公开行为：

- ``write_output`` / ``notify_event`` 行为
- ``prompt_approval`` 三路径：成功 ack / 超时 / cell evict cancel
- ``resolve_approval`` 重放守卫 / call_id 不存在容错
- ``read_input`` 永远抛 NotImplementedError
- ``close`` 幂等 + 把 pending future resolve(False)
- ``attach_ws`` 重置 closed
- ``_safe_send_json`` 异常吞咽 + 标 closed
- 重复 ``call_id`` 抛 RuntimeError（保护安全链路）

stub WS：用 ``unittest.mock.AsyncMock`` 的 ``send_json`` / ``close``，
满足 :class:`web.app_support.host_adapter._WSSendable` Protocol。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.contracts import ApprovalAction, ApprovalRequest, Event
from web.app_support.host_adapter import WebHostAdapter


def _make_ws() -> AsyncMock:
    """构造一个 duck-typed WS mock。"""
    ws = AsyncMock()
    ws.send_json = AsyncMock(return_value=None)
    ws.close = AsyncMock(return_value=None)
    return ws


def _make_request(call_id: str = "call-1", tool_name: str = "ReadFile") -> ApprovalRequest:
    return ApprovalRequest(
        run_id="run-1",
        session_id="thread-aaaaaaaaaaaa",
        turn=2,
        call_id=call_id,
        tool_name=tool_name,
        arguments={"path": "/tmp/x"},
        reason="test",
    )


# ---------------------------------------------------------------------------
# read_input / notify_event 基本契约
# ---------------------------------------------------------------------------


async def test_read_input_raises_not_implemented() -> None:
    adapter = WebHostAdapter(_make_ws())
    with pytest.raises(NotImplementedError):
        await adapter.read_input()


async def test_notify_event_is_noop() -> None:
    """notify_event 不应推 WS（统一走 WSEventSink），也不抛。"""
    ws = _make_ws()
    adapter = WebHostAdapter(ws)
    event = Event(kind="turn.start", run_id="r", turn=1)
    await adapter.notify_event(event)
    ws.send_json.assert_not_called()


# ---------------------------------------------------------------------------
# write_output
# ---------------------------------------------------------------------------


async def test_write_output_pushes_assistant_final_frame() -> None:
    ws = _make_ws()
    adapter = WebHostAdapter(ws)
    await adapter.write_output("hello world")
    ws.send_json.assert_awaited_once()
    payload = ws.send_json.await_args.args[0]
    assert payload["frame_type"] == "assistant.final"
    assert payload["content"] == "hello world"
    assert payload["turn"] == -1
    assert isinstance(payload["timestamp_ms"], int)


async def test_write_output_silent_when_closed() -> None:
    ws = _make_ws()
    adapter = WebHostAdapter(ws)
    await adapter.close()
    await adapter.write_output("after-close")
    ws.send_json.assert_not_called()


# ---------------------------------------------------------------------------
# prompt_approval — 三路径 resolve
# ---------------------------------------------------------------------------


async def test_prompt_approval_accept_once_via_ack() -> None:
    """v0.1.6 三态：ACCEPT_ONCE ack 路径。"""
    ws = _make_ws()
    adapter = WebHostAdapter(ws, pending_approval_timeout_seconds=5.0)
    request = _make_request("call-success")

    async def _ack() -> None:
        # 让 prompt_approval 先进 wait_for
        await asyncio.sleep(0.05)
        adapter.resolve_approval("call-success", ApprovalAction.ACCEPT_ONCE)

    ack_task = asyncio.create_task(_ack())
    result = await adapter.prompt_approval(request)
    assert result is ApprovalAction.ACCEPT_ONCE
    await ack_task
    # request 帧已发出
    ws.send_json.assert_awaited_once()
    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "approval.request"
    assert sent["call_id"] == "call-success"
    assert sent["tool_name"] == "ReadFile"
    # finally 块已清理 pending
    assert adapter.pending_approval_count == 0


async def test_prompt_approval_accept_for_session_via_ack() -> None:
    """v0.1.6 三态：ACCEPT_FOR_SESSION ack 路径（"本 session 同意"按钮）。"""
    ws = _make_ws()
    adapter = WebHostAdapter(ws, pending_approval_timeout_seconds=5.0)
    request = _make_request("call-session")

    async def _ack() -> None:
        await asyncio.sleep(0.05)
        adapter.resolve_approval("call-session", ApprovalAction.ACCEPT_FOR_SESSION)

    ack_task = asyncio.create_task(_ack())
    result = await adapter.prompt_approval(request)
    assert result is ApprovalAction.ACCEPT_FOR_SESSION
    await ack_task
    assert adapter.pending_approval_count == 0


async def test_prompt_approval_rejected_via_ack() -> None:
    ws = _make_ws()
    adapter = WebHostAdapter(ws, pending_approval_timeout_seconds=5.0)
    request = _make_request("call-reject")

    async def _ack() -> None:
        await asyncio.sleep(0.05)
        adapter.resolve_approval("call-reject", ApprovalAction.REJECT)

    ack_task = asyncio.create_task(_ack())
    result = await adapter.prompt_approval(request)
    assert result is ApprovalAction.REJECT
    await ack_task
    assert adapter.pending_approval_count == 0


async def test_prompt_approval_timeout_returns_reject() -> None:
    """超时 fail-safe：返回 ApprovalAction.REJECT（语义不变，类型从 bool 升级）。"""
    ws = _make_ws()
    # 把 timeout 调到极小，避免单测拖时间
    adapter = WebHostAdapter(ws, pending_approval_timeout_seconds=0.05)
    request = _make_request("call-timeout")
    result = await adapter.prompt_approval(request)
    assert result is ApprovalAction.REJECT
    assert adapter.pending_approval_count == 0


async def test_prompt_approval_cancel_via_close_returns_reject() -> None:
    """close() 期间 cancel 所有 pending future → REJECT。"""
    ws = _make_ws()
    adapter = WebHostAdapter(ws, pending_approval_timeout_seconds=5.0)
    request = _make_request("call-cancel")

    async def _close() -> None:
        await asyncio.sleep(0.05)
        await adapter.close()

    close_task = asyncio.create_task(_close())
    result = await adapter.prompt_approval(request)
    assert result is ApprovalAction.REJECT
    await close_task


async def test_prompt_approval_external_task_cancel_propagates() -> None:
    """interrupt 路径（外部 task.cancel()）必须把 CancelledError 透传给 runner，
    **不能**翻译成 REJECT，否则 runner 会以为"用户拒绝了 1 个工具"继续跑下一轮，
    与 interrupt 语义冲突。

    场景：runner 在 await prompt_approval(...) 阻塞时，外部（如 ws.py 收到
    InterruptFrame）调 ``task.cancel()``。此时 future 未 done，wait_for
    抛 CancelledError，本测试验证它**不**被吞成 REJECT。
    """
    ws = _make_ws()
    adapter = WebHostAdapter(ws, pending_approval_timeout_seconds=5.0)
    request = _make_request("call-external-cancel")

    async def _runner_call() -> ApprovalAction:
        return await adapter.prompt_approval(request)

    task = asyncio.create_task(_runner_call())
    # 让 prompt_approval 走到 wait_for（注册 pending future + 推 ws）
    await asyncio.sleep(0.05)
    assert adapter.pending_approval_count == 1

    # 外部 cancel（模拟 ws.py 收到 InterruptFrame → cell.current_run_task.cancel()）
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # finally 块仍要清掉 pending 字典（避免泄漏）
    assert adapter.pending_approval_count == 0
    # adapter 不应被 cancel 标 closed（cancel 不等于断连）
    assert adapter.closed is False


async def test_prompt_approval_when_already_closed_returns_reject() -> None:
    ws = _make_ws()
    adapter = WebHostAdapter(ws)
    await adapter.close()
    request = _make_request("call-closed")
    result = await adapter.prompt_approval(request)
    assert result is ApprovalAction.REJECT
    ws.send_json.assert_not_called()


async def test_prompt_approval_duplicate_call_id_raises() -> None:
    """同 call_id 二次进入要早失败 — 上游 runner 应当保证唯一。"""
    ws = _make_ws()
    adapter = WebHostAdapter(ws, pending_approval_timeout_seconds=5.0)
    request = _make_request("call-dup")

    # 先抢占字典
    loop = asyncio.get_running_loop()
    adapter._pending_approvals[request.call_id] = loop.create_future()

    with pytest.raises(RuntimeError, match="duplicate approval call_id"):
        await adapter.prompt_approval(request)


# ---------------------------------------------------------------------------
# resolve_approval 容错
# ---------------------------------------------------------------------------


async def test_resolve_approval_unknown_call_id_silently_dropped() -> None:
    adapter = WebHostAdapter(_make_ws())
    # 不应抛
    adapter.resolve_approval("never-sent", ApprovalAction.ACCEPT_ONCE)


async def test_resolve_approval_replay_after_done_silently_dropped() -> None:
    """ack 重放：第一次 set_result 后第二次应静默丢弃。"""
    ws = _make_ws()
    adapter = WebHostAdapter(ws, pending_approval_timeout_seconds=5.0)
    request = _make_request("call-replay")

    async def _ack_twice() -> None:
        await asyncio.sleep(0.05)
        adapter.resolve_approval("call-replay", ApprovalAction.ACCEPT_ONCE)
        # 第二次重放
        adapter.resolve_approval("call-replay", ApprovalAction.REJECT)

    ack_task = asyncio.create_task(_ack_twice())
    result = await adapter.prompt_approval(request)
    # 第一次胜出
    assert result is ApprovalAction.ACCEPT_ONCE
    await ack_task


# ---------------------------------------------------------------------------
# close 幂等
# ---------------------------------------------------------------------------


async def test_close_is_idempotent() -> None:
    adapter = WebHostAdapter(_make_ws())
    await adapter.close()
    assert adapter.closed is True
    await adapter.close()
    assert adapter.closed is True


# ---------------------------------------------------------------------------
# close → ApprovalManager.cancel_by_thread wiring（stage1 reviewer 必修 #4）
# ---------------------------------------------------------------------------


async def test_close_with_thread_id_calls_manager_cancel_by_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """thread_id 传入时，close() 调 manager.cancel_by_thread(thread_id, reason='cell_evict')。

    防止 reviewer 必修 #4 wiring 漂移：用户关 tab → WebHostAdapter.close →
    ApprovalManager 清该 thread 名下所有 pending（否则卡 60s timeout，R10 内存泄漏诱因）。
    """
    from unittest.mock import MagicMock

    manager_spy = MagicMock()
    manager_spy.cancel_by_thread = MagicMock(return_value=2)  # 假装清掉 2 条 pending

    # 注：源码内是 `from safety.approval.manager import get_approval_manager`（局部 import）
    import safety.approval.manager as approval_manager_mod

    monkeypatch.setattr(approval_manager_mod, "get_approval_manager", lambda: manager_spy)

    adapter = WebHostAdapter(_make_ws(), thread_id="t-real")
    await adapter.close()

    manager_spy.cancel_by_thread.assert_called_once_with("t-real", reason="cell_evict")


async def test_close_without_thread_id_skips_manager_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """thread_id 未传时（向后兼容老调用），close() 不调 manager.cancel_by_thread。

    避免破坏旧测试 / CLI 装配点 / 任何不需要 inbox 路径的场景。
    """
    from unittest.mock import MagicMock

    manager_spy = MagicMock()
    manager_spy.cancel_by_thread = MagicMock()

    import safety.approval.manager as approval_manager_mod

    monkeypatch.setattr(approval_manager_mod, "get_approval_manager", lambda: manager_spy)

    adapter = WebHostAdapter(_make_ws())  # 不传 thread_id
    await adapter.close()

    manager_spy.cancel_by_thread.assert_not_called()


# ---------------------------------------------------------------------------
# attach_ws 重连
# ---------------------------------------------------------------------------


async def test_attach_ws_replaces_reference_and_resets_closed() -> None:
    old_ws = _make_ws()
    new_ws = _make_ws()
    adapter = WebHostAdapter(old_ws)
    await adapter.close()
    assert adapter.closed is True

    adapter.attach_ws(new_ws)
    assert adapter.closed is False

    await adapter.write_output("after-reconnect")
    new_ws.send_json.assert_awaited_once()
    old_ws.send_json.assert_not_called()


# ---------------------------------------------------------------------------
# _safe_send_json 失败容错
# ---------------------------------------------------------------------------


async def test_safe_send_json_marks_closed_on_send_failure() -> None:
    ws = _make_ws()
    ws.send_json = AsyncMock(side_effect=ConnectionError("ws gone"))
    adapter = WebHostAdapter(ws)
    # write_output 触发 _safe_send_json 失败
    await adapter.write_output("first")
    assert adapter.closed is True
    # 后续再发不会抛
    await adapter.write_output("second")


async def test_safe_send_json_best_effort_close_does_not_raise() -> None:
    """send 失败后 best-effort ws.close() 也不抛（即使 close 也异常）。"""
    ws = _make_ws()
    ws.send_json = AsyncMock(side_effect=RuntimeError("kaboom"))
    ws.close = AsyncMock(side_effect=RuntimeError("close kaboom"))
    adapter = WebHostAdapter(ws)
    await adapter.write_output("x")  # 不抛
    assert adapter.closed is True


async def test_pending_approval_count_property() -> None:
    ws = _make_ws()
    adapter = WebHostAdapter(ws, pending_approval_timeout_seconds=5.0)
    request = _make_request("call-count")

    async def _ack() -> None:
        # 在 ack 之前观测 count == 1
        await asyncio.sleep(0.02)
        assert adapter.pending_approval_count == 1
        adapter.resolve_approval("call-count", True)

    task = asyncio.create_task(_ack())
    await adapter.prompt_approval(request)
    await task
    assert adapter.pending_approval_count == 0


# ---------------------------------------------------------------------------
# write_output 在 send 失败后不抛（防止主链路被污染）
# ---------------------------------------------------------------------------


async def test_write_output_swallows_arbitrary_exception() -> None:
    ws = _make_ws()
    ws.send_json = AsyncMock(side_effect=ValueError("anything"))
    adapter = WebHostAdapter(ws)
    # 不抛 ValueError
    await adapter.write_output("x")
    assert adapter.closed is True


# ---------------------------------------------------------------------------
# write_output 校验 turn / timestamp 字段类型
# ---------------------------------------------------------------------------


async def test_write_output_timestamp_is_milliseconds() -> None:
    ws = _make_ws()
    adapter = WebHostAdapter(ws)
    await adapter.write_output("hi")
    payload: dict[str, Any] = ws.send_json.await_args.args[0]
    # 13 位毫秒级（不严格断定 13 位，但要求是较大整数）
    assert payload["timestamp_ms"] > 1_000_000_000_000
