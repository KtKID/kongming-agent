"""EvolutionEventBus — 按 thread_id 路由 evolution 事件到频道 sink。

实现 ``EventSink`` Protocol，可直接传给 NativeRuntime / reviewer_runtime
作为 event_sinks 列表元素。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.contracts import Event, EventSink

__all__ = ["EvolutionEventBus"]

logger = logging.getLogger(__name__)

# v0.1 hardcode claude 前缀；M1.5 codex 接入时扩成多正则或 channel_id 参数化
_RUN_ID_RE = re.compile(r"^run-(?:claude|codex|native)-(thread-[a-f0-9]{12})-\d+$")


class EvolutionEventBus:
    """按 ``thread_id`` 路由事件的 EventSink 实现。

    频道方在 ws 连接时 ``register(thread_id, sink)``，断开时 ``unregister``。
    reviewer 跑出的事件通过 ``emit`` 路由到对应 sink。

    **并发安全说明**：register / unregister 是同步方法，在 asyncio 单线程
    event loop 里原子执行（dict 操作不跨 await 边界）。emit 是 async，在路由
    后对 sink 调 ``await sink.emit(event)``，此时 dict 不被并发修改。
    """

    def __init__(self) -> None:
        self._routes: dict[str, EventSink] = {}

    def register(self, thread_id: str, sink: EventSink) -> None:
        """登记 thread_id → sink 路由。重复登记覆盖旧的。"""
        old = self._routes.get(thread_id)
        if old is not None:
            logger.info("event_bus: replacing existing route for %s", thread_id)
        self._routes[thread_id] = sink

    def unregister(self, thread_id: str) -> None:
        """撤销路由。幂等。"""
        self._routes.pop(thread_id, None)

    async def emit(self, event: Event) -> None:
        """收到 reviewer/tool 事件 → 按 run_id 正则解析 thread_id → 路由到 sink。

        路由不存在 → 静默丢弃 + log debug。
        Sink 抛异常 → 吞 + log warning（不影响其它事件）。
        """
        run_id = event.run_id or ""
        match = _RUN_ID_RE.match(run_id)
        if not match:
            logger.debug("event_bus: cannot parse thread_id from run_id=%r, drop", run_id)
            return
        thread_id = match.group(1)
        sink = self._routes.get(thread_id)
        if sink is None:
            logger.debug("event_bus: no route for thread_id=%s, drop", thread_id)
            return
        try:
            await sink.emit(event)
        except Exception as exc:
            logger.warning("event_bus: sink for %s raised: %r", thread_id, exc)
