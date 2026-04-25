"""unit：observability.trace_sink 覆盖。

B1 / CR 报告 cr-report-20260424-202744.md。覆盖：

- 懒初始化：构造不创建目录，首次 emit 时创建父目录 + 空文件
- emit 一行合法 JSON，含完整字段
- 多次 emit 顺序追加
- 并发 emit（asyncio.gather）不交错半行
- auto_flush=False 不强制 flush（行为可观测：能正常写入）
- dataclass Event 走 asdict；非 dataclass 鸭子对象走 __dict__ / getattr 兜底
- Path / datetime / set / bytes / 未知对象的 _json_default 降级
- build_jsonl_trace_sink 从 Config 只读 output_path
- close() 幂等 no-op
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pytest

from observability.trace_sink import (
    JsonlTraceSink,
    _event_to_dict,
    _json_default,
    build_jsonl_trace_sink,
)


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_constructor_does_no_io(tmp_path):
    """构造不触发任何 I/O；父目录保持未创建。"""
    nested = tmp_path / "a" / "b" / "trace.jsonl"
    sink = JsonlTraceSink(nested)
    assert not nested.parent.exists()
    assert not nested.exists()
    assert sink.output_path == nested


@pytest.mark.asyncio
async def test_first_emit_creates_parent_dir_and_file(tmp_path):
    from core.contracts import Event

    nested = tmp_path / "deep" / "trace.jsonl"
    sink = JsonlTraceSink(nested)
    await sink.emit(Event(kind="run.start", run_id="r1"))

    assert nested.exists()
    lines = _read_jsonl(nested)
    assert len(lines) == 1
    assert lines[0]["kind"] == "run.start"
    assert lines[0]["run_id"] == "r1"


@pytest.mark.asyncio
async def test_multiple_emits_append_in_order(tmp_path):
    from core.contracts import Event

    path = tmp_path / "trace.jsonl"
    sink = JsonlTraceSink(path)
    for i in range(5):
        await sink.emit(Event(kind="turn.start", run_id="r", turn=i))
    lines = _read_jsonl(path)
    assert [line["turn"] for line in lines] == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_concurrent_emits_do_not_interleave(tmp_path):
    """并发 emit 必须被 asyncio.Lock 串行，不得出现半行 JSON。"""
    from core.contracts import Event

    path = tmp_path / "trace.jsonl"
    sink = JsonlTraceSink(path)
    await asyncio.gather(
        *[sink.emit(Event(kind="llm.request", run_id="r", turn=i)) for i in range(50)]
    )
    raw = path.read_text().splitlines()
    # 每行必须独立可解析
    parsed = [json.loads(line) for line in raw if line.strip()]
    assert len(parsed) == 50
    assert sorted(line["turn"] for line in parsed) == list(range(50))


@pytest.mark.asyncio
async def test_emit_dataclass_event_uses_asdict(tmp_path):
    from core.contracts import Event

    path = tmp_path / "trace.jsonl"
    sink = JsonlTraceSink(path)
    await sink.emit(Event(kind="llm.response", run_id="r", turn=1, payload={"k": "v"}))
    line = _read_jsonl(path)[0]
    # dataclass 展开：asdict 产出 kind / run_id / turn / payload / timestamp_ms
    assert line["payload"] == {"k": "v"}
    assert "timestamp_ms" in line


@pytest.mark.asyncio
async def test_emit_ducktype_object_via_dict(tmp_path):
    """非 dataclass 鸭子对象走 __dict__ 分支。"""

    class FakeEvent:
        def __init__(self):
            self.kind = "custom"
            self.run_id = "r"
            self.turn = 3
            self.payload = {"hello": "世界"}
            self._private = "should-not-appear"

    path = tmp_path / "trace.jsonl"
    sink = JsonlTraceSink(path)
    await sink.emit(FakeEvent())
    line = _read_jsonl(path)[0]
    assert line["kind"] == "custom"
    assert line["payload"]["hello"] == "世界"
    # 下划线开头字段必须被过滤
    assert "_private" not in line


@pytest.mark.asyncio
async def test_emit_slotted_object_via_getattr(tmp_path):
    """没有 __dict__ 且非 dataclass → 走 getattr 兜底读已知字段。"""

    class SlottedEvent:
        __slots__ = ("kind", "payload", "run_id", "timestamp_ms", "turn")

        def __init__(self):
            self.kind = "slotted"
            self.run_id = "r"
            self.turn = 0
            self.payload = {}
            self.timestamp_ms = 123

    d = _event_to_dict(SlottedEvent())
    assert d["kind"] == "slotted"
    assert d["timestamp_ms"] == 123


def test_json_default_dataclass():
    @dataclass
    class X:
        a: int
        b: str

    assert _json_default(X(1, "x")) == {"a": 1, "b": "x"}


def test_json_default_path():
    assert _json_default(Path("/tmp/a")) == "/tmp/a"


def test_json_default_datetime_and_date():
    dt = datetime(2026, 1, 2, 3, 4, 5)
    assert _json_default(dt) == dt.isoformat()
    d = date(2026, 1, 2)
    assert _json_default(d) == d.isoformat()


def test_json_default_set_deterministic_order():
    result = _json_default({"b", "a", "c"})
    assert result == ["a", "b", "c"]


def test_json_default_bytes_utf8():
    assert _json_default(b"hello") == "hello"


def test_json_default_bytes_non_utf8_falls_back_to_hex():
    raw = bytes([0xFF, 0xFE, 0xFD])
    assert _json_default(raw) == raw.hex()


def test_json_default_unknown_object_falls_back_to_repr():
    class Odd:
        def __repr__(self):
            return "<Odd>"

    assert _json_default(Odd()) == "<Odd>"


@pytest.mark.asyncio
async def test_emit_payload_with_path_and_datetime(tmp_path):
    """payload 里包含不可 JSON 原生序列化的值时，走 _json_default 降级。"""
    from core.contracts import Event

    path = tmp_path / "trace.jsonl"
    sink = JsonlTraceSink(path)
    event = Event(
        kind="tool.call.end",
        run_id="r",
        turn=1,
        payload={
            "file": Path("/etc/hosts"),
            "at": datetime(2026, 4, 24, 12, 0, 0),
            "tags": {"a", "b"},
            "raw": b"ok",
        },
    )
    await sink.emit(event)
    line = _read_jsonl(path)[0]
    assert line["payload"]["file"] == "/etc/hosts"
    assert line["payload"]["at"].startswith("2026-04-24T12:00:00")
    assert line["payload"]["tags"] == ["a", "b"]
    assert line["payload"]["raw"] == "ok"


@pytest.mark.asyncio
async def test_auto_flush_false_still_writes(tmp_path):
    from core.contracts import Event

    path = tmp_path / "trace.jsonl"
    sink = JsonlTraceSink(path, auto_flush=False)
    await sink.emit(Event(kind="run.end", run_id="r"))
    # 即使不 flush，退出 with 上下文也会 close → 保证写盘
    assert len(_read_jsonl(path)) == 1


@pytest.mark.asyncio
async def test_close_is_noop_and_idempotent(tmp_path):
    path = tmp_path / "trace.jsonl"
    sink = JsonlTraceSink(path)
    await sink.close()
    await sink.close()  # 幂等


@pytest.mark.asyncio
async def test_build_jsonl_trace_sink_reads_only_output_path(tmp_path):
    from config_loader.models import Config, ModelConfig, TraceConfig

    cfg = Config(
        model=ModelConfig(name="m", base_url="http://localhost:1234"),
        trace=TraceConfig(output_path=str(tmp_path / "out.jsonl")),
    )
    sink = build_jsonl_trace_sink(cfg)
    assert sink.output_path == Path(str(tmp_path / "out.jsonl"))
