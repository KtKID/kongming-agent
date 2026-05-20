"""OpenAICompatStreamParser 单元测试。

对应 ``docs/llm-provider-v0.2/streaming/06-test-cases-detailed.md`` §2，
并按 §7 的构造指南补齐 A.8/A.9/A.10/A.11/A.13/A.15/A.16/A.18/A.20/A.21/A.22。

所有用例用 :mod:`.streaming.fixtures` 提供的 ``make_openai_chunk`` /
``make_tool_call_delta`` 工厂构造 chunk，不手写字典字面量（见 06 §7
"必须遵守的契约 #1"）。
"""

from __future__ import annotations

from core.contracts import LLMStreamChunk
from executors.llm.openai_compat_stream_parser import OpenAICompatStreamParser

from .streaming.fixtures import (
    assert_message_done_equivalent,
    async_iter_events,
    collect_content_deltas,
    collect_reasoning_deltas,
    make_openai_chunk,
    make_tool_call_delta,
)


async def _run(events: list[tuple[str | None, dict]]) -> list[LLMStreamChunk]:
    parser = OpenAICompatStreamParser()
    return [c async for c in parser.parse(async_iter_events(events))]


# ---------------------------------------------------------------------------
# A.7 / §2.1 — 基础纯内容等价
# ---------------------------------------------------------------------------


async def test_pure_content() -> None:
    events = [
        (None, make_openai_chunk(content="a")),
        (None, make_openai_chunk(content="b")),
        (None, make_openai_chunk(content="c")),
        (
            None,
            make_openai_chunk(
                content=None,
                finish_reason="stop",
                usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            ),
        ),
    ]
    chunks = await _run(events)

    # 不变式 1 + 5：message.done 是最后一个；message.content == 三段拼接
    assert_message_done_equivalent(chunks, expected_content="abc")

    # 正好 3 条 content.delta，顺序保留
    content_chunks = [c for c in chunks if c.kind == "content.delta"]
    assert [c.delta for c in content_chunks] == ["a", "b", "c"]

    # content.delta 的 index 都是 0（单正文 block 语义）
    assert all(c.index == 0 for c in content_chunks)

    # usage 正确透传（task#3.1：含 provider_kind + SDK 原生字段）
    assert chunks[-1].usage == {
        "provider_kind": "openai_compatible",
        "prompt_tokens": 5,
        "completion_tokens": 3,
        "total_tokens": 8,
        "input_tokens": 5,
        "output_tokens": 3,
    }


# ---------------------------------------------------------------------------
# A.8 — content 等价不变式（显式断言）
# ---------------------------------------------------------------------------


async def test_content_equivalence_invariant() -> None:
    """不变式 1：message.content == 所有 content.delta 拼接。"""
    pieces = ["The ", "quick ", "brown ", "fox"]
    events = [(None, make_openai_chunk(content=p)) for p in pieces]
    events.append((None, make_openai_chunk(content=None, finish_reason="stop")))
    chunks = await _run(events)

    concat = collect_content_deltas(chunks)
    done = chunks[-1]
    assert done.message is not None
    assert done.message.content == concat == "The quick brown fox"


# ---------------------------------------------------------------------------
# A.9 — reasoning_content 单独存在时的累积
# ---------------------------------------------------------------------------


async def test_reasoning_content_only() -> None:
    events = [
        (None, make_openai_chunk(reasoning_content="step1 ")),
        (None, make_openai_chunk(reasoning_content="step2")),
        (None, make_openai_chunk(content="final", finish_reason="stop")),
    ]
    chunks = await _run(events)

    assert collect_reasoning_deltas(chunks) == "step1 step2"
    assert_message_done_equivalent(chunks, expected_content="final")

    # provider_metadata 含截断后的 reasoning_content + 完整长度
    done = chunks[-1]
    assert done.provider_metadata["reasoning_content"] == "step1 step2"
    assert done.provider_metadata["reasoning_content_length"] == len("step1 step2")


# ---------------------------------------------------------------------------
# A.10 — reasoning fallback：只有 reasoning 字段（Qwen 风格）
# ---------------------------------------------------------------------------


async def test_reasoning_fallback_only_reasoning_field() -> None:
    events = [
        (None, make_openai_chunk(reasoning="alpha")),
        (None, make_openai_chunk(reasoning="beta")),
        (None, make_openai_chunk(content="hi", finish_reason="stop")),
    ]
    chunks = await _run(events)

    # 只有 reasoning 字段时仍应正确累积
    assert collect_reasoning_deltas(chunks) == "alphabeta"


# ---------------------------------------------------------------------------
# A.11 — 混合顺序：reasoning → content 相互穿插
# ---------------------------------------------------------------------------


async def test_mixed_reasoning_and_content_interleaved() -> None:
    events = [
        (None, make_openai_chunk(reasoning_content="思考")),
        (None, make_openai_chunk(content="正")),
        (None, make_openai_chunk(reasoning_content="继续")),
        (None, make_openai_chunk(content="文")),
        (None, make_openai_chunk(content=None, finish_reason="stop")),
    ]
    chunks = await _run(events)

    # 两路累积互不影响
    assert collect_reasoning_deltas(chunks) == "思考继续"
    assert_message_done_equivalent(chunks, expected_content="正文")

    # 顺序保留：reasoning.delta / content.delta 按 chunk 到达顺序交错
    kinds = [c.kind for c in chunks if c.kind in ("reasoning.delta", "content.delta")]
    assert kinds == [
        "reasoning.delta",
        "content.delta",
        "reasoning.delta",
        "content.delta",
    ]


# ---------------------------------------------------------------------------
# A.12 / §2.2 — 双字段并存：只取 reasoning_content，不双倍累积
# ---------------------------------------------------------------------------


async def test_both_reasoning_fields_no_double_count() -> None:
    """关键语义：reasoning_content 与 reasoning 同时存在时只取前者。

    对齐 Hermes run_agent.py:4766 的 `reasoning_content or reasoning`。
    """
    events = [
        (
            None,
            make_openai_chunk(
                reasoning_content="A",
                reasoning="B",
            ),
        ),
        (None, make_openai_chunk(content="hi", finish_reason="stop")),
    ]
    chunks = await _run(events)

    # 只有 reasoning_content 被累积
    assert collect_reasoning_deltas(chunks) == "A"
    assert "B" not in collect_reasoning_deltas(chunks)

    # 3 条：1×reasoning.delta + 1×content.delta + 1×message.done
    assert len(chunks) == 3
    assert chunks[0].kind == "reasoning.delta"
    assert chunks[0].delta == "A"


# ---------------------------------------------------------------------------
# A.13 — 空 choices chunk（心跳 / usage-only）：只抽 model / usage
# ---------------------------------------------------------------------------


async def test_empty_choices_chunk_extracts_metadata() -> None:
    events = [
        (None, make_openai_chunk(content="hi", model="gpt-a")),
        (None, make_openai_chunk(content=None, finish_reason="stop", model="gpt-a")),
        # GLM / OpenAI：在最后一个 chunk 送 usage，choices 为空
        (
            None,
            make_openai_chunk(
                model="gpt-final",
                usage={
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                    "completion_tokens_details": {"reasoning_tokens": 5},
                    "prompt_tokens_details": {"cached_tokens": 3},
                },
            ),
        ),
    ]
    chunks = await _run(events)

    done = chunks[-1]
    # task#3.1：usage 字段透传 SDK 原生字段 + provider_kind；
    # cached_tokens / reasoning_tokens 从 details 子结构提到 usage 主体
    assert done.usage == {
        "provider_kind": "openai_compatible",
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "total_tokens": 12,
        "input_tokens": 10,
        "output_tokens": 2,
        "cached_input_tokens": 3,
        "reasoning_output_tokens": 5,
    }
    assert done.provider_metadata["model"] == "gpt-final"
    assert done.provider_metadata["reasoning_tokens"] == 5
    assert done.provider_metadata["cached_tokens"] == 3


# ---------------------------------------------------------------------------
# A.14 / §2.3 — 单 tool_call 聚合
# ---------------------------------------------------------------------------


async def test_single_tool_call() -> None:
    events = [
        # chunk 1: 首次出现 tool_call，带 id + name
        (
            None,
            make_openai_chunk(
                tool_calls=[make_tool_call_delta(index=0, id="call_abc", name="read_file")]
            ),
        ),
        # chunk 2: arguments 前半段
        (
            None,
            make_openai_chunk(tool_calls=[make_tool_call_delta(index=0, arguments='{"pa')]),
        ),
        # chunk 3: arguments 后半段
        (
            None,
            make_openai_chunk(
                tool_calls=[make_tool_call_delta(index=0, arguments='th":"/tmp/x.txt"}')]
            ),
        ),
        # chunk 4: finish
        (None, make_openai_chunk(content=None, finish_reason="tool_calls")),
    ]
    chunks = await _run(events)

    # 不变式 2：kind 序列顺序
    kinds = [c.kind for c in chunks]
    assert kinds == [
        "tool_call.start",
        "tool_call.arguments.delta",
        "tool_call.arguments.delta",
        "tool_call.end",
        "message.done",
    ]
    # 不变式 3：tool_call.start 只出现 1 次
    assert kinds.count("tool_call.start") == 1

    # 不变式 6：tool_call.end 不承载完整 arguments
    end_chunk = next(c for c in chunks if c.kind == "tool_call.end")
    assert end_chunk.delta == ""
    assert end_chunk.tool_name == "read_file"
    assert end_chunk.tool_call_id == "call_abc"
    assert end_chunk.index == 0

    # message.done.message.tool_calls 重建完整 tool_call
    done = chunks[-1]
    assert done.kind == "message.done"
    assert done.message is not None
    assert done.message.tool_calls is not None
    assert len(done.message.tool_calls) == 1
    tc = done.message.tool_calls[0]
    assert tc.call_id == "call_abc"
    assert tc.tool_name == "read_file"
    assert tc.arguments == {"path": "/tmp/x.txt"}
    assert done.finish_reason == "tool_calls"


# ---------------------------------------------------------------------------
# A.15 — tool_call.start 只发一次（即使后续 chunk 再次带 name 片段）
# ---------------------------------------------------------------------------


async def test_tool_call_start_not_duplicated() -> None:
    events = [
        (
            None,
            make_openai_chunk(
                tool_calls=[make_tool_call_delta(index=0, id="call_x", name="read_")]
            ),
        ),
        # name 继续追加 —— parser 不能再 yield 第二次 tool_call.start
        (
            None,
            make_openai_chunk(tool_calls=[make_tool_call_delta(index=0, name="file")]),
        ),
        (
            None,
            make_openai_chunk(tool_calls=[make_tool_call_delta(index=0, arguments='{"path":"/"}')]),
        ),
        (None, make_openai_chunk(content=None, finish_reason="tool_calls")),
    ]
    chunks = await _run(events)
    starts = [c for c in chunks if c.kind == "tool_call.start"]
    assert len(starts) == 1
    assert starts[0].tool_name == "read_"  # 首次触发时的片段（累积到 end 时为 "read_file"）
    done = chunks[-1]
    assert done.message is not None
    assert done.message.tool_calls is not None
    assert done.message.tool_calls[0].tool_name == "read_file"


# ---------------------------------------------------------------------------
# A.16 — arguments 等价性不变式
# ---------------------------------------------------------------------------


async def test_tool_call_arguments_equivalence() -> None:
    """不变式 4：对每个 tool_call，arguments 流片段拼接 == message.done 里
    tool_call.arguments 的 JSON 表达。"""
    events = [
        (
            None,
            make_openai_chunk(tool_calls=[make_tool_call_delta(index=0, id="c1", name="t")]),
        ),
        (
            None,
            make_openai_chunk(tool_calls=[make_tool_call_delta(index=0, arguments='{"k1":"v1"')]),
        ),
        (
            None,
            make_openai_chunk(tool_calls=[make_tool_call_delta(index=0, arguments=',"k2":2}')]),
        ),
        (None, make_openai_chunk(content=None, finish_reason="tool_calls")),
    ]
    chunks = await _run(events)

    # 拼接 arguments.delta
    concat = "".join(
        c.delta for c in chunks if c.kind == "tool_call.arguments.delta" and c.index == 0
    )
    assert concat == '{"k1":"v1","k2":2}'

    # message.done.tool_calls[0].arguments 是合法 JSON，内容等价
    done = chunks[-1]
    assert done.message is not None
    assert done.message.tool_calls is not None
    assert done.message.tool_calls[0].arguments == {"k1": "v1", "k2": 2}


# ---------------------------------------------------------------------------
# A.17 / §2.4 — Ollama index 复用防御（最重要）
# ---------------------------------------------------------------------------


async def test_ollama_index_reuse() -> None:
    """模拟 Ollama：两个并行 tool_call 都在 index=0，靠 id 区分。

    对齐 Hermes run_agent.py:4803-4816 的防御代码。
    """
    events = [
        # 第一个 tool_call：id=call_A
        (
            None,
            make_openai_chunk(
                tool_calls=[make_tool_call_delta(index=0, id="call_A", name="read_file")]
            ),
        ),
        (
            None,
            make_openai_chunk(
                tool_calls=[make_tool_call_delta(index=0, arguments='{"path":"/a.txt"}')]
            ),
        ),
        # 第二个 tool_call：id=call_B，还是 index=0（Ollama 不分 index）
        (
            None,
            make_openai_chunk(
                tool_calls=[make_tool_call_delta(index=0, id="call_B", name="list_dir")]
            ),
        ),
        (
            None,
            make_openai_chunk(
                tool_calls=[make_tool_call_delta(index=0, arguments='{"path":"/tmp"}')]
            ),
        ),
        (None, make_openai_chunk(content=None, finish_reason="tool_calls")),
    ]
    chunks = await _run(events)

    # 不变式 7：两个不同 id 在同一原始 index 下必须分出两条独立记录
    done = chunks[-1]
    assert done.kind == "message.done"
    assert done.message is not None
    assert done.message.tool_calls is not None
    assert len(done.message.tool_calls) == 2

    # 第一个 slot（原始 index=0）
    tc0 = done.message.tool_calls[0]
    assert tc0.call_id == "call_A"
    assert tc0.tool_name == "read_file"
    assert tc0.arguments == {"path": "/a.txt"}

    # 第二个 slot（parser 分到 new_slot）
    tc1 = done.message.tool_calls[1]
    assert tc1.call_id == "call_B"
    assert tc1.tool_name == "list_dir"
    assert tc1.arguments == {"path": "/tmp"}

    # 两条 tool_call.start 都出现了（不变式 3：每个 slot 只触发一次）
    starts = [c for c in chunks if c.kind == "tool_call.start"]
    assert len(starts) == 2
    assert {s.tool_call_id for s in starts} == {"call_A", "call_B"}

    # 两条 tool_call.end 分别属于不同 slot
    ends = [c for c in chunks if c.kind == "tool_call.end"]
    assert len(ends) == 2
    assert {e.tool_call_id for e in ends} == {"call_A", "call_B"}


# ---------------------------------------------------------------------------
# A.18 — 真正并行 tool_call（不同 index）
# ---------------------------------------------------------------------------


async def test_parallel_tool_calls_with_distinct_indices() -> None:
    """OpenAI 官方形态：两个 tool_call 分别在 index=0 / index=1。"""
    events = [
        (
            None,
            make_openai_chunk(
                tool_calls=[
                    make_tool_call_delta(index=0, id="call_0", name="read_file"),
                    make_tool_call_delta(index=1, id="call_1", name="list_dir"),
                ]
            ),
        ),
        (
            None,
            make_openai_chunk(
                tool_calls=[
                    make_tool_call_delta(index=0, arguments='{"path":"/a"}'),
                    make_tool_call_delta(index=1, arguments='{"path":"/b"}'),
                ]
            ),
        ),
        (None, make_openai_chunk(content=None, finish_reason="tool_calls")),
    ]
    chunks = await _run(events)

    done = chunks[-1]
    assert done.message is not None
    assert done.message.tool_calls is not None
    assert len(done.message.tool_calls) == 2

    by_id = {tc.call_id: tc for tc in done.message.tool_calls}
    assert by_id["call_0"].arguments == {"path": "/a"}
    assert by_id["call_1"].arguments == {"path": "/b"}


# ---------------------------------------------------------------------------
# A.19 / §2.5 — 截断降级：arguments 半截 → finish_reason="length"
# ---------------------------------------------------------------------------


async def test_truncated_args_marks_length() -> None:
    """模拟流在 tool_call arguments 中途被 max_tokens 截断。"""
    events = [
        (
            None,
            make_openai_chunk(
                tool_calls=[make_tool_call_delta(index=0, id="call_x", name="read_file")]
            ),
        ),
        # 只收到不完整 JSON：'{"path":"/tmp'
        (
            None,
            make_openai_chunk(
                tool_calls=[make_tool_call_delta(index=0, arguments='{"path":"/tmp')]
            ),
        ),
        (None, make_openai_chunk(content=None, finish_reason="tool_calls")),
    ]
    chunks = await _run(events)

    done = chunks[-1]
    assert done.kind == "message.done"

    # finish_reason 被降级为 length
    assert done.finish_reason == "length"

    # tool_call.end 仍被 yield（不丢 tool_call）
    assert any(c.kind == "tool_call.end" for c in chunks)

    # message.tool_calls 里的 arguments 存原始字符串（__raw__ 标记）
    assert done.message is not None
    assert done.message.tool_calls is not None
    tc = done.message.tool_calls[0]
    assert tc.arguments == {"__raw__": '{"path":"/tmp'}


# ---------------------------------------------------------------------------
# A.20 — extra_content：主字段优先
# ---------------------------------------------------------------------------


async def test_extra_content_primary_wins() -> None:
    events = [
        (
            None,
            make_openai_chunk(
                tool_calls=[
                    make_tool_call_delta(
                        index=0,
                        id="c1",
                        name="t",
                        arguments="{}",
                        extra_content={"source": "primary"},
                        model_extra={"extra_content": {"source": "fallback"}},
                    )
                ]
            ),
        ),
        (None, make_openai_chunk(content=None, finish_reason="tool_calls")),
    ]
    parser = OpenAICompatStreamParser()
    _ = [c async for c in parser.parse(async_iter_events(events))]
    # _PartialCall 是 parser 内部结构；验收 extra_content 处理正确以 parser state 反查
    assert parser._tool_calls_acc[0].extra_content == {"source": "primary"}  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# A.21 — extra_content：fallback model_extra["extra_content"]
# ---------------------------------------------------------------------------


async def test_extra_content_fallback_from_model_extra() -> None:
    events = [
        (
            None,
            make_openai_chunk(
                tool_calls=[
                    make_tool_call_delta(
                        index=0,
                        id="c1",
                        name="t",
                        arguments="{}",
                        model_extra={"extra_content": {"source": "fallback"}},
                    )
                ]
            ),
        ),
        (None, make_openai_chunk(content=None, finish_reason="tool_calls")),
    ]
    parser = OpenAICompatStreamParser()
    _ = [c async for c in parser.parse(async_iter_events(events))]
    assert parser._tool_calls_acc[0].extra_content == {"source": "fallback"}  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# A.22 — finish_reason 缺失时默认 "stop"
# ---------------------------------------------------------------------------


async def test_missing_finish_reason_defaults_to_stop() -> None:
    events = [
        (None, make_openai_chunk(content="hello")),
        # 没有 finish_reason 的收尾 chunk
        (None, make_openai_chunk(content=None)),
    ]
    chunks = await _run(events)
    assert_message_done_equivalent(chunks, expected_content="hello", expected_finish_reason="stop")


# ---------------------------------------------------------------------------
# 不变式 #5 — message.done 必须是最后一个 chunk
# ---------------------------------------------------------------------------


async def test_message_done_is_last_and_unique() -> None:
    events = [
        (None, make_openai_chunk(content="x", finish_reason="stop")),
    ]
    chunks = await _run(events)
    assert chunks[-1].kind == "message.done"
    assert sum(1 for c in chunks if c.kind == "message.done") == 1


# ---------------------------------------------------------------------------
# 不变式 #4 — *.delta kind 必须携带非空 delta
# ---------------------------------------------------------------------------


async def test_empty_delta_content_not_emitted() -> None:
    """delta.content 为空字符串时不 yield content.delta（保证不变式 #4）。"""
    events = [
        (None, make_openai_chunk(content="")),
        (None, make_openai_chunk(content="hi", finish_reason="stop")),
    ]
    chunks = await _run(events)
    content_chunks = [c for c in chunks if c.kind == "content.delta"]
    assert len(content_chunks) == 1
    assert content_chunks[0].delta == "hi"


async def test_empty_reasoning_not_emitted() -> None:
    events = [
        (None, make_openai_chunk(reasoning_content="")),
        (None, make_openai_chunk(content="hi", finish_reason="stop")),
    ]
    chunks = await _run(events)
    reasoning_chunks = [c for c in chunks if c.kind == "reasoning.delta"]
    assert reasoning_chunks == []
