"""EvolutionEventBus — 按 thread_id 路由 evolution 事件到频道 sink。

实现 ``EventSink`` Protocol，可直接传给 SessionEngine / reviewer_runtime
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

# 从 run_id 里提取 thread-{hex12} 子串。不限前缀——reviewer 事件的 run_id
# 格式是 evo-review-{session_id}-run-{channel}-{thread_id}-{count}，
# 主链事件是 run-{channel}-{thread_id}-{count}，两种都能匹配。
_THREAD_ID_RE = re.compile(r"(thread-[a-f0-9]{12})")


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
        self._route_ref_counts: dict[str, int] = {}

    def register(self, thread_id: str, sink: EventSink) -> None:
        """登记 thread_id → sink 路由；同一 sink 重复登记使用引用计数。"""
        old = self._routes.get(thread_id)
        if old is sink:
            self._route_ref_counts[thread_id] = self._route_ref_counts.get(thread_id, 1) + 1
            return
        if old is not None:
            logger.info("event_bus: replacing existing route for %s", thread_id)
        self._routes[thread_id] = sink
        self._route_ref_counts[thread_id] = 1

    def unregister(self, thread_id: str, sink: EventSink | None = None) -> None:
        """撤销路由；可按 sink 身份安全释放单个连接登记。"""
        current = self._routes.get(thread_id)
        if current is None:
            return
        if sink is not None and current is not sink:
            return
        if sink is not None:
            remaining = self._route_ref_counts.get(thread_id, 1) - 1
            if remaining > 0:
                self._route_ref_counts[thread_id] = remaining
                return
        self._routes.pop(thread_id, None)
        self._route_ref_counts.pop(thread_id, None)

    async def emit(self, event: Event) -> None:
        """收到 reviewer/tool 事件 → 按 run_id 正则解析 thread_id → 路由到 sink。

        路由不存在 → 静默丢弃 + log debug。
        Sink 抛异常 → 吞 + log warning（不影响其它事件）。
        """
        run_id = event.run_id or ""
        match = _THREAD_ID_RE.search(run_id)
        if not match:
            logger.debug("event_bus: cannot parse thread_id from run_id=%r, drop", run_id)
            return
        thread_id = match.group(1)
        sink = self._routes.get(thread_id)
        if sink is None:
            logger.debug("event_bus: no route for thread_id=%s, drop", thread_id)
            return
        try:
            logger.info(
                "event_bus: routing event kind=%s run_id=%s → thread_id=%s sink=%s",
                event.kind,
                run_id,
                thread_id,
                type(sink).__name__,
            )
            await sink.emit(event)
            logger.info("event_bus: sink.emit OK for thread_id=%s kind=%s", thread_id, event.kind)
        except Exception as exc:
            logger.warning("event_bus: sink for %s raised: %r", thread_id, exc)
