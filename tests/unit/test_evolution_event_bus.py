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
