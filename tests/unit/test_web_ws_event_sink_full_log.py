"""WSEventSink × FullLogger 接入集成测试（full-log-v0.1 阶段 1）。

验证 :class:`web.websocket.event_sink.WSEventSink` 在 emit 阶段：

- send 成功后对 ``turn.start`` / ``turn.end`` 调 ``full_logger.log()``，
  日志记录里包含 ``thread_id`` 字段
- 其他 kind（``content.delta`` / ``tool.call.*`` / ``approval.decision`` / ``usage`` /
  ``error`` 等）在阶段 1 **不写** full_log（阶段 2 #11 才放开）
- ``thread_id=None`` 构造时日志记录里 ``thread_id`` 字段为 ``null``（向后兼容）
- ``full_logger.enabled=False`` 时 emit 完全不触发写盘（零开销）
- send 失败时不调 logger（先 send 再 log 的次序约束）
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.contracts import Event
from devtools.full_logger import FullLogger, _reset_for_tests, init_full_logger
from hosts.web.websocket.event_sink import WSEventSink


class _StubConfig:
    """鸭子类型 _FullLogConfigLike 实例，避免本测试依赖 WebFullLogConfig。"""

    def __init__(self, *, enabled: bool, path: str) -> None:
        self.enabled = enabled
        self.path = path
        self.queue_size = 100
        self.rotate_daily = False


def _make_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send_json = AsyncMock(return_value=None)
    ws.close = AsyncMock(return_value=None)
    return ws


@pytest.fixture(autouse=True)
def _reset_full_logger() -> None:
    """每个用例独立 logger 实例，避免上一个测试残留。"""
    _reset_for_tests()
    yield
    _reset_for_tests()


def _read_log_lines(path: Path) -> list[dict]:
    """读 jsonl 文件返回每行 parsed dict 列表。"""
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return [json.loads(line) for line in raw.splitlines()]


# ---------------------------------------------------------------------------
# turn.* 写入路径
# ---------------------------------------------------------------------------


async def test_emit_turn_start_writes_full_log(tmp_path: Path) -> None:
    log_path = tmp_path / "full_log.jsonl"
    flogger: FullLogger = init_full_logger(_StubConfig(enabled=True, path=str(log_path)))
    await flogger.start()

    ws = _make_ws()
    sink = WSEventSink(ws, thread_id="thread-abc123def456")
    await sink.emit(Event(kind="turn.start", run_id="r1", turn=0))

    await flogger.aclose()  # flush 队列

    lines = _read_log_lines(log_path)
    assert len(lines) == 1
    record = lines[0]
    assert record["dir"] == "s2c"
    assert record["channel"] == "ws.threads"
    assert record["thread_id"] == "thread-abc123def456"
    assert record["payload"]["frame_type"] == "turn.start"
    assert record["payload"]["turn"] == 0
    assert record["payload"]["run_id"] == "r1"
    # ts 必须是 UTC+8 ISO8601 毫秒精度
    assert record["ts"].endswith("+08:00")


async def test_emit_turn_end_writes_full_log(tmp_path: Path) -> None:
    log_path = tmp_path / "full_log.jsonl"
    flogger = init_full_logger(_StubConfig(enabled=True, path=str(log_path)))
    await flogger.start()

    ws = _make_ws()
    sink = WSEventSink(ws, thread_id="thread-xyz")
    await sink.emit(Event(kind="turn.end", run_id="r1", turn=1))

    await flogger.aclose()
    lines = _read_log_lines(log_path)
    assert len(lines) == 1
    assert lines[0]["payload"]["frame_type"] == "turn.end"
    assert lines[0]["payload"]["turn"] == 1


async def test_emit_turn_start_and_end_paired(tmp_path: Path) -> None:
    """一轮对话 turn.start + turn.end 配对写入。"""
    log_path = tmp_path / "full_log.jsonl"
    flogger = init_full_logger(_StubConfig(enabled=True, path=str(log_path)))
    await flogger.start()

    ws = _make_ws()
    sink = WSEventSink(ws, thread_id="thread-pair")
    await sink.emit(Event(kind="turn.start", run_id="r1", turn=0))
    await sink.emit(Event(kind="turn.end", run_id="r1", turn=0))

    await flogger.aclose()
    lines = _read_log_lines(log_path)
    types = [line["payload"]["frame_type"] for line in lines]
    assert types == ["turn.start", "turn.end"]


# ---------------------------------------------------------------------------
# 不在白名单的 kind 不写入
# ---------------------------------------------------------------------------


async def test_emit_content_delta_does_not_write_full_log(tmp_path: Path) -> None:
    """阶段 1 白名单：content.delta 不应写 full_log（阶段 2 #11 才放开）。"""
    log_path = tmp_path / "full_log.jsonl"
    flogger = init_full_logger(_StubConfig(enabled=True, path=str(log_path)))
    await flogger.start()

    ws = _make_ws()
    sink = WSEventSink(ws, thread_id="thread-ct")
    await sink.emit(Event(kind="content.delta", run_id="r1", turn=1, payload={"delta": "hi"}))
    await flogger.aclose()
    assert _read_log_lines(log_path) == []


async def test_emit_tool_call_does_not_write_full_log(tmp_path: Path) -> None:
    log_path = tmp_path / "full_log.jsonl"
    flogger = init_full_logger(_StubConfig(enabled=True, path=str(log_path)))
    await flogger.start()

    ws = _make_ws()
    sink = WSEventSink(ws, thread_id="thread-tool")
    await sink.emit(
        Event(
            kind="tool.call.start",
            run_id="r1",
            turn=1,
            payload={"tool_name": "shell", "call_id": "c1"},
        )
    )
    await flogger.aclose()
    assert _read_log_lines(log_path) == []


async def test_emit_usage_does_not_write_full_log(tmp_path: Path) -> None:
    log_path = tmp_path / "full_log.jsonl"
    flogger = init_full_logger(_StubConfig(enabled=True, path=str(log_path)))
    await flogger.start()

    ws = _make_ws()
    sink = WSEventSink(ws, thread_id="thread-usage")
    await sink.emit(
        Event(
            kind="usage",
            run_id="r1",
            turn=1,
            payload={"input_tokens": 10, "output_tokens": 5},
        )
    )
    await flogger.aclose()
    assert _read_log_lines(log_path) == []


# ---------------------------------------------------------------------------
# thread_id 默认值 / 兼容性
# ---------------------------------------------------------------------------


async def test_emit_turn_start_without_thread_id_logs_null(tmp_path: Path) -> None:
    """构造 WSEventSink 不传 thread_id（旧代码 / 测试），日志字段写 null。"""
    log_path = tmp_path / "full_log.jsonl"
    flogger = init_full_logger(_StubConfig(enabled=True, path=str(log_path)))
    await flogger.start()

    ws = _make_ws()
    sink = WSEventSink(ws)  # 不传 thread_id
    await sink.emit(Event(kind="turn.start", run_id="r1", turn=0))

    await flogger.aclose()
    lines = _read_log_lines(log_path)
    assert len(lines) == 1
    assert lines[0]["thread_id"] is None


# ---------------------------------------------------------------------------
# enabled=False 零开销
# ---------------------------------------------------------------------------


async def test_emit_with_full_logger_disabled_no_write(tmp_path: Path) -> None:
    log_path = tmp_path / "full_log.jsonl"
    flogger = init_full_logger(_StubConfig(enabled=False, path=str(log_path)))
    await flogger.start()  # enabled=False 时是 no-op

    ws = _make_ws()
    sink = WSEventSink(ws, thread_id="thread-disabled")
    await sink.emit(Event(kind="turn.start", run_id="r1", turn=0))
    await sink.emit(Event(kind="turn.end", run_id="r1", turn=0))

    await flogger.aclose()
    assert not log_path.exists() or _read_log_lines(log_path) == []


async def test_emit_with_no_logger_init_no_crash(tmp_path: Path) -> None:
    """完全不 init_full_logger 时，emit 应该走"哑实例"分支，不抛不卡。"""
    # 没 init —— get_full_logger() 应该返回 disabled 哑实例
    ws = _make_ws()
    sink = WSEventSink(ws, thread_id="thread-no-init")
    await sink.emit(Event(kind="turn.start", run_id="r1", turn=0))
    # 没有抛异常 + send_json 仍然被调用就算过
    ws.send_json.assert_awaited_once()


# ---------------------------------------------------------------------------
# send 失败优先于 logger 调用（不在断连时浪费 logger 资源）
# ---------------------------------------------------------------------------


async def test_emit_send_failure_does_not_write_full_log(tmp_path: Path) -> None:
    """send_json 抛 → 标 closed + 不调 logger（已无意义）。"""
    log_path = tmp_path / "full_log.jsonl"
    flogger = init_full_logger(_StubConfig(enabled=True, path=str(log_path)))
    await flogger.start()

    ws = _make_ws()
    ws.send_json.side_effect = RuntimeError("WS disconnected")
    sink = WSEventSink(ws, thread_id="thread-broken")
    await sink.emit(Event(kind="turn.start", run_id="r1", turn=0))

    await flogger.aclose()
    assert _read_log_lines(log_path) == []
    assert sink.closed is True
