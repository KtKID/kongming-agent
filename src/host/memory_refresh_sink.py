"""Memory snapshot 刷新 sink：compact 后重新 load_from_disk。

独立 EventSink 实现，订阅 ``kind=history.compact`` 事件，触发 memory snapshot
重新加载，并 emit ``memory.snapshot.refreshed`` 事件到 downstream sinks。

符合 v1-mini 架构约束：

- core / runner 完全不感知 memory 存在
- 事件只走 :class:`core.contracts.EventSink` Protocol，不新增 Protocol
- memory 层不 import host / runner / cli；host 层消费 memory 是允许的
  （layers 合约 ``cli > host > ... > memory``）
"""

from __future__ import annotations

from collections.abc import Sequence

from core.contracts import Event, EventSink
from memory import MemoryStore


class MemoryRefreshSink:
    """订阅 ``history.compact`` 事件，触发 memory snapshot 刷新。

    装配方式（v1-mini 方案 γ）：CLI 在 ``build_session`` 之后把
    :class:`MemoryRefreshSink` 加进 runner 的 ``list[EventSink]``，使其和
    :class:`observability.JsonlTraceSink` 并列接收事件。收到
    ``history.compact`` 时调 :meth:`memory.MemoryStore.load_from_disk`
    刷新冻结快照，并把 ``memory.snapshot.refreshed`` 事件 fan-out 到
    downstream sinks（通常是 trace sink），保证下一轮 prompt 组装
    能读到最新的 memory 内容。

    Attributes:
        memory_store: 被刷新的 :class:`MemoryStore` 实例。CLI 装配时持有同一个
            实例；MemoryTool 读写后，此处 ``load_from_disk`` 会重新 merge 磁盘
            最新状态到 snapshot。
        downstream_sinks: fan-out 目标（通常是 :class:`JsonlTraceSink`）。
            runner 自己会写 ``history.compact`` 到 trace，但
            ``memory.snapshot.refreshed`` 需要本 sink 手动转发。
    """

    def __init__(
        self,
        memory_store: MemoryStore,
        downstream_sinks: Sequence[EventSink] = (),
    ) -> None:
        self._store = memory_store
        self._downstream: list[EventSink] = list(downstream_sinks)

    async def emit(self, event: Event) -> None:
        """EventSink Protocol 实现。

        - 只关心 ``history.compact``，其它 kind 直接忽略（不报错、不 no-op trace）。
        - load_from_disk 失败不抛异常（避免污染 runner 的 fan-out 主链路），
          只是不触发后续的 refreshed 事件。
        - downstream sink 的异常也被单独捕获，一个 sink 抛错不影响其他 sink。
        """
        if event.kind != "history.compact":
            return

        try:
            new_snapshot = await self._store.load_from_disk()
        except Exception:
            # 刷新失败不污染主链路；不 forward refreshed 事件
            return

        refreshed_event = Event(
            kind="memory.snapshot.refreshed",
            run_id=event.run_id,
            turn=event.turn,
            payload={
                "checksum": new_snapshot.checksum,
                "memory_chars": len(new_snapshot.memory_text),
                "user_chars": len(new_snapshot.user_text),
                "source_paths": list(new_snapshot.source_paths),
                "trigger": "history.compact",
            },
        )
        for sink in self._downstream:
            try:
                await sink.emit(refreshed_event)
            except Exception:
                continue


__all__ = ["MemoryRefreshSink"]
