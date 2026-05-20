"""集成：A tab 触发 interrupt → 同 thread 名下所有 ws（含 B tab）都收到
RunInterruptedFrame（interrupt-run-v0.1 #14）。

链路验证：
- runner 顶层 emit ``run.cancelled`` event
- :class:`WSEventSink` 翻译成 :class:`RunInterruptedFrame`
- :class:`WebSocketFanout` fanout 推给所有 attach 的 ws

不开真 ws 服务，用 ``WebSocketFanout`` + 两个 mock ws 模拟 A/B tab。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core.contracts import Event
from web.ws_event_sink import WSEventSink
from web.ws_fanout import WebSocketFanout


def _make_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send_json = AsyncMock(return_value=None)
    ws.close = AsyncMock(return_value=None)
    return ws


@pytest.mark.asyncio
async def test_run_cancelled_fans_out_to_all_attached_ws() -> None:
    """A tab 发 interrupt → run.cancelled event → fanout 推 RunInterruptedFrame
    给 A 和 B 两个 tab。
    """
    fanout = WebSocketFanout()
    ws_a = _make_ws()
    ws_b = _make_ws()
    fanout.attach_ws(ws_a)
    fanout.attach_ws(ws_b)

    sink = WSEventSink(fanout)

    await sink.emit(
        Event(
            kind="run.cancelled",
            run_id="run-thread-mt-1",
            turn=2,
            payload={
                "cancelled_at_turn": 2,
                "cancelled_tool_call_id": "call-z",
                "cancel_reason": "user_interrupt",
            },
        )
    )

    # 两个 tab 都应收到 run.interrupted frame
    ws_a.send_json.assert_awaited_once()
    ws_b.send_json.assert_awaited_once()

    sent_a = ws_a.send_json.await_args.args[0]
    sent_b = ws_b.send_json.await_args.args[0]
    for sent in (sent_a, sent_b):
        assert sent["kind"] == "run.interrupted"
        assert sent["run_id"] == "run-thread-mt-1"
        assert sent["cancelled_at_turn"] == 2
        assert sent["cancelled_tool_call_id"] == "call-z"
        assert sent["cancel_reason"] == "user_interrupt"


@pytest.mark.asyncio
async def test_run_cancelled_drops_failed_ws_keeps_healthy() -> None:
    """A tab 已断（send 报错）时，fanout 自动剔除它；B tab 仍正常收到。

    防御场景：A tab 是发 interrupt 的源，但发出后用户立刻关 tab；B tab
    仍打开 → 不应因 A 断而漏掉 B 的同步。
    """
    fanout = WebSocketFanout()
    bad = _make_ws()
    healthy = _make_ws()
    bad.send_json.side_effect = RuntimeError("ws closed")
    fanout.attach_ws(bad)
    fanout.attach_ws(healthy)

    sink = WSEventSink(fanout)

    await sink.emit(
        Event(
            kind="run.cancelled",
            run_id="run-x",
            turn=1,
            payload={
                "cancelled_at_turn": 1,
                "cancelled_tool_call_id": None,
                "cancel_reason": "user_interrupt",
            },
        )
    )

    # bad ws 触发 close（fanout 内部清理）
    bad.close.assert_awaited_once()
    # healthy ws 收到 run.interrupted
    healthy.send_json.assert_awaited_once()
    sent = healthy.send_json.await_args.args[0]
    assert sent["kind"] == "run.interrupted"
    # bad 已 detach
    assert fanout.client_count == 1
