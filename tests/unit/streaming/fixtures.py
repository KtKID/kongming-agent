"""流式 parser 单测的共享工厂 / 假对象 / 断言助手。

对应 ``docs/llm-provider-v0.2/streaming/06-test-cases-detailed.md`` §0。
两个测试文件（``test_sse_reader.py`` / ``test_openai_compat_stream_parser.py``）
共享本文件；不把辅助和具体用例耦合，方便后续模块 C / D / E 复用。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from core.contracts import LLMStreamChunk

# ---------------------------------------------------------------------------
# OpenAI chunk / tool_call delta 工厂
# ---------------------------------------------------------------------------


def make_openai_chunk(
    *,
    id: str = "chatcmpl-test",
    model: str | None = "test-model",
    content: str | None = None,
    reasoning_content: str | None = None,
    reasoning: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
    system_fingerprint: str | None = None,
) -> dict[str, Any]:
    """构造一个符合 OpenAI Chat Completions stream 形状的 chunk dict。

    None 字段不写入（对齐真实 API 行为；parser 走 ``.get()`` 路径）。
    """
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if reasoning_content is not None:
        delta["reasoning_content"] = reasoning_content
    if reasoning is not None:
        delta["reasoning"] = reasoning
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls

    choice: dict[str, Any] = {"index": 0, "delta": delta}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason

    chunk: dict[str, Any] = {"id": id, "choices": [choice]}
    if model is not None:
        chunk["model"] = model
    if system_fingerprint is not None:
        chunk["system_fingerprint"] = system_fingerprint
    if usage is not None:
        chunk["usage"] = usage
        # OpenAI / GLM: usage chunk 的 choices 为空
        chunk["choices"] = []
    return chunk


def make_tool_call_delta(
    *,
    index: int = 0,
    id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
    extra_content: Any = None,
    model_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 ``delta.tool_calls[i]`` 形状。"""
    out: dict[str, Any] = {"index": index}
    if id is not None:
        out["id"] = id
    if name is not None or arguments is not None:
        func: dict[str, Any] = {}
        if name is not None:
            func["name"] = name
        if arguments is not None:
            func["arguments"] = arguments
        out["function"] = func
    if extra_content is not None:
        out["extra_content"] = extra_content
    if model_extra is not None:
        out["model_extra"] = model_extra
    return out


# ---------------------------------------------------------------------------
# 断言助手
# ---------------------------------------------------------------------------


def assert_message_done_equivalent(
    chunks: list[LLMStreamChunk],
    *,
    expected_content: str | None,
    expected_tool_call_count: int = 0,
    expected_finish_reason: str = "stop",
) -> None:
    """断言流最后一个 chunk 是 ``message.done``，且聚合字段符合预期。"""
    assert chunks, "chunks is empty"
    last = chunks[-1]
    assert last.kind == "message.done", f"last kind is {last.kind}"
    assert last.message is not None
    assert last.message.content == expected_content, (
        f"content mismatch: got {last.message.content!r}, expected {expected_content!r}"
    )
    n_tool = len(last.message.tool_calls or ())
    assert n_tool == expected_tool_call_count, (
        f"tool_call count mismatch: got {n_tool}, expected {expected_tool_call_count}"
    )
    assert last.finish_reason == expected_finish_reason, (
        f"finish_reason mismatch: got {last.finish_reason}, expected {expected_finish_reason}"
    )


def collect_content_deltas(chunks: list[LLMStreamChunk]) -> str:
    return "".join(c.delta for c in chunks if c.kind == "content.delta")


def collect_reasoning_deltas(chunks: list[LLMStreamChunk]) -> str:
    return "".join(c.delta for c in chunks if c.kind == "reasoning.delta")


# ---------------------------------------------------------------------------
# AsyncIterator 包装
# ---------------------------------------------------------------------------


async def async_iter_events(
    items: list[tuple[str | None, dict[str, Any]]],
) -> AsyncIterator[tuple[str | None, dict[str, Any]]]:
    """把一个 list 包成 async iterator，喂给 parser.parse()。"""
    for item in items:
        yield item


# ---------------------------------------------------------------------------
# Fake httpx.Response（只实现 aiter_bytes()）
# ---------------------------------------------------------------------------


class FakeStreamingResponse:
    """模拟 :class:`httpx.Response`，仅实现 ``aiter_bytes()``。

    用于 sse_reader 单测：通过 ``chunk_size`` 强制分包，验证跨 TCP 包拼接正确性。
    """

    def __init__(self, raw: bytes, chunk_size: int = 4096) -> None:
        self._raw = raw
        self._chunk_size = max(1, chunk_size)

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for i in range(0, len(self._raw), self._chunk_size):
            yield self._raw[i : i + self._chunk_size]


def mock_response_with_bytes(raw: bytes, *, chunk_size: int = 4096) -> FakeStreamingResponse:
    return FakeStreamingResponse(raw, chunk_size=chunk_size)


__all__ = [
    "FakeStreamingResponse",
    "assert_message_done_equivalent",
    "async_iter_events",
    "collect_content_deltas",
    "collect_reasoning_deltas",
    "make_openai_chunk",
    "make_tool_call_delta",
    "mock_response_with_bytes",
]
