"""Translator 单元测试。

不调真 SDK——直接构造 SDK 类型实例（dataclass）做夹具，``OutputWriter``
用 ``AsyncMock`` 替代以便断言。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from claude_sidecar.runtime import RunContext
from claude_sidecar.translator import (
    ClaudeMessageTranslator,
    SessionState,
    _normalize_usage,
)

# ---------- 测试夹具 ----------


@pytest.fixture
def context() -> RunContext:
    return RunContext(
        workspace_id="ws_test",
        thread_id="thr_test",
        run_id="run_test_1",
    )


@pytest.fixture
def output() -> AsyncMock:
    """mock OutputWriter—— ``emit`` 用 AsyncMock 让 ``await`` 能 work。"""
    mock = AsyncMock()
    mock.emit = AsyncMock()
    return mock


@pytest.fixture
def translator(output: AsyncMock) -> ClaudeMessageTranslator:
    return ClaudeMessageTranslator(output)


def emitted_events(output: AsyncMock) -> list[dict[str, Any]]:
    """读出 mock 上所有 emit 调用的参数。"""
    return [call.args[0] for call in output.emit.call_args_list]


# ---------- SessionState ----------


class TestSessionState:
    def test_default_fields(self) -> None:
        s = SessionState()
        assert s.last_session_id is None
        assert s.current_run_id is None
        assert s.pending_deny_request_ids == set()
        assert s.current_streaming_message_id is None

    def test_reset_for_new_run_clears_run_level_fields(self) -> None:
        s = SessionState(
            last_session_id="sid_keep",
            current_run_id="run_old",
            pending_deny_request_ids={"req_a", "req_b"},
            current_streaming_message_id="msg_old",
        )
        s.reset_for_new_run("run_new")
        assert s.current_run_id == "run_new"
        assert s.pending_deny_request_ids == set()
        assert s.current_streaming_message_id is None
        # 跨 run 字段保留
        assert s.last_session_id == "sid_keep"


# ---------- SystemMessage ----------


class TestSystemMessage:
    @pytest.mark.asyncio
    async def test_init_emits_session_created_and_updates_last_session_id(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        msg = SystemMessage(
            subtype="init",
            data={"session_id": "sess_abc123", "model": "claude-sonnet-4"},
        )
        await translator.handle(msg, context)

        events = emitted_events(output)
        assert len(events) == 1
        assert events[0]["type"] == "claude_session_created"
        assert events[0]["runtimeSessionId"] == "sess_abc123"
        assert events[0]["threadId"] == "thr_test"
        assert events[0]["runId"] == "run_test_1"
        assert translator.state.last_session_id == "sess_abc123"

    @pytest.mark.asyncio
    async def test_init_without_session_id_still_emits(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        # SDK 异常情况：data 没 session_id —— 照实输出 None 不静默吞
        msg = SystemMessage(subtype="init", data={})
        await translator.handle(msg, context)

        events = emitted_events(output)
        assert len(events) == 1
        assert events[0]["type"] == "claude_session_created"
        assert events[0]["runtimeSessionId"] is None
        assert translator.state.last_session_id is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "subtype",
        ["hook_started", "hook_response", "task_progress", "anything_else"],
    )
    async def test_non_init_subtypes_dropped(
        self,
        translator: ClaudeMessageTranslator,
        output: AsyncMock,
        context: RunContext,
        subtype: str,
    ) -> None:
        msg = SystemMessage(subtype=subtype, data={"session_id": "sess_x"})
        await translator.handle(msg, context)
        assert emitted_events(output) == []
        # 也不应触碰 last_session_id
        assert translator.state.last_session_id is None

    @pytest.mark.asyncio
    async def test_task_started_message_dropped(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        # TaskStartedMessage 是 SystemMessage 子类（dataclass 继承），
        # subtype='task_started' → 走 _handle_system 然后被丢弃
        msg = TaskStartedMessage(
            subtype="task_started",
            data={},
            task_id="task_1",
            description="probe task",
            uuid="uuid_t",
            session_id="sess_t",
        )
        await translator.handle(msg, context)
        assert emitted_events(output) == []

    @pytest.mark.asyncio
    async def test_task_progress_message_dropped(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        msg = TaskProgressMessage(
            subtype="task_progress",
            data={},
            task_id="task_1",
            description="probe",
            usage={"total_tokens": 1, "tool_uses": 0, "duration_ms": 1},
            uuid="u",
            session_id="s",
        )
        await translator.handle(msg, context)
        assert emitted_events(output) == []


# ---------- AssistantMessage ----------


class TestAssistantMessage:
    @pytest.mark.asyncio
    async def test_text_block_emits_assistant_final(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        msg = AssistantMessage(
            content=[TextBlock(text="Hello world")],
            model="claude-sonnet-4",
            message_id="msg_01ABCDEF",
        )
        await translator.handle(msg, context)

        events = emitted_events(output)
        assert len(events) == 1
        assert events[0]["type"] == "claude_assistant_final"
        # 验证 responseId 取自 message_id，不是 StreamEvent.uuid
        assert events[0]["responseId"] == "msg_01ABCDEF"
        assert events[0]["content"] == "Hello world"

    @pytest.mark.asyncio
    async def test_thinking_block_emits_thinking_final(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        msg = AssistantMessage(
            content=[ThinkingBlock(thinking="reasoning text", signature="sig")],
            model="claude-sonnet-4",
            message_id="msg_thinking",
        )
        await translator.handle(msg, context)

        events = emitted_events(output)
        assert len(events) == 1
        assert events[0]["type"] == "claude_assistant_thinking_final"
        assert events[0]["responseId"] == "msg_thinking"
        assert events[0]["content"] == "reasoning text"

    @pytest.mark.asyncio
    async def test_empty_thinking_block_emits_empty_string(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        # smoke §2.3：模型可能产生空 ThinkingBlock，content='' 照实输出
        msg = AssistantMessage(
            content=[ThinkingBlock(thinking="", signature="sig_only")],
            model="claude-sonnet-4",
            message_id="msg_empty_think",
        )
        await translator.handle(msg, context)

        events = emitted_events(output)
        assert len(events) == 1
        assert events[0]["type"] == "claude_assistant_thinking_final"
        assert events[0]["content"] == ""

    @pytest.mark.asyncio
    async def test_tool_use_block_emits_tool_started(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        msg = AssistantMessage(
            content=[
                ToolUseBlock(
                    id="toolu_01XYZ",
                    name="Read",
                    input={"file_path": "/tmp/foo.txt"},
                )
            ],
            model="claude-sonnet-4",
            message_id="msg_tool",
        )
        await translator.handle(msg, context)

        events = emitted_events(output)
        assert len(events) == 1
        assert events[0]["type"] == "claude_tool_started"
        assert events[0]["toolCallId"] == "toolu_01XYZ"
        assert events[0]["toolName"] == "Read"
        assert events[0]["input"] == {"file_path": "/tmp/foo.txt"}

    @pytest.mark.asyncio
    async def test_assistant_message_updates_streaming_message_id(
        self, translator: ClaudeMessageTranslator, context: RunContext
    ) -> None:
        msg = AssistantMessage(
            content=[TextBlock(text="anchor")],
            model="claude-sonnet-4",
            message_id="msg_anchor",
        )
        await translator.handle(msg, context)
        assert translator.state.current_streaming_message_id == "msg_anchor"

    @pytest.mark.asyncio
    async def test_multiple_blocks_each_emit(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        # 防御性：SDK 类型允许多 block 数组，每个都翻译
        msg = AssistantMessage(
            content=[
                ThinkingBlock(thinking="thought", signature="sig"),
                TextBlock(text="text"),
                ToolUseBlock(id="t1", name="Read", input={}),
            ],
            model="claude-sonnet-4",
            message_id="msg_multi",
        )
        await translator.handle(msg, context)

        events = emitted_events(output)
        assert len(events) == 3
        assert events[0]["type"] == "claude_assistant_thinking_final"
        assert events[1]["type"] == "claude_assistant_final"
        assert events[2]["type"] == "claude_tool_started"
        # 三个事件共享同一个 responseId（message_id）
        assert events[0]["responseId"] == "msg_multi"
        assert events[1]["responseId"] == "msg_multi"

    @pytest.mark.asyncio
    async def test_empty_content_list_emits_nothing(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        msg = AssistantMessage(
            content=[],
            model="claude-sonnet-4",
            message_id="msg_empty",
        )
        await translator.handle(msg, context)
        assert emitted_events(output) == []


# ---------- StreamEvent ----------


class TestStreamEvent:
    @pytest.mark.asyncio
    async def test_text_delta_emits_assistant_delta(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        # 先喂一条 AssistantMessage 锚定 responseId
        translator.state.current_streaming_message_id = "msg_streaming"

        ev = StreamEvent(
            uuid="uuid_se_1",
            session_id="sess_x",
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hel"},
            },
        )
        await translator.handle(ev, context)

        events = emitted_events(output)
        assert len(events) == 1
        assert events[0]["type"] == "claude_assistant_delta"
        # responseId 必须是 message_id 不是 StreamEvent.uuid
        assert events[0]["responseId"] == "msg_streaming"
        assert events[0]["responseId"] != "uuid_se_1"
        assert events[0]["delta"] == "Hel"

    @pytest.mark.asyncio
    async def test_thinking_delta_emits_thinking_delta(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        translator.state.current_streaming_message_id = "msg_t"
        ev = StreamEvent(
            uuid="uuid",
            session_id="s",
            event={
                "type": "content_block_delta",
                "delta": {"type": "thinking_delta", "thinking": "...thoughts..."},
            },
        )
        await translator.handle(ev, context)

        events = emitted_events(output)
        assert len(events) == 1
        assert events[0]["type"] == "claude_assistant_thinking_delta"
        assert events[0]["delta"] == "...thoughts..."

    @pytest.mark.asyncio
    async def test_delta_aggregation_shares_response_id(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        # 模拟一个完整 turn：先 partial AssistantMessage 锚定 message_id，
        # 再 3 条 StreamEvent 增量；3 条 delta 都应共享同一 responseId
        anchor = AssistantMessage(
            content=[TextBlock(text="")],  # partial 阶段 content 可能空
            model="claude-sonnet-4",
            message_id="msg_turn_42",
        )
        await translator.handle(anchor, context)

        for chunk in ["Hel", "lo, ", "world"]:
            ev = StreamEvent(
                uuid=f"uuid_{chunk}",
                session_id="s",
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": chunk},
                },
            )
            await translator.handle(ev, context)

        events = emitted_events(output)
        # 1 个 final（来自 anchor）+ 3 个 delta
        delta_events = [e for e in events if e["type"] == "claude_assistant_delta"]
        assert len(delta_events) == 3
        for e in delta_events:
            assert e["responseId"] == "msg_turn_42"

    @pytest.mark.asyncio
    async def test_stream_event_without_anchor_falls_back_to_unknown(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        # 没先发 AssistantMessage —— 降级 'unknown'，不丢失事件
        ev = StreamEvent(
            uuid="uuid",
            session_id="s",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "x"},
            },
        )
        await translator.handle(ev, context)

        events = emitted_events(output)
        assert len(events) == 1
        assert events[0]["responseId"] == "unknown"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "event_type",
        [
            "message_start",
            "content_block_start",
            "content_block_stop",
            "message_delta",
            "message_stop",
            "ping",
        ],
    )
    async def test_non_content_block_delta_dropped(
        self,
        translator: ClaudeMessageTranslator,
        output: AsyncMock,
        context: RunContext,
        event_type: str,
    ) -> None:
        ev = StreamEvent(
            uuid="u",
            session_id="s",
            event={"type": event_type},
        )
        await translator.handle(ev, context)
        assert emitted_events(output) == []

    @pytest.mark.asyncio
    async def test_signature_delta_dropped(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        translator.state.current_streaming_message_id = "msg"
        ev = StreamEvent(
            uuid="u",
            session_id="s",
            event={
                "type": "content_block_delta",
                "delta": {"type": "signature_delta", "signature": "abc"},
            },
        )
        await translator.handle(ev, context)
        assert emitted_events(output) == []


# ---------- UserMessage / Tool result + deny 去重 ----------


class TestUserMessage:
    @pytest.mark.asyncio
    async def test_tool_result_emits_tool_finished(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        msg = UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="toolu_01ABC",
                    content="file contents",
                    is_error=False,
                )
            ]
        )
        await translator.handle(msg, context)

        events = emitted_events(output)
        assert len(events) == 1
        assert events[0]["type"] == "claude_tool_finished"
        assert events[0]["toolCallId"] == "toolu_01ABC"
        assert events[0]["content"] == "file contents"
        assert events[0]["isError"] is False

    @pytest.mark.asyncio
    async def test_tool_result_omits_optional_fields_when_none(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        msg = UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="toolu_01",
                    content=None,
                    is_error=None,
                )
            ]
        )
        await translator.handle(msg, context)

        events = emitted_events(output)
        assert len(events) == 1
        assert events[0]["type"] == "claude_tool_finished"
        assert events[0]["toolCallId"] == "toolu_01"
        # 缺省时不写字段（不写 None）
        assert "content" not in events[0]
        assert "isError" not in events[0]

    @pytest.mark.asyncio
    async def test_tool_result_with_list_content_passthrough(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        # 契约 §7.9 允许 content 是 list[dict]（如 tool_reference）
        list_content = [{"type": "tool_reference", "ref": "abc"}]
        msg = UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="t1",
                    content=list_content,
                    is_error=False,
                )
            ]
        )
        await translator.handle(msg, context)

        events = emitted_events(output)
        assert events[0]["content"] == list_content

    @pytest.mark.asyncio
    async def test_str_content_user_message_dropped(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        # 用户原文输入（非 tool result），sidecar 不回吐
        msg = UserMessage(content="hello, claude")
        await translator.handle(msg, context)
        assert emitted_events(output) == []


class TestDenyDeduplication:
    @pytest.mark.asyncio
    async def test_denied_tool_result_is_dropped(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        # 模拟 deny 流程：先 add_pending_deny，再喂 ToolResultBlock
        translator.add_pending_deny("toolu_denied")

        msg = UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="toolu_denied",
                    content="for smoke",
                    is_error=True,
                )
            ]
        )
        await translator.handle(msg, context)

        # 不应输出 claude_tool_finished
        assert emitted_events(output) == []
        # discard 后集合清空
        assert "toolu_denied" not in translator.state.pending_deny_request_ids

    @pytest.mark.asyncio
    async def test_non_denied_tool_result_passes_through(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        # deny 集合里有别的 id，不该影响当前 ToolResult
        translator.add_pending_deny("toolu_other")

        msg = UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="toolu_legit",
                    content="legit result",
                    is_error=False,
                )
            ]
        )
        await translator.handle(msg, context)

        events = emitted_events(output)
        assert len(events) == 1
        assert events[0]["type"] == "claude_tool_finished"
        # 别的 deny id 不变
        assert "toolu_other" in translator.state.pending_deny_request_ids

    @pytest.mark.asyncio
    async def test_reset_for_new_run_clears_pending_deny(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        # 上一个 run 留下 pending deny
        translator.add_pending_deny("toolu_old_deny")
        assert "toolu_old_deny" in translator.state.pending_deny_request_ids

        # 新 run 开始
        new_ctx = RunContext(
            workspace_id="ws_test",
            thread_id="thr_test",
            run_id="run_test_2",
        )
        translator.reset_for_new_run(new_ctx)

        # 集合清空，新 run 收到 ToolResult 正常输出
        assert translator.state.pending_deny_request_ids == set()
        assert translator.state.current_run_id == "run_test_2"

        msg = UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="toolu_old_deny",
                    content="should not be dropped now",
                    is_error=False,
                )
            ]
        )
        await translator.handle(msg, new_ctx)
        events = emitted_events(output)
        assert len(events) == 1
        assert events[0]["type"] == "claude_tool_finished"

    @pytest.mark.asyncio
    async def test_reset_preserves_last_session_id(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        init = SystemMessage(subtype="init", data={"session_id": "sess_persist"})
        await translator.handle(init, context)
        assert translator.state.last_session_id == "sess_persist"

        new_ctx = RunContext(
            workspace_id="ws_test",
            thread_id="thr_test",
            run_id="run_test_2",
        )
        translator.reset_for_new_run(new_ctx)
        # last_session_id 跨 run 保留
        assert translator.state.last_session_id == "sess_persist"


# ---------- ResultMessage 终态分类 ----------


class TestResultMessage:
    @pytest.mark.asyncio
    async def test_finished_normal(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        msg = ResultMessage(
            subtype="success",
            duration_ms=1234,
            duration_api_ms=900,
            is_error=False,
            num_turns=1,
            session_id="s",
            stop_reason="end_turn",
        )
        await translator.handle(msg, context)

        events = emitted_events(output)
        assert len(events) == 1
        assert events[0]["type"] == "claude_run_finished"
        assert events[0]["durationMs"] == 1234
        assert events[0]["durationApiMs"] == 900

    @pytest.mark.asyncio
    async def test_finished_with_usage_snake_case_converted(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        msg = ResultMessage(
            subtype="success",
            duration_ms=100,
            duration_api_ms=50,
            is_error=False,
            num_turns=1,
            session_id="s",
            stop_reason="end_turn",
            usage={
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_read_input_tokens": 500,
                "cache_creation_input_tokens": 50,
            },
            total_cost_usd=0.012,
        )
        await translator.handle(msg, context)

        events = emitted_events(output)
        assert len(events) == 1
        usage = events[0]["usage"]
        assert usage["inputTokens"] == 1000
        assert usage["outputTokens"] == 200
        assert usage["cacheReadTokens"] == 500
        assert usage["cacheCreationTokens"] == 50
        assert usage["totalCostUsd"] == 0.012

    @pytest.mark.asyncio
    async def test_finished_with_already_camel_case_usage_passthrough(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        msg = ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s",
            stop_reason="end_turn",
            usage={"inputTokens": 42, "outputTokens": 7},
        )
        await translator.handle(msg, context)
        usage = emitted_events(output)[0]["usage"]
        assert usage == {"inputTokens": 42, "outputTokens": 7}

    @pytest.mark.asyncio
    async def test_failed_when_is_error_true(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        msg = ResultMessage(
            subtype="error_max_turns",
            duration_ms=100,
            duration_api_ms=50,
            is_error=True,
            num_turns=2,
            session_id="s",
            stop_reason="end_turn",
            errors=["Reached maximum number of turns (2)"],
        )
        await translator.handle(msg, context)

        events = emitted_events(output)
        assert len(events) == 1
        assert events[0]["type"] == "claude_run_failed"
        assert events[0]["message"] == "Reached maximum number of turns (2)"

    @pytest.mark.asyncio
    async def test_failed_with_multiple_errors_joined(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        msg = ResultMessage(
            subtype="error_during_execution",
            duration_ms=10,
            duration_api_ms=5,
            is_error=True,
            num_turns=1,
            session_id="s",
            errors=["error one", "error two"],
        )
        await translator.handle(msg, context)
        assert emitted_events(output)[0]["message"] == "error one / error two"

    @pytest.mark.asyncio
    async def test_failed_no_errors_falls_back_to_subtype(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        msg = ResultMessage(
            subtype="weird_subtype",
            duration_ms=10,
            duration_api_ms=5,
            is_error=True,
            num_turns=1,
            session_id="s",
        )
        await translator.handle(msg, context)
        assert emitted_events(output)[0]["message"] == "weird_subtype"

    @pytest.mark.asyncio
    async def test_interrupted_when_stop_reason_is_interrupted(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        msg = ResultMessage(
            subtype="interrupted",
            duration_ms=100,
            duration_api_ms=50,
            is_error=False,
            num_turns=1,
            session_id="s",
            stop_reason="interrupted",
        )
        await translator.handle(msg, context)

        events = emitted_events(output)
        assert len(events) == 1
        assert events[0]["type"] == "claude_run_interrupted"
        assert events[0]["reason"] == "interrupted"

    @pytest.mark.asyncio
    async def test_is_error_takes_priority_over_interrupted(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        # 边界：is_error=True 同时 stop_reason='interrupted' —— 走 failed 分支
        msg = ResultMessage(
            subtype="interrupted",
            duration_ms=10,
            duration_api_ms=5,
            is_error=True,
            num_turns=1,
            session_id="s",
            stop_reason="interrupted",
            errors=["something blew up"],
        )
        await translator.handle(msg, context)
        events = emitted_events(output)
        assert events[0]["type"] == "claude_run_failed"

    @pytest.mark.asyncio
    async def test_finished_without_usage_omits_field(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        msg = ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=5,
            is_error=False,
            num_turns=1,
            session_id="s",
            stop_reason="end_turn",
            usage=None,
            total_cost_usd=None,
        )
        await translator.handle(msg, context)

        events = emitted_events(output)
        assert "usage" not in events[0]


# ---------- _normalize_usage 单元 ----------


class TestNormalizeUsage:
    def test_empty_returns_empty(self) -> None:
        assert _normalize_usage(None) == {}
        assert _normalize_usage({}) == {}

    def test_snake_case_to_camel(self) -> None:
        out = _normalize_usage(
            {
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 4,
            }
        )
        assert out == {
            "inputTokens": 1,
            "outputTokens": 2,
            "cacheReadTokens": 3,
            "cacheCreationTokens": 4,
        }

    def test_alt_cache_keys(self) -> None:
        # SDK 偶尔可能用不带 _input_ 中段的字段名
        out = _normalize_usage(
            {
                "cache_read_tokens": 10,
                "cache_creation_tokens": 20,
            }
        )
        assert out == {"cacheReadTokens": 10, "cacheCreationTokens": 20}

    def test_unknown_keys_passthrough(self) -> None:
        # SDK 升级新增字段不破：未知字段透传
        out = _normalize_usage({"newFutureField": 99, "input_tokens": 1})
        assert out == {"newFutureField": 99, "inputTokens": 1}

    def test_total_cost_usd_injected(self) -> None:
        out = _normalize_usage({"input_tokens": 1}, total_cost_usd=0.5)
        assert out["totalCostUsd"] == 0.5

    def test_camel_existing_not_overridden_by_snake(self) -> None:
        # 同一逻辑 key 同时有 camel 和 snake 时，camel（已存在）优先
        out = _normalize_usage({"inputTokens": 100, "input_tokens": 999})
        assert out["inputTokens"] == 100


# ---------- handle 类型分发 ----------


class TestHandleDispatch:
    @pytest.mark.asyncio
    async def test_unknown_type_silently_ignored(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        # 不在 5 类内的 SDK 对象（RateLimitEvent 或自定义对象）应该静默忽略
        class FakeMessage:
            pass

        await translator.handle(FakeMessage(), context)
        assert emitted_events(output) == []

    @pytest.mark.asyncio
    async def test_emit_always_includes_thread_and_run_id(
        self, translator: ClaudeMessageTranslator, output: AsyncMock, context: RunContext
    ) -> None:
        msg = SystemMessage(subtype="init", data={"session_id": "s"})
        await translator.handle(msg, context)
        ev = emitted_events(output)[0]
        assert ev["threadId"] == context.thread_id
        assert ev["runId"] == context.run_id
