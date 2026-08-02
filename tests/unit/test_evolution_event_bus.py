"""EvolutionEventBus 单测：路由 / 静默丢弃 / sink 异常隔离 / 并发 / unregister 幂等。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from core.contracts import Event
from evolution.event_bus import EvolutionEventBus


@dataclass
class _RecordingSink:
    events: list[Event] = field(default_factory=list)

    async def emit(self, event: Event) -> None:
        self.events.append(event)


@dataclass
class _ExplodingSink:
    async def emit(self, event: Event) -> None:
        raise RuntimeError("boom")


def _make_event(thread_id: str, count: int = 1) -> Event:
    return Event(kind="test", run_id=f"run-claude-{thread_id}-{count}")


class TestEventBus:
    @pytest.mark.asyncio
    async def test_route_hit(self) -> None:
        bus = EvolutionEventBus()
        sink = _RecordingSink()
        bus.register("thread-aabbccddeeff", sink)
        event = _make_event("thread-aabbccddeeff")
        await bus.emit(event)
        assert len(sink.events) == 1
        assert sink.events[0] is event

    @pytest.mark.asyncio
    async def test_route_miss_silent(self) -> None:
        bus = EvolutionEventBus()
        event = _make_event("thread-aabbccddeeff")
        await bus.emit(event)  # no route → no crash

    @pytest.mark.asyncio
    async def test_sink_exception_swallowed(self) -> None:
        bus = EvolutionEventBus()
        bus.register("thread-aabbccddeeff", _ExplodingSink())
        event = _make_event("thread-aabbccddeeff")
        await bus.emit(event)  # sink raises → swallowed

    @pytest.mark.asyncio
    async def test_concurrent_register_emit(self) -> None:
        bus = EvolutionEventBus()
        sinks = [_RecordingSink() for _ in range(5)]
        for i, sink in enumerate(sinks):
            bus.register(f"thread-{i:012x}", sink)

        events = [_make_event(f"thread-{i:012x}", count=i) for i in range(5)]
        await asyncio.gather(*(bus.emit(e) for e in events))

        for i, sink in enumerate(sinks):
            assert len(sink.events) == 1

    @pytest.mark.asyncio
    async def test_unregister_idempotent(self) -> None:
        bus = EvolutionEventBus()
        bus.register("thread-aabbccddeeff", _RecordingSink())
        bus.unregister("thread-aabbccddeeff")
        bus.unregister("thread-aabbccddeeff")  # idempotent, no crash
        bus.unregister("thread-000000000000")  # never registered

    @pytest.mark.asyncio
    async def test_same_sink_reference_count_keeps_second_connection_route(self) -> None:
        """同一 thread fanout sink 注册两次时，单次断连只释放一份引用。"""
        bus = EvolutionEventBus()
        sink = _RecordingSink()
        thread_id = "thread-aabbccddeeff"
        bus.register(thread_id, sink)
        bus.register(thread_id, sink)

        bus.unregister(thread_id, sink)
        event = _make_event(thread_id)
        await bus.emit(event)
        assert sink.events == [event]

        bus.unregister(thread_id, sink)
        await bus.emit(_make_event(thread_id, count=2))
        assert sink.events == [event]

    @pytest.mark.asyncio
    async def test_stale_sink_unregister_does_not_remove_replacement(self) -> None:
        """旧连接断开时按身份注销，不影响后来替换的新 sink。"""
        bus = EvolutionEventBus()
        thread_id = "thread-aabbccddeeff"
        old_sink = _RecordingSink()
        new_sink = _RecordingSink()
        bus.register(thread_id, old_sink)
        bus.register(thread_id, new_sink)

        bus.unregister(thread_id, old_sink)
        event = _make_event(thread_id)
        await bus.emit(event)

        assert old_sink.events == []
        assert new_sink.events == [event]
