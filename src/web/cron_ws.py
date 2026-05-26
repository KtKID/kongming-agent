"""web — cron 全局 WS broker + ``/ws/cron`` 端点 (v0.3 M4)。

设计目的：cron 不绑定具体 thread；触发投递结果应广播给**所有**当前打开
web app 的客户端，前端在 ``/cron`` 页（M7）渲染。

关键设计（plan.md M4 决策 1 + 4 锁定）：

- 独立端点 ``/ws/cron``，**不复用** thread WS（``/ws/threads/{thread_id}``）
- 鉴权：复用 thread WS 的 cookie-based session（``SESSION_COOKIE_NAME`` +
  ``verify_session_cookie``），失败 ``ws.close(1008)``
- 连接管理：单例 :class:`CronWSBroker` 维护 ``set[WebSocket]``；attach
  on accept / detach on disconnect 或 send 失败
- broadcast：``asyncio.gather(..., return_exceptions=True)`` 包裹；单连接
  send 失败 → 自动 detach 该连接，不影响其他订阅者
- 单例方式：模块级 lazy 函数 :func:`get_broker`，让 cli/web 装配方与 WS
  端点 handler 共享同一个 broker 实例（避免传 broker 引用穿过 6 层装配）

边界：

- broker 不持久化任何状态；纯运行时连接池
- 客户端断连后会被 ``finally`` 块从 set 移除
- 不实现订阅过滤（M4 当前广播给所有连接；前端按需 filter）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, WebSocket

from web.auth import SESSION_COOKIE_NAME, verify_session_cookie

logger = logging.getLogger(__name__)


WS_CLOSE_POLICY_VIOLATION = 1008
"""与 thread WS 一致的 close code。"""


class CronWSBroker:
    """v0.3 cron-delivery M4：cron 全局 WS 广播器。

    方法均为 async；内部用 :class:`asyncio.Lock` 保证连接池增删的并发安全。

    使用方式::

        broker = get_broker()           # 模块级单例
        # web/run.py 装配 dispatcher 时
        sink = WebDeliverySink(broker)
        # ws endpoint
        await broker.attach(websocket)
        try:
            ...
        finally:
            await broker.detach(websocket)
        # 业务侧广播
        await broker.broadcast({"frame_type": "cron.run.completed", ...})
    """

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    @property
    def connection_count(self) -> int:
        """当前连接数（仅供测试 / 监控用，不加锁）。"""
        return len(self._connections)

    async def attach(self, ws: WebSocket) -> None:
        """把 WS 连接加入广播池。重复 attach 同一连接幂等（set 去重）。"""
        async with self._lock:
            self._connections.add(ws)

    async def detach(self, ws: WebSocket) -> None:
        """把 WS 连接移出广播池。不存在的连接忽略（discard）。"""
        async with self._lock:
            self._connections.discard(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """异步广播 payload 给所有当前订阅者。

        实现细节：

        - 持锁拷贝当前连接列表（副本迭代，避免 broadcast 期间 attach/detach
          改 set）
        - ``asyncio.gather(..., return_exceptions=True)`` 包裹每个 send：
          单连接抛异常不影响其他订阅者
        - 任何抛异常的连接立即从池里移除（自动清理半死连接）
        """
        async with self._lock:
            connections = list(self._connections)

        if not connections:
            return

        await asyncio.gather(
            *(self._send_one(ws, payload) for ws in connections),
            return_exceptions=True,
        )

    async def _send_one(self, ws: WebSocket, payload: dict[str, Any]) -> None:
        """对单个连接 send；失败则自动 detach。"""
        try:
            await ws.send_json(payload)
        except Exception:
            # 半死连接：客户端已断开但未走到 finally detach 路径
            await self.detach(ws)


# ---------------------------------------------------------------------------
# 模块级单例（让 cli/web 装配方与 WS endpoint 共享同一 broker 实例）
# ---------------------------------------------------------------------------


_broker_singleton: CronWSBroker | None = None


def get_broker() -> CronWSBroker:
    """获取或创建 :class:`CronWSBroker` 单例。

    使用模块级 lazy 单例而非 ``app.state.cron_ws_broker``：
    - run.py 的 ``_make_runtime_factory`` 闭包 / inner factory 拿不到 ``app``
      引用，无法读 ``app.state``
    - 测试隔离需要时调 :func:`reset_broker_for_testing` 清空
    """
    global _broker_singleton
    if _broker_singleton is None:
        _broker_singleton = CronWSBroker()
    return _broker_singleton


def reset_broker_for_testing() -> None:
    """**仅测试用**：重置单例到 None，下一次 :func:`get_broker` 创建新实例。"""
    global _broker_singleton
    _broker_singleton = None


# ---------------------------------------------------------------------------
# WS endpoint
# ---------------------------------------------------------------------------


def register_cron_ws_routes(app: FastAPI) -> None:
    """把 ``/ws/cron`` 端点注册到 FastAPI app。由 :func:`web.app.create_app` 调。

    端点行为：

    1. 校验 ``SESSION_COOKIE_NAME`` cookie（复用 thread WS 鉴权 pattern）；
       失败 ``close(1008)``
    2. ``accept()`` 后 ``broker.attach(ws)``
    3. 进入 receive 循环（仅消费心跳 / 客户端断连信号；不响应业务消息）
    4. 任何路径退出（断连 / 异常）都 ``broker.detach(ws)``
    """

    @app.websocket("/ws/cron")
    async def cron_ws(websocket: WebSocket) -> None:
        await _cron_ws_handler(websocket)


async def _cron_ws_handler(websocket: WebSocket) -> None:
    """``/ws/cron`` 连接生命周期：鉴权 → accept → attach → receive 循环 → detach。"""
    # 1. cookie 鉴权
    serializer = getattr(websocket.app.state, "serializer", None)
    if serializer is None:
        await websocket.close(
            code=WS_CLOSE_POLICY_VIOLATION,
            reason="auth not configured",
        )
        return

    raw_cookie = websocket.cookies.get(SESSION_COOKIE_NAME)
    payload = verify_session_cookie(raw_cookie, serializer)
    if payload is None:
        await websocket.close(
            code=WS_CLOSE_POLICY_VIOLATION,
            reason="not authenticated",
        )
        return

    # 2. accept + attach
    await websocket.accept()
    broker = get_broker()
    await broker.attach(websocket)

    # 3. 入帧循环：客户端不需要给本端发数据，仅 await 直到断连
    try:
        while True:
            # receive_text 会在客户端断开时抛 WebSocketDisconnect
            await websocket.receive_text()
    except Exception:
        # 客户端断连或其它 ws 错误；安静退出
        logger.debug("/ws/cron client disconnected or errored")
    finally:
        # 4. detach
        await broker.detach(websocket)


__all__ = [
    "CronWSBroker",
    "get_broker",
    "register_cron_ws_routes",
    "reset_broker_for_testing",
]
