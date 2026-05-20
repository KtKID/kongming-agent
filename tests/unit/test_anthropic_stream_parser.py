"""AnthropicStreamParser 单元测试。

对应 ``docs/llm-provider-v0.2/streaming/04-data-and-state.md`` §5 映射表 +
``02-module-breakdown.md`` 模块 C。

覆盖：

- 基础事件序列 → 等价 LLMStreamChunk 序列（text / thinking / tool_use）
- input_json_delta 增量累积 + 流末一次 json.loads
- usage 累积（含 cache_creation / cache_read）
- stop_reason 映射（end_turn / tool_use / max_tokens / refusal / 其它）
- 序列不变式（message.done 收尾、tool_call.start 唯一、tool_call.end 仅承载定位信息）
- **流结束守卫**两条（无 message_start / 见 message_start 但无 stop_reason 且无 tool_use）
- Stall 检测 logger.warning（不主动 abort）
- error event → ProviderError
"""

from __future__ import annotations

import json

import pytest

from core.contracts import LLMStreamChunk
from core.errors import ProviderError
from executors.llm.anthropic_stream_parser import AnthropicStreamParser

from .streaming.fixtures import (
    async_iter_events,
    collect_content_deltas,
    collect_reasoning_deltas,
    make_anthropic_block_start,
    make_anthropic_block_stop,
    make_anthropic_input_json_delta,
    make_anthropic_message_delta,
    make_anthropic_message_start,
    make_anthropic_message_stop,
    make_anthropic_text_delta,
    make_anthropic_thinking_delta,
)


async def _run(events: list[tuple[str | None, dict]]) -> list[LLMStreamChunk]:
    parser = AnthropicStreamParser()
    return [c async for c in parser.parse(async_iter_events(events))]


# ---------------------------------------------------------------------------
# 基础：纯 text block
# ---------------------------------------------------------------------------


async def test_pure_text_block() -> None:
    events = [
        make_anthropic_message_start(input_tokens=10),
        make_anthropic_block_start(index=0, block_type="text"),
        make_anthropic_text_delta(index=0, text="hello "),
        make_anthropic_text_delta(index=0, text="world"),
        make_anthropic_block_stop(index=0),
        make_anthropic_message_delta(stop_reason="end_turn", output_tokens=5),
        make_anthropic_message_stop(),
    ]
    chunks = await _run(events)

    # 序列不变式 1：message.done 收尾
    assert chunks[-1].kind == "message.done"
    assert chunks[-1].message is not None
    # 序列不变式 5：content == 所有 content.delta 拼接
    assert chunks[-1].message.content == "hello world"
    assert collect_content_deltas(chunks) == "hello world"
    # content.delta 的 index 始终 0（V1 单正文 block 语义）
    for c in chunks:
        if c.kind == "content.delta":
            assert c.index == 0
    # finish_reason 映射
    assert chunks[-1].finish_reason == "stop"
    # usage 透传 + 累加（task#3.1：新增 SDK 原生字段 + provider_kind）
    assert chunks[-1].usage == {
        "provider_kind": "anthropic",
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }


# ---------------------------------------------------------------------------
# thinking block → reasoning.delta + provider_metadata.reasoning_content
# ---------------------------------------------------------------------------


async def test_thinking_block_emits_reasoning_delta() -> None:
    events = [
        make_anthropic_message_start(input_tokens=8),
        make_anthropic_block_start(index=0, block_type="thinking"),
        make_anthropic_thinking_delta(index=0, thinking="嗯"),
        make_anthropic_thinking_delta(index=0, thinking="，让我想想"),
        make_anthropic_block_stop(index=0),
        make_anthropic_block_start(index=1, block_type="text"),
        make_anthropic_text_delta(index=1, text="答案是 42"),
        make_anthropic_block_stop(index=1),
        make_anthropic_message_delta(stop_reason="end_turn", output_tokens=12),
        make_anthropic_message_stop(),
    ]
    chunks = await _run(events)

    # reasoning 增量按到达顺序 yield
    assert collect_reasoning_deltas(chunks) == "嗯，让我想想"
    # text 部分独立累积
    assert chunks[-1].message is not None
    assert chunks[-1].message.content == "答案是 42"
    # reasoning_content 写到 provider_metadata
    meta = chunks[-1].provider_metadata
    assert meta["reasoning_content"] == "嗯，让我想想"
    assert meta["reasoning_content_length"] == len("嗯，让我想想")


# ---------------------------------------------------------------------------
# tool_use block：start → input_json_delta 增量 → end → 流末聚合
# ---------------------------------------------------------------------------


async def test_tool_use_block_with_input_json_streaming() -> None:
    events = [
        make_anthropic_message_start(input_tokens=20),
        make_anthropic_block_start(
            index=0,
            block_type="tool_use",
            tool_id="toolu_abc",
            tool_name="ReadFile",
        ),
        make_anthropic_input_json_delta(index=0, partial_json='{"path"'),
        make_anthropic_input_json_delta(index=0, partial_json=': "/etc/'),
        make_anthropic_input_json_delta(index=0, partial_json='hosts"}'),
        make_anthropic_block_stop(index=0),
        make_anthropic_message_delta(stop_reason="tool_use", output_tokens=18),
        make_anthropic_message_stop(),
    ]
    chunks = await _run(events)

    # 序列不变式 2：tool_call.start 在所有 arguments.delta 之前；
    # tool_call.end 在该 index 所有 delta 之后
    kinds = [c.kind for c in chunks]
    start_idx = kinds.index("tool_call.start")
    end_idx = kinds.index("tool_call.end")
    args_indices = [i for i, k in enumerate(kinds) if k == "tool_call.arguments.delta"]
    assert all(start_idx < a < end_idx for a in args_indices)

    # start 唯一（不变式 3）
    assert kinds.count("tool_call.start") == 1

    # tool_call.end 只承载定位信息（不变式 6）
    end_chunk = chunks[end_idx]
    assert end_chunk.tool_call_id == "toolu_abc"
    assert end_chunk.tool_name == "ReadFile"
    assert end_chunk.delta == ""

    # arguments.delta 内容拼回 raw json，能在 message.done 解析成 dict
    args_pieces = [c.delta for c in chunks if c.kind == "tool_call.arguments.delta"]
    assert "".join(args_pieces) == '{"path": "/etc/hosts"}'

    # message.done：tool_calls 列表非空，arguments 是合法 dict
    last = chunks[-1]
    assert last.kind == "message.done"
    assert last.message is not None
    assert last.message.tool_calls is not None
    assert len(last.message.tool_calls) == 1
    tc = last.message.tool_calls[0]
    assert tc.call_id == "toolu_abc"
    assert tc.tool_name == "ReadFile"
    assert tc.arguments == {"path": "/etc/hosts"}
    # finish_reason 映射 tool_use → tool_calls
    assert last.finish_reason == "tool_calls"


# ---------------------------------------------------------------------------
# input_json_delta 解析失败 → arguments 保留原始字符串（与 OpenAI parser 一致）
# ---------------------------------------------------------------------------


async def test_tool_use_with_invalid_json_keeps_raw() -> None:
    events = [
        make_anthropic_message_start(input_tokens=5),
        make_anthropic_block_start(
            index=0,
            block_type="tool_use",
            tool_id="toolu_xyz",
            tool_name="Shell",
        ),
        make_anthropic_input_json_delta(index=0, partial_json='{"cmd": "ls'),
        # 故意不闭合
        make_anthropic_block_stop(index=0),
        make_anthropic_message_delta(stop_reason="tool_use", output_tokens=4),
        make_anthropic_message_stop(),
    ]
    chunks = await _run(events)
    last = chunks[-1]
    assert last.message is not None
    assert last.message.tool_calls is not None
    tc = last.message.tool_calls[0]
    assert "__raw__" in tc.arguments


# ---------------------------------------------------------------------------
# usage 累加 + cache token 字段写入 provider_metadata
# ---------------------------------------------------------------------------


async def test_usage_and_cache_tokens() -> None:
    events = [
        make_anthropic_message_start(
            msg_id="msg_cached",
            model="claude-test",
            input_tokens=100,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=300,
        ),
        make_anthropic_block_start(index=0, block_type="text"),
        make_anthropic_text_delta(index=0, text="ok"),
        make_anthropic_block_stop(index=0),
        make_anthropic_message_delta(stop_reason="end_turn", output_tokens=42),
        make_anthropic_message_stop(),
    ]
    chunks = await _run(events)
    last = chunks[-1]
    # task#3.1：prompt_tokens 修 lossy bug，现在含 cache_read + cache_creation
    # 真值 = 100 + 300 + 200 = 600
    assert last.usage == {
        "provider_kind": "anthropic",
        "input_tokens": 100,
        "output_tokens": 42,
        "cache_read_input_tokens": 300,
        "cache_creation_input_tokens": 200,
        "prompt_tokens": 600,  # ← 修 lossy: input + cache_read + cache_creation
        "completion_tokens": 42,
        "total_tokens": 642,  # ← 600 + 42
    }
    meta = last.provider_metadata
    assert meta["id"] == "msg_cached"
    assert meta["model"] == "claude-test"
    assert meta["cache_creation_input_tokens"] == 200
    assert meta["cache_read_input_tokens"] == 300


# ---------------------------------------------------------------------------
# stop_reason 映射全表
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_stop_reason", "expect_finish"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("refusal", "error"),
        ("some_unknown", "other"),
    ],
)
async def test_stop_reason_mapping(raw_stop_reason: str, expect_finish: str) -> None:
    events = [
        make_anthropic_message_start(input_tokens=1),
        make_anthropic_block_start(index=0, block_type="text"),
        make_anthropic_text_delta(index=0, text="x"),
        make_anthropic_block_stop(index=0),
        make_anthropic_message_delta(stop_reason=raw_stop_reason, output_tokens=1),
        make_anthropic_message_stop(),
    ]
    chunks = await _run(events)
    assert chunks[-1].finish_reason == expect_finish


# ---------------------------------------------------------------------------
# 守卫 1：从未见 message_start
# ---------------------------------------------------------------------------


async def test_guard_no_message_start_raises() -> None:
    events: list[tuple[str | None, dict]] = [
        # 直接给一个 ping，再就结束（模拟代理 200 但 body 非 SSE / 空 SSE）
        ("ping", {"type": "ping"}),
    ]
    with pytest.raises(ProviderError) as excinfo:
        await _run(events)
    assert "without receiving message_start" in str(excinfo.value)
    assert excinfo.value.details is not None
    assert excinfo.value.details.get("chunk_count") == 1


# ---------------------------------------------------------------------------
# 守卫 2：见 message_start 但既无 stop_reason 也无 tool_use 完成
# ---------------------------------------------------------------------------


async def test_guard_message_start_but_no_stop_reason_raises() -> None:
    events = [
        make_anthropic_message_start(msg_id="msg_partial", input_tokens=10),
        # 注意：没有 message_delta（无 stop_reason），也没有任何 content_block_stop
    ]
    with pytest.raises(ProviderError) as excinfo:
        await _run(events)
    assert "without stop_reason" in str(excinfo.value)
    assert excinfo.value.details is not None
    assert excinfo.value.details.get("message_id") == "msg_partial"


async def test_guard_text_block_without_stop_reason_raises() -> None:
    """text block 完成但没 message_delta：也算未完成（tool_use 才算独立完成）。"""
    events = [
        make_anthropic_message_start(input_tokens=10),
        make_anthropic_block_start(index=0, block_type="text"),
        make_anthropic_text_delta(index=0, text="不完整..."),
        make_anthropic_block_stop(index=0),
        # 故意省略 message_delta + message_stop
    ]
    with pytest.raises(ProviderError) as excinfo:
        await _run(events)
    assert "without stop_reason" in str(excinfo.value)


# ---------------------------------------------------------------------------
# tool_use block 独立完成则不触发守卫 2（即使没 message_delta）
# ---------------------------------------------------------------------------


async def test_tool_use_complete_without_message_delta_passes_guard() -> None:
    """tool_use block 单独完成时即便 message_delta 缺失也允许（防御实现侧异常切流）。"""
    events = [
        make_anthropic_message_start(input_tokens=5),
        make_anthropic_block_start(index=0, block_type="tool_use", tool_id="t1", tool_name="X"),
        make_anthropic_input_json_delta(index=0, partial_json="{}"),
        make_anthropic_block_stop(index=0),
        # 故意省略 message_delta；不应抛错
    ]
    chunks = await _run(events)
    last = chunks[-1]
    assert last.kind == "message.done"
    # finish_reason 默认 end_turn → 但 tool_calls 存在时强制 tool_calls
    assert last.finish_reason == "tool_calls"


# ---------------------------------------------------------------------------
# error event → ProviderError
# ---------------------------------------------------------------------------


async def test_error_event_raises_provider_error() -> None:
    events: list[tuple[str | None, dict]] = [
        make_anthropic_message_start(input_tokens=1),
        (
            "error",
            {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
        ),
    ]
    with pytest.raises(ProviderError) as excinfo:
        await _run(events)
    assert "overloaded_error" in str(excinfo.value)
    assert "busy" in str(excinfo.value)


# ---------------------------------------------------------------------------
# stall 检测：logger.warning（不主动 abort）
# ---------------------------------------------------------------------------


async def test_stall_detection_emits_warning(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    """连续两个 chunk 间隔 > 30s 时 emit logger.warning。

    替换 parser 模块内的 ``time.monotonic`` 模拟时间跳跃；不真实 sleep。
    用 itertools.count + 闭包给确定性时间序列，第二次调用比第一次晚 45s。
    """
    import logging

    from executors.llm import anthropic_stream_parser as mod

    # 第 1 次 100.0，第 2 次 145.0（跳 45s 触发 stall），之后递增 0.1 不再触发
    call_count = {"n": 0}

    def fake_monotonic() -> float:
        n = call_count["n"]
        call_count["n"] = n + 1
        if n == 0:
            return 100.0
        if n == 1:
            return 145.0
        return 145.0 + n * 0.1

    # 用替换 mod.time.monotonic 的方式，作用域仅本模块（不污染其它模块）
    monkeypatch.setattr(mod.time, "monotonic", fake_monotonic)

    events = [
        make_anthropic_message_start(input_tokens=1),
        make_anthropic_block_start(index=0, block_type="text"),
        make_anthropic_text_delta(index=0, text="x"),
        make_anthropic_block_stop(index=0),
        make_anthropic_message_delta(stop_reason="end_turn", output_tokens=1),
        make_anthropic_message_stop(),
    ]
    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        chunks = await _run(events)
    assert chunks[-1].kind == "message.done"
    # 应当有至少一条包含 "stall" 的 warning
    stall_records = [r for r in caplog.records if "stall" in r.getMessage()]
    assert stall_records, "expected stall warning"


# ---------------------------------------------------------------------------
# message.done 等价性：拼接 content + JSON-able tool args
# ---------------------------------------------------------------------------


async def test_message_done_equivalence() -> None:
    """tool_use + text 混排：content 拼接 + tool_calls 列表完整。"""
    events = [
        make_anthropic_message_start(input_tokens=20),
        make_anthropic_block_start(index=0, block_type="text"),
        make_anthropic_text_delta(index=0, text="先调工具:"),
        make_anthropic_block_stop(index=0),
        make_anthropic_block_start(index=1, block_type="tool_use", tool_id="tu1", tool_name="Calc"),
        make_anthropic_input_json_delta(index=1, partial_json='{"a":1,'),
        make_anthropic_input_json_delta(index=1, partial_json='"b":2}'),
        make_anthropic_block_stop(index=1),
        make_anthropic_message_delta(stop_reason="tool_use", output_tokens=15),
        make_anthropic_message_stop(),
    ]
    chunks = await _run(events)
    last = chunks[-1]
    assert last.message is not None
    assert last.message.content == "先调工具:"
    assert last.message.tool_calls is not None
    assert len(last.message.tool_calls) == 1
    tc = last.message.tool_calls[0]
    assert tc.tool_name == "Calc"
    assert tc.arguments == {"a": 1, "b": 2}
    # 验证 round-trip：流末 arguments 来自 partial_json 累积，json.loads 一次成功
    raw = '{"a":1,' + '"b":2}'
    assert json.loads(raw) == tc.arguments
