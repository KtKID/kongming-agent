"""ThreadStatusBroadcaster 单测：emit frame_type→phase 映射 + 连接管理。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core.contracts import Event
from hosts.web.websocket.thread_status import (
    ThreadStatusBroadcaster,
    ThreadStatusEventSink,
    get_broadcaster,
    reset_broadcaster_for_testing,
)

THREAD_ID = "thread-abcdef123456"


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    reset_broadcaster_for_testing()


def _make_ws(*, fail: bool = False) -> AsyncMock:
    ws = AsyncMock()
    if fail:
        ws.send_json.side_effect = Exception("connection lost")
    return ws


# ---------------------------------------------------------------------------
# emit: frame_type → phase 映射
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_stream_status_responding() -> None:
    b = ThreadStatusBroadcaster()
    ws = _make_ws()
    await b.attach(ws)

    await b.emit(THREAD_ID, {"frame_type": "stream_status", "phase": "responding"})

    ws.send_json.assert_called_once()
    frame = ws.send_json.call_args[0][0]
    assert frame["frame_type"] == "thread-status"
    assert frame["threadId"] == THREAD_ID
    assert frame["phase"] == "responding"
    assert "toolName" not in frame


@pytest.mark.asyncio
async def test_emit_stream_status_thinking() -> None:
    b = ThreadStatusBroadcaster()
    ws = _make_ws()
    await b.attach(ws)

    await b.emit(THREAD_ID, {"frame_type": "stream_status", "phase": "thinking"})

    frame = ws.send_json.call_args[0][0]
    assert frame["phase"] == "thinking"


@pytest.mark.asyncio
async def test_emit_stream_status_tool_calling_with_tool_name() -> None:
    b = ThreadStatusBroadcaster()
    ws = _make_ws()
    await b.attach(ws)

    await b.emit(
        THREAD_ID,
        {
            "frame_type": "stream_status",
            "phase": "tool_calling",
            "toolName": "Read",
        },
    )

    frame = ws.send_json.call_args[0][0]
    assert frame["phase"] == "tool_calling"
    assert frame["toolName"] == "Read"


@pytest.mark.asyncio
async def test_emit_permission_request_maps_to_waiting_approval() -> None:
    b = ThreadStatusBroadcaster()
    ws = _make_ws()
    await b.attach(ws)

    await b.emit(THREAD_ID, {"frame_type": "permission_request", "requestId": "r1"})

    frame = ws.send_json.call_args[0][0]
    assert frame["phase"] == "waiting_approval"


@pytest.mark.asyncio
async def test_emit_permission_cancelled_maps_to_idle() -> None:
    b = ThreadStatusBroadcaster()
    ws = _make_ws()
    await b.attach(ws)

    await b.emit(THREAD_ID, {"frame_type": "permission_cancelled"})

    frame = ws.send_json.call_args[0][0]
    assert frame["phase"] == "idle"


@pytest.mark.asyncio
async def test_emit_complete_maps_to_complete() -> None:
    b = ThreadStatusBroadcaster()
    ws = _make_ws()
    await b.attach(ws)

    await b.emit(THREAD_ID, {"frame_type": "complete", "exitCode": 0})

    frame = ws.send_json.call_args[0][0]
    assert frame["phase"] == "complete"


@pytest.mark.asyncio
async def test_emit_error_maps_to_error() -> None:
    b = ThreadStatusBroadcaster()
    ws = _make_ws()
    await b.attach(ws)

    await b.emit(THREAD_ID, {"frame_type": "error", "error": "boom"})

    frame = ws.send_json.call_args[0][0]
    assert frame["phase"] == "error"


@pytest.mark.asyncio
async def test_emit_irrelevant_frame_type_does_not_broadcast() -> None:
    """text / tool_use / stream_delta 等非状态帧不触发广播。"""
    b = ThreadStatusBroadcaster()
    ws = _make_ws()
    await b.attach(ws)

    for frame_type in (
        "text",
        "tool_use",
        "tool_result",
        "stream_delta",
        "stream_end",
        "session_created",
    ):
        await b.emit(THREAD_ID, {"frame_type": frame_type})

    ws.send_json.assert_not_called()


@pytest.mark.asyncio
async def test_emit_unknown_stream_status_phase_ignored() -> None:
    """stream_status 带未知 phase → 不推送（保守降级）。"""
    b = ThreadStatusBroadcaster()
    ws = _make_ws()
    await b.attach(ws)

    await b.emit(THREAD_ID, {"frame_type": "stream_status", "phase": "unknown_phase"})

    ws.send_json.assert_not_called()


@pytest.mark.asyncio
async def test_emit_missing_frame_type_does_nothing() -> None:
    b = ThreadStatusBroadcaster()
    ws = _make_ws()
    await b.attach(ws)

    await b.emit(THREAD_ID, {})

    ws.send_json.assert_not_called()


# ---------------------------------------------------------------------------
# EventSink: Runner event → phase 映射
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_sink_run_cancelled_maps_to_idle_after_running_phase() -> None:
    b = get_broadcaster()
    ws = _make_ws()
    await b.attach(ws)
    sink = ThreadStatusEventSink(THREAD_ID)

    await sink.emit(
        Event(
            kind="tool.call.start",
            run_id="run-1",
            turn=3,
            payload={"tool_name": "run_shell"},
        )
    )
    await sink.emit(
        Event(
            kind="run.cancelled",
            run_id="run-1",
            turn=3,
            payload={
                "cancelled_at_turn": 3,
                "cancelled_tool_call_id": "call-1",
                "cancel_reason": "user_interrupt",
            },
        )
    )

    phases = [call.args[0]["phase"] for call in ws.send_json.call_args_list]
    assert phases == ["tool_calling", "idle"]
    assert ws.send_json.call_args_list[0].args[0]["toolName"] == "run_shell"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "phase"),
    [
        ("completed", "complete"),
        ("cancelled", "idle"),
        ("failed", "error"),
    ],
)
async def test_event_sink_run_end_status_maps_to_terminal_phase(
    status: str,
    phase: str,
) -> None:
    b = get_broadcaster()
    ws = _make_ws()
    await b.attach(ws)
    sink = ThreadStatusEventSink(THREAD_ID)

    await sink.emit(
        Event(
            kind="run.end",
            run_id="run-1",
            turn=3,
            payload={"status": status, "turn_count": 3},
        )
    )

    ws.send_json.assert_called_once()
    frame = ws.send_json.call_args[0][0]
    assert frame["frame_type"] == "thread-status"
    assert frame["threadId"] == THREAD_ID
    assert frame["phase"] == phase


# ---------------------------------------------------------------------------
# 连接管理
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_detach_connection_count() -> None:
    b = ThreadStatusBroadcaster()
    ws1 = _make_ws()
    ws2 = _make_ws()

    assert b.connection_count == 0
    await b.attach(ws1)
    assert b.connection_count == 1
    await b.attach(ws2)
    assert b.connection_count == 2
    await b.detach(ws1)
    assert b.connection_count == 1
    await b.detach(ws1)  # idempotent
    assert b.connection_count == 1


@pytest.mark.asyncio
async def test_broadcast_fan_out_to_multiple_clients() -> None:
    b = ThreadStatusBroadcaster()
    ws1 = _make_ws()
    ws2 = _make_ws()
    await b.attach(ws1)
    await b.attach(ws2)

    await b.emit(THREAD_ID, {"frame_type": "complete", "exitCode": 0})

    assert ws1.send_json.call_count == 1
    assert ws2.send_json.call_count == 1
    assert ws1.send_json.call_args[0][0]["phase"] == "complete"
    assert ws2.send_json.call_args[0][0]["phase"] == "complete"


@pytest.mark.asyncio
async def test_failed_connection_auto_detached() -> None:
    b = ThreadStatusBroadcaster()
    ws_ok = _make_ws()
    ws_fail = _make_ws(fail=True)
    await b.attach(ws_ok)
    await b.attach(ws_fail)
    assert b.connection_count == 2

    await b.emit(THREAD_ID, {"frame_type": "complete", "exitCode": 0})

    assert b.connection_count == 1
    assert ws_ok.send_json.call_count == 1


@pytest.mark.asyncio
async def test_no_connections_emit_is_noop() -> None:
    """0 连接时 emit 不抛异常。"""
    b = ThreadStatusBroadcaster()
    await b.emit(THREAD_ID, {"frame_type": "complete", "exitCode": 0})
