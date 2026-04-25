"""CLIStreamSink 单元测试（D#8）。

覆盖：
- D.1 content.delta → stdout
- D.2 reasoning.delta 默认带 ANSI 灰色包裹
- D.3 reasoning_color=None 时 reasoning.delta 不染色
- D.4 llm.stream.end → 收尾换行
- D.5 tool.call.start → 收尾换行
- D.6 未识别 kind 静默忽略，不抛异常
"""

from __future__ import annotations

import io

import pytest

from core.contracts import Event
from host.cli_stream_sink import CLIStreamSink


@pytest.mark.asyncio
async def test_d1_content_delta_writes_to_stdout() -> None:
    """D.1：content.delta 直接写 stdout。"""
    out = io.StringIO()
    sink = CLIStreamSink(out=out)
    await sink.emit(
        Event(kind="content.delta", run_id="r1", payload={"delta": "Hello", "index": 0})
    )
    await sink.emit(
        Event(kind="content.delta", run_id="r1", payload={"delta": " world", "index": 0})
    )
    assert out.getvalue() == "Hello world"


@pytest.mark.asyncio
async def test_d2_reasoning_delta_wrapped_in_ansi_grey() -> None:
    """D.2：reasoning.delta 默认用 ANSI 灰色（\\x1b[90m）包裹 + reset（\\x1b[0m）。"""
    out = io.StringIO()
    sink = CLIStreamSink(out=out)
    await sink.emit(Event(kind="reasoning.delta", run_id="r1", payload={"delta": "thinking"}))
    assert out.getvalue() == "\x1b[90mthinking\x1b[0m"


@pytest.mark.asyncio
async def test_d3_reasoning_color_none_disables_ansi() -> None:
    """D.3：reasoning_color=None 时直接打印，不带 ANSI 序列。"""
    out = io.StringIO()
    sink = CLIStreamSink(out=out, reasoning_color=None)
    await sink.emit(Event(kind="reasoning.delta", run_id="r1", payload={"delta": "thinking"}))
    assert out.getvalue() == "thinking"


@pytest.mark.asyncio
async def test_d4_stream_end_emits_newline() -> None:
    """D.4：llm.stream.end 收尾换行。"""
    out = io.StringIO()
    sink = CLIStreamSink(out=out)
    await sink.emit(Event(kind="content.delta", run_id="r1", payload={"delta": "ans"}))
    await sink.emit(Event(kind="llm.stream.end", run_id="r1", payload={"chunk_count": 2}))
    assert out.getvalue() == "ans\n"


@pytest.mark.asyncio
async def test_d5_tool_call_start_emits_newline() -> None:
    """D.5：tool.call.start 在 tool 调用前收尾换行。"""
    out = io.StringIO()
    sink = CLIStreamSink(out=out)
    await sink.emit(Event(kind="content.delta", run_id="r1", payload={"delta": "pre-tool"}))
    await sink.emit(
        Event(
            kind="tool.call.start",
            run_id="r1",
            payload={"call_id": "c1", "tool_name": "ToolA"},
        )
    )
    assert out.getvalue() == "pre-tool\n"


@pytest.mark.asyncio
async def test_d6_unknown_kind_silently_ignored() -> None:
    """D.6：未识别 kind / 空 delta / 非 str 都静默忽略，不抛异常。"""
    out = io.StringIO()
    sink = CLIStreamSink(out=out)
    # 未识别 kind
    await sink.emit(Event(kind="approval.request", run_id="r1", payload={"foo": "bar"}))
    # 空 delta
    await sink.emit(Event(kind="content.delta", run_id="r1", payload={"delta": ""}))
    await sink.emit(Event(kind="reasoning.delta", run_id="r1", payload={"delta": ""}))
    # delta 不是 str（防御性）
    await sink.emit(
        Event(kind="content.delta", run_id="r1", payload={"delta": 123, "index": 0})
    )
    # 全部应该没输出
    assert out.getvalue() == ""
