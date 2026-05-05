from __future__ import annotations

from unittest.mock import AsyncMock

from web.ws_fanout import WebSocketFanout


def _make_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send_json = AsyncMock(return_value=None)
    ws.close = AsyncMock(return_value=None)
    return ws


async def test_ws_fanout_broadcasts_to_all_clients() -> None:
    fanout = WebSocketFanout()
    ws1 = _make_ws()
    ws2 = _make_ws()
    fanout.attach_ws(ws1)
    fanout.attach_ws(ws2)

    await fanout.send_json({"kind": "system.notice"})

    ws1.send_json.assert_awaited_once_with({"kind": "system.notice"})
    ws2.send_json.assert_awaited_once_with({"kind": "system.notice"})


async def test_ws_fanout_drops_failed_client_and_keeps_healthy_client() -> None:
    fanout = WebSocketFanout()
    failing = _make_ws()
    healthy = _make_ws()
    failing.send_json.side_effect = RuntimeError("boom")
    fanout.attach_ws(failing)
    fanout.attach_ws(healthy)

    await fanout.send_json({"kind": "tool.call.start"})

    failing.close.assert_awaited_once()
    healthy.send_json.assert_awaited_once_with({"kind": "tool.call.start"})
    assert fanout.client_count == 1


async def test_ws_fanout_detach_removes_only_target_client() -> None:
    fanout = WebSocketFanout()
    ws1 = _make_ws()
    ws2 = _make_ws()
    fanout.attach_ws(ws1)
    fanout.attach_ws(ws2)

    fanout.detach_ws(ws1)
    await fanout.send_json({"kind": "usage"})

    ws1.send_json.assert_not_called()
    ws2.send_json.assert_awaited_once_with({"kind": "usage"})
    assert fanout.client_count == 1
