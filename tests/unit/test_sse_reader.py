"""sse_reader 行缓冲 / JSON 解包单测。

对应 ``docs/llm-provider-v0.2/streaming/06-test-cases-detailed.md`` §1。
"""

from __future__ import annotations

import logging
from typing import Any, cast

import httpx
import pytest

from infrastructure.llm_providers.sse_reader import iter_sse_events

from .streaming.fixtures import mock_response_with_bytes


async def _collect(
    raw: bytes, *, chunk_size: int = 4096
) -> list[tuple[str | None, dict[str, Any]]]:
    response = cast(httpx.Response, mock_response_with_bytes(raw, chunk_size=chunk_size))
    return [e async for e in iter_sse_events(response)]


async def test_openai_style_single_chunk() -> None:
    """A.1：最小 OpenAI 样本 —— 单条 data 行。"""
    raw = b'data: {"id":"x","choices":[{"delta":{"content":"hi"}}]}\n\n'
    events = await _collect(raw)
    assert events == [
        (None, {"id": "x", "choices": [{"delta": {"content": "hi"}}]}),
    ]


async def test_openai_style_multi_chunk() -> None:
    """A.2：多 data 行顺序保持。"""
    raw = (
        b'data: {"id":"x","choices":[{"delta":{"content":"a"}}]}\n\n'
        b'data: {"id":"x","choices":[{"delta":{"content":"b"}}]}\n\n'
        b'data: {"id":"x","choices":[{"delta":{"content":"c"}}]}\n\n'
    )
    events = await _collect(raw)
    assert len(events) == 3
    assert [e[1]["choices"][0]["delta"]["content"] for e in events] == ["a", "b", "c"]
    # OpenAI 不带 event: 行；全为 None
    assert all(e[0] is None for e in events)


async def test_done_sentinel_terminates() -> None:
    """A.3：[DONE] 哨兵立即终止；之后的行被忽略。"""
    raw = b'data: {"id":"x"}\n\ndata: [DONE]\n\ndata: {"id":"should_not_be_seen"}\n\n'
    events = await _collect(raw)
    assert events == [(None, {"id": "x"})]


async def test_line_span_tcp_packets() -> None:
    """A.4：跨 TCP 包的行拼接 —— chunk_size=3 强制分包。"""
    raw = b'data: {"id":"abc","choices":[{"delta":{"content":"hello world"}}]}\n\ndata: [DONE]\n\n'
    events = await _collect(raw, chunk_size=3)
    assert events == [
        (
            None,
            {
                "id": "abc",
                "choices": [{"delta": {"content": "hello world"}}],
            },
        ),
    ]


async def test_skip_heartbeat_comment_lines() -> None:
    """A.5：: 开头的注释行 / 空行被安全跳过。"""
    raw = (
        b": keep-alive comment\n"
        b"\n"
        b'data: {"n":1}\n\n'
        b": another comment\n"
        b'data: {"n":2}\n\n'
        b"data: [DONE]\n\n"
    )
    events = await _collect(raw)
    assert events == [(None, {"n": 1}), (None, {"n": 2})]


async def test_malformed_json_warns_and_skips(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A.6：JSON 解析失败 —— log.warning 后跳过，不抛。"""
    raw = b'data: {this is not json}\n\ndata: {"id":"x"}\n\ndata: [DONE]\n\n'
    with caplog.at_level(logging.WARNING, logger="infrastructure.llm_providers.sse_reader"):
        events = await _collect(raw)
    # 第二条合法行仍然 yield
    assert events == [(None, {"id": "x"})]
    # 至少有一条 warning 关于该失败行
    assert any("failed to decode" in rec.message.lower() for rec in caplog.records)


async def test_anthropic_style_event_name_preserved() -> None:
    """A.7：Anthropic 风格 —— event: 行名保留到对应 data 行。"""
    raw = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_1"}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"hi"}}\n\n'
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n'
    )
    events = await _collect(raw)
    assert len(events) == 3
    assert events[0][0] == "message_start"
    assert events[0][1]["message"]["id"] == "msg_1"
    assert events[1][0] == "content_block_delta"
    assert events[1][1]["delta"]["text"] == "hi"
    assert events[2][0] == "message_stop"


async def test_event_name_does_not_leak_to_subsequent_data() -> None:
    """A.7 补强：event_name 在对应 data yield 后重置；不 leak 到无 event: 前缀的行。"""
    raw = b'event: some_event\ndata: {"n":1}\n\ndata: {"n":2}\n\n'
    events = await _collect(raw)
    assert events == [
        ("some_event", {"n": 1}),
        (None, {"n": 2}),
    ]
