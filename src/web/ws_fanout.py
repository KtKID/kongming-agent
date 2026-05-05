"""同一 thread 的多 WebSocket 广播器。

把一个 thread cell 的浏览器连接聚合成一个轻量 fanout，对上游暴露
``send_json / close / attach_ws / detach_ws`` 四个方法。

设计目标：

- 同一 thread 同时存在多个聊天页 / StrictMode 临时重连时，事件广播到全部活连接
- 单个连接发送失败时，只移除该连接，保留其它连接
- 没有活连接时静默丢弃发送，cell 生命周期继续由 ThreadManager 管
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any


class WebSocketFanout:
    """同一 thread 的多连接 fanout。"""

    def __init__(self) -> None:
        self._clients: list[Any] = []

    def attach_ws(self, ws: Any) -> None:
        if any(client is ws for client in self._clients):
            return
        self._clients.append(ws)

    def detach_ws(self, ws: Any) -> None:
        self._clients = [client for client in self._clients if client is not ws]

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def send_json(self, payload: Any) -> None:
        if not self._clients:
            return
        dead_clients: list[Any] = []
        for client in list(self._clients):
            try:
                await client.send_json(payload)
            except Exception:
                dead_clients.append(client)
                with contextlib.suppress(Exception):
                    close_call = client.close()
                    if asyncio.iscoroutine(close_call):
                        await close_call
        if dead_clients:
            dead_ids = {id(client) for client in dead_clients}
            self._clients = [client for client in self._clients if id(client) not in dead_ids]

    async def close(self) -> None:
        for client in list(self._clients):
            with contextlib.suppress(Exception):
                close_call = client.close()
                if asyncio.iscoroutine(close_call):
                    await close_call
        self._clients.clear()


__all__ = ["WebSocketFanout"]
