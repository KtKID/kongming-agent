"""EventBus 真实 run_id 格式覆盖——拦住"正则匹配不上"类 bug。

之前只测了理想格式 run-claude-{tid}-{n}，没覆盖：
- reviewer 事件的 evo-review-{sid}-run-{channel}-{tid}-{n}
- 不含 thread_id 的 run_id
- 空 run_id
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from core.contracts import Event
from evolution.event_bus import EvolutionEventBus


@dataclass
class _Recorder:
    events: list[Event] = field(default_factory=list)

    async def emit(self, event: Event) -> None:
        self.events.append(event)


TID = "thread-aabbccddeeff"


class TestRealRunIdFormats:
    """覆盖所有实际出现的 run_id 格式。"""

    @pytest.mark.asyncio
    async def test_main_chain_runid(self) -> None:
        """主链事件: run-claude-{tid}-{count}"""
        bus = EvolutionEventBus()
        sink = _Recorder()
        bus.register(TID, sink)
        await bus.emit(Event(kind="test", run_id=f"run-claude-{TID}-5"))
        assert len(sink.events) == 1

    @pytest.mark.asyncio
    async def test_reviewer_session_runid(self) -> None:
        """reviewer 事件: evo-review-{tid}-run-claude-{tid}-{count}"""
        bus = EvolutionEventBus()
        sink = _Recorder()
        bus.register(TID, sink)
        await bus.emit(
            Event(kind="evolution.review.completed", run_id=f"evo-review-{TID}-run-claude-{TID}-5")
        )
        assert len(sink.events) == 1

    @pytest.mark.asyncio
    async def test_codex_runid(self) -> None:
        """codex 频道: run-codex-{tid}-{count}"""
        bus = EvolutionEventBus()
        sink = _Recorder()
        bus.register(TID, sink)
        await bus.emit(Event(kind="test", run_id=f"run-codex-{TID}-3"))
        assert len(sink.events) == 1

    @pytest.mark.asyncio
    async def test_native_runid(self) -> None:
        """native 频道: run-native-{tid}-{count}"""
        bus = EvolutionEventBus()
        sink = _Recorder()
        bus.register(TID, sink)
        await bus.emit(Event(kind="test", run_id=f"run-native-{TID}-7"))
        assert len(sink.events) == 1

    @pytest.mark.asyncio
    async def test_no_thread_id_in_runid(self) -> None:
        """不含 thread-{hex12} 的 run_id → 静默丢弃。"""
        bus = EvolutionEventBus()
        sink = _Recorder()
        bus.register(TID, sink)
        await bus.emit(Event(kind="test", run_id="run-smoke-10"))
        assert len(sink.events) == 0

    @pytest.mark.asyncio
    async def test_empty_runid(self) -> None:
        """空 run_id → 静默丢弃。"""
        bus = EvolutionEventBus()
        sink = _Recorder()
        bus.register(TID, sink)
        await bus.emit(Event(kind="test", run_id=""))
        assert len(sink.events) == 0
