"""WebHostAdapter 单测。

覆盖当前职责：

- `write_output` / `notify_event` 行为
- `read_input` 永远抛 NotImplementedError
- `close` 幂等
- `attach_ws` 重置 closed
- `_safe_send_json` 异常吞咽 + 标 closed
- `render_result` 无 error no-op / 有 error 推 [error] 帧 / closed 时 no-op
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.contracts import Event
from core.errors import AgentError
from core.result import Result
from hosts.web.app_support.host_adapter import WebHostAdapter


def _make_ws() -> AsyncMock:
    """构造一个 duck-typed WS mock。"""
    ws = AsyncMock()
    ws.send_json = AsyncMock(return_value=None)
    ws.close = AsyncMock(return_value=None)
    return ws


async def test_read_input_raises_not_implemented() -> None:
    adapter = WebHostAdapter(_make_ws())
    with pytest.raises(NotImplementedError):
        await adapter.read_input()


async def test_notify_event_is_noop() -> None:
    """notify_event 不应推 WS，也不抛。"""
    ws = _make_ws()
    adapter = WebHostAdapter(ws)
    event = Event(kind="turn.start", run_id="r", turn=1)
    await adapter.notify_event(event)
    ws.send_json.assert_not_called()


async def test_write_output_pushes_assistant_final_frame() -> None:
    ws = _make_ws()
    adapter = WebHostAdapter(ws)
    await adapter.write_output("hello world")
    ws.send_json.assert_awaited_once()
    payload = ws.send_json.await_args.args[0]
    assert payload["frame_type"] == "assistant.final"
    assert payload["content"] == "hello world"
    assert payload["turn"] == -1
    assert isinstance(payload["timestamp_ms"], int)


async def test_write_output_silent_when_closed() -> None:
    ws = _make_ws()
    adapter = WebHostAdapter(ws)
    await adapter.close()
    await adapter.write_output("after-close")
    ws.send_json.assert_not_called()


async def test_close_is_idempotent() -> None:
    adapter = WebHostAdapter(_make_ws())
    await adapter.close()
    assert adapter.closed is True
    await adapter.close()
    assert adapter.closed is True


async def test_attach_ws_replaces_reference_and_resets_closed() -> None:
    old_ws = _make_ws()
    new_ws = _make_ws()
    adapter = WebHostAdapter(old_ws)
    await adapter.close()
    assert adapter.closed is True

    adapter.attach_ws(new_ws)
    assert adapter.closed is False

    await adapter.write_output("after-reconnect")
    new_ws.send_json.assert_awaited_once()
    old_ws.send_json.assert_not_called()


async def test_safe_send_json_marks_closed_on_send_failure() -> None:
    ws = _make_ws()
    ws.send_json = AsyncMock(side_effect=ConnectionError("ws gone"))
    adapter = WebHostAdapter(ws)
    await adapter.write_output("first")
    assert adapter.closed is True
    await adapter.write_output("second")


async def test_safe_send_json_best_effort_close_does_not_raise() -> None:
    """send 失败后 best-effort ws.close() 也不抛。"""
    ws = _make_ws()
    ws.send_json = AsyncMock(side_effect=RuntimeError("send failed"))
    ws.close = AsyncMock(side_effect=RuntimeError("close failed"))
    adapter = WebHostAdapter(ws)
    await adapter.write_output("x")
    assert adapter.closed is True


async def test_write_output_swallows_arbitrary_exception() -> None:
    ws = _make_ws()
    ws.send_json = AsyncMock(side_effect=ValueError("anything"))
    adapter = WebHostAdapter(ws)
    await adapter.write_output("x")
    assert adapter.closed is True


async def test_write_output_timestamp_is_milliseconds() -> None:
    ws = _make_ws()
    adapter = WebHostAdapter(ws)
    await adapter.write_output("hi")
    payload: dict[str, Any] = ws.send_json.await_args.args[0]
    assert payload["timestamp_ms"] > 1_000_000_000_000


# ----------------------------------------------------------------------
# render_result：无 error no-op / 有 error 推 [error] 帧 / closed 时 no-op
# ----------------------------------------------------------------------


def _make_result(*, error: AgentError | None = None) -> Result:
    """构造一个最小 Result。completed 路径 error=None；failed 路径传 error。"""
    return Result(
        run_id="run-1",
        session_id="thread-1",
        status="failed" if error is not None else "completed",
        final_message=None,
        turn_count=0,
        error=error,
        metadata={},
    )


async def test_render_result_noop_when_no_error() -> None:
    """无 error 的 Result(正常完成)走 no-op,不推 WS 帧。

    web 主路径下内容 / 用量 / interrupt 全由 WSEventSink 实时帧承担,
    render_result 只在 error 兜底时有活——本用例锁住 no-op 契约。
    """
    ws = _make_ws()
    adapter = WebHostAdapter(ws)
    await adapter.render_result(_make_result(error=None))
    ws.send_json.assert_not_called()


async def test_render_result_pushes_error_frame_when_error() -> None:
    """有 error 的 Result 推一条 AssistantFinalFrame,content 形如 [error] Type: msg。"""
    ws = _make_ws()
    adapter = WebHostAdapter(ws)
    error = AgentError("boom")
    await adapter.render_result(_make_result(error=error))

    ws.send_json.assert_awaited_once()
    payload = ws.send_json.await_args.args[0]
    assert payload["frame_type"] == "assistant.final"
    # content 含 [error] 前缀 + 异常类名 + 消息
    assert payload["content"] == "[error] AgentError: boom"
    assert "[error]" in payload["content"]
    assert payload["turn"] == -1
    assert isinstance(payload["timestamp_ms"], int)


async def test_render_result_noop_when_closed_even_with_error() -> None:
    """adapter._closed=True 时即使有 error 也 no-op(防御:浏览器已断连)。"""
    ws = _make_ws()
    adapter = WebHostAdapter(ws)
    await adapter.close()
    assert adapter.closed is True

    await adapter.render_result(_make_result(error=AgentError("boom")))
    ws.send_json.assert_not_called()
