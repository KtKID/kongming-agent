"""可重绑的 WebSocket writer。

给长生命周期后台任务一个稳定 writer 引用；浏览器重连时只替换底层 ws，
后台流和审批流继续复用同一个 writer 对象。
"""

from __future__ import annotations

from typing import Any

from network.network_log import log_network_exception


class ReconnectableWebSocketWriter:
    """duck-typed writer：支持 ``send_json`` + ``attach_ws`` + ``detach_ws``。"""

    def __init__(self, websocket: Any) -> None:
        self._ws: Any | None = websocket

    def attach_ws(self, websocket: Any) -> None:
        self._ws = websocket

    def detach_ws(self, websocket: Any) -> None:
        if self._ws is websocket:
            self._ws = None

    async def send_json(self, msg: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("websocket writer detached")
        await self._ws.send_json(msg)

    async def close(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is None:
            return
        try:
            close_call = ws.close()
            if hasattr(close_call, "__await__"):
                await close_call
        except Exception as exc:
            log_network_exception(
                "web._shared.reconnectable_writer",
                "close_ws_failed",
                exc,
            )


__all__ = ["ReconnectableWebSocketWriter"]
