"""MemoryRefreshSink 单元测试。

覆盖：
- 只对 ``history.compact`` 事件触发 load_from_disk，其他 kind 忽略
- refreshed 事件 payload 字段完整（checksum / memory_chars / user_chars /
  source_paths / trigger）
- load_from_disk 抛异常时 emit() 不抛、也不 forward refreshed
- 一个 downstream sink 抛异常不影响其他 sink 收到事件
- 连续两次 compact 能看到两次 snapshot 刷新（checksum 变化时可区分）
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from core.contracts import Event
from host.memory_refresh_sink import MemoryRefreshSink
from memory.store import MemorySnapshot

# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


@dataclass
class _FakeSnapshot:
    """兼容 MemorySnapshot 接口的轻量测试替身。

    MemoryRefreshSink 只读 ``checksum`` / ``memory_text`` / ``user_text`` /
    ``source_paths``，不关心对象是否是严格的 MemorySnapshot 实例。
    """

    memory_text: str = ""
    user_text: str = ""
    checksum: str = "sha256:test"
    source_paths: tuple[str, ...] = ()


class _StubMemoryStore:
    """记录 load_from_disk 调用次数，可注入下一次返回的 snapshot 或异常。"""

    def __init__(self) -> None:
        self.call_count = 0
        self._queue: list[_FakeSnapshot | Exception] = []

    def queue_snapshot(self, snap: _FakeSnapshot) -> None:
        self._queue.append(snap)

    def queue_error(self, exc: Exception) -> None:
        self._queue.append(exc)

    async def load_from_disk(self) -> _FakeSnapshot:
        self.call_count += 1
        if not self._queue:
            return _FakeSnapshot()
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


class _RaisingSink:
    def __init__(self) -> None:
        self.called = 0

    async def emit(self, event: Event) -> None:
        self.called += 1
        raise RuntimeError("downstream sink broken")


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


class TestMemoryRefreshSink:
    async def test_emit_history_compact_triggers_load_from_disk(self) -> None:
        store = _StubMemoryStore()
        store.queue_snapshot(_FakeSnapshot(memory_text="hello"))
        sink = MemoryRefreshSink(store)  # type: ignore[arg-type]

        await sink.emit(
            Event(kind="history.compact", run_id="r1", turn=3, payload={}),
        )

        assert store.call_count == 1

    async def test_emit_other_kinds_ignored(self) -> None:
        store = _StubMemoryStore()
        sink = MemoryRefreshSink(store)  # type: ignore[arg-type]

        for kind in ("run.start", "turn.end", "llm.request", "memory.write.success"):
            await sink.emit(Event(kind=kind, run_id="r1"))

        assert store.call_count == 0

    async def test_refreshed_event_forwarded_to_downstream(self) -> None:
        store = _StubMemoryStore()
        snap = _FakeSnapshot(
            memory_text="m" * 100,
            user_text="u" * 50,
            checksum="sha256:abc",
            source_paths=("/tmp/MEMORY.md", "/tmp/USER.md"),
        )
        store.queue_snapshot(snap)
        downstream = _RecordingSink()
        sink = MemoryRefreshSink(store, downstream_sinks=[downstream])  # type: ignore[arg-type]

        await sink.emit(
            Event(kind="history.compact", run_id="run-xyz", turn=5, payload={"original_count": 80}),
        )

        assert len(downstream.events) == 1
        ev = downstream.events[0]
        assert ev.kind == "memory.snapshot.refreshed"
        assert ev.run_id == "run-xyz"
        assert ev.turn == 5
        assert ev.payload["checksum"] == "sha256:abc"
        assert ev.payload["memory_chars"] == 100
        assert ev.payload["user_chars"] == 50
        assert ev.payload["source_paths"] == ["/tmp/MEMORY.md", "/tmp/USER.md"]
        assert ev.payload["trigger"] == "history.compact"

    async def test_load_failure_does_not_raise(self) -> None:
        store = _StubMemoryStore()
        store.queue_error(OSError("disk unreachable"))
        downstream = _RecordingSink()
        sink = MemoryRefreshSink(store, downstream_sinks=[downstream])  # type: ignore[arg-type]

        # 不应 raise
        await sink.emit(Event(kind="history.compact", run_id="r1"))

        # 失败后不 forward refreshed 事件
        assert store.call_count == 1
        assert downstream.events == []

    async def test_downstream_sink_exception_does_not_break_others(self) -> None:
        store = _StubMemoryStore()
        store.queue_snapshot(_FakeSnapshot(checksum="sha256:ok"))
        broken = _RaisingSink()
        good = _RecordingSink()
        # broken 在前
        sink = MemoryRefreshSink(
            store,  # type: ignore[arg-type]
            downstream_sinks=[broken, good],
        )

        await sink.emit(Event(kind="history.compact", run_id="r1"))

        assert broken.called == 1
        # 后续 sink 仍然收到了事件
        assert len(good.events) == 1
        assert good.events[0].kind == "memory.snapshot.refreshed"

    async def test_two_compacts_refresh_twice_with_different_checksums(self) -> None:
        """连续两次 compact → 两次 load_from_disk → 两次 refreshed 事件，checksum 可不同。"""
        store = _StubMemoryStore()
        store.queue_snapshot(_FakeSnapshot(checksum="sha256:first"))
        store.queue_snapshot(_FakeSnapshot(checksum="sha256:second"))
        downstream = _RecordingSink()
        sink = MemoryRefreshSink(store, downstream_sinks=[downstream])  # type: ignore[arg-type]

        await sink.emit(Event(kind="history.compact", run_id="r1", turn=1))
        await sink.emit(Event(kind="history.compact", run_id="r1", turn=2))

        assert store.call_count == 2
        assert len(downstream.events) == 2
        assert downstream.events[0].payload["checksum"] == "sha256:first"
        assert downstream.events[1].payload["checksum"] == "sha256:second"

    async def test_downstream_sinks_default_empty(self) -> None:
        """默认没有 downstream 时，仍然正常刷新，只是不 forward。"""
        store = _StubMemoryStore()
        store.queue_snapshot(_FakeSnapshot())
        sink = MemoryRefreshSink(store)  # type: ignore[arg-type]

        await sink.emit(Event(kind="history.compact", run_id="r1"))

        assert store.call_count == 1


# 保持 Path / MemorySnapshot / pytest 不被标成未使用
_ = Path
_ = MemorySnapshot
_ = pytest
