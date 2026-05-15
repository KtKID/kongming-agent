"""ClaudeNormalizer 单测：覆盖 5 类 SDK message 翻译路径。"""

from __future__ import annotations

from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from web.claude_code.normalizer import INTERNAL_CONTENT_PREFIXES, ClaudeNormalizer

SESSION_ID = "sess-abc"


# ---------------------------------------------------------------------------
# SystemMessage
# ---------------------------------------------------------------------------


def test_system_init_translates_to_session_created() -> None:
    n = ClaudeNormalizer()
    msg = SystemMessage(subtype="init", data={"session_id": "sdk-sid-1", "model": "sonnet"})
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    assert out[0]["kind"] == "session_created"
    assert out[0]["newSessionId"] == "sdk-sid-1"
    assert out[0]["sessionId"] == "sdk-sid-1"
    assert out[0]["provider"] == "claude"
    assert "id" in out[0]
    assert "timestamp" in out[0]


def test_system_hook_started_is_dropped() -> None:
    n = ClaudeNormalizer()
    msg = SystemMessage(subtype="hook_started", data={"session_id": "x"})
    assert n.normalize(msg, SESSION_ID) == []


def test_system_task_progress_is_dropped() -> None:
    n = ClaudeNormalizer()
    msg = SystemMessage(subtype="task_progress", data={"name": "x"})
    assert n.normalize(msg, SESSION_ID) == []


# ---------------------------------------------------------------------------
# AssistantMessage
# ---------------------------------------------------------------------------


def _make_assistant(blocks: list, message_id: str = "msg-1") -> AssistantMessage:
    return AssistantMessage(
        content=blocks,
        model="claude-sonnet",
        parent_tool_use_id=None,
        message_id=message_id,
    )


def test_assistant_text_block_translates_to_text() -> None:
    n = ClaudeNormalizer()
    msg = _make_assistant([TextBlock(text="hello world")])
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    assert out[0]["kind"] == "text"
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == "hello world"


def test_assistant_thinking_block_translates_to_thinking() -> None:
    n = ClaudeNormalizer()
    msg = _make_assistant([ThinkingBlock(thinking="hmm", signature="sig")])
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    assert out[0]["kind"] == "thinking"
    assert out[0]["content"] == "hmm"


def test_assistant_empty_thinking_block_still_emits() -> None:
    """smoke §2.3：模型可能产生空 ThinkingBlock，照实输出。"""
    n = ClaudeNormalizer()
    msg = _make_assistant([ThinkingBlock(thinking="", signature="sig")])
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    assert out[0]["kind"] == "thinking"
    assert out[0]["content"] == ""


def test_assistant_tool_use_block_translates() -> None:
    n = ClaudeNormalizer()
    msg = _make_assistant(
        [ToolUseBlock(id="toolu_xyz", name="Read", input={"path": "/tmp"})],
    )
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    assert out[0]["kind"] == "tool_use"
    assert out[0]["toolId"] == "toolu_xyz"
    assert out[0]["toolName"] == "Read"
    assert out[0]["toolInput"] == {"path": "/tmp"}


def test_assistant_internal_content_is_filtered() -> None:
    n = ClaudeNormalizer()
    for prefix in INTERNAL_CONTENT_PREFIXES:
        msg = _make_assistant([TextBlock(text=f"{prefix} blah blah")])
        out = n.normalize(msg, SESSION_ID)
        assert out == [], f"prefix {prefix!r} should be filtered"


def test_assistant_multiple_blocks_emits_multiple_messages() -> None:
    n = ClaudeNormalizer()
    msg = _make_assistant(
        [
            TextBlock(text="hi"),
            ToolUseBlock(id="t1", name="Read", input={}),
        ],
    )
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 2
    assert out[0]["kind"] == "text"
    assert out[1]["kind"] == "tool_use"


# ---------------------------------------------------------------------------
# UserMessage / ToolResultBlock + deny 去重
# ---------------------------------------------------------------------------


def test_user_tool_result_translates() -> None:
    n = ClaudeNormalizer()
    msg = UserMessage(
        content=[
            ToolResultBlock(
                tool_use_id="toolu_xyz",
                content="some output",
                is_error=False,
            ),
        ],
    )
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    assert out[0]["kind"] == "tool_result"
    assert out[0]["toolId"] == "toolu_xyz"
    assert out[0]["content"] == "some output"
    assert out[0]["isError"] is False


def test_user_tool_result_after_deny_is_dropped() -> None:
    """E8：deny 后 SDK 自动塞的 ToolResult 必须丢弃。"""
    n = ClaudeNormalizer()
    n.add_pending_deny("toolu_denied")
    msg = UserMessage(
        content=[
            ToolResultBlock(
                tool_use_id="toolu_denied",
                content="for smoke",
                is_error=True,
            ),
        ],
    )
    out = n.normalize(msg, SESSION_ID)
    assert out == []
    # discard 后再来同 id 不应再被抑制（防御性测试 —— 实际 SDK 不会重发）
    msg2 = UserMessage(
        content=[
            ToolResultBlock(
                tool_use_id="toolu_denied",
                content="ok",
                is_error=False,
            ),
        ],
    )
    out2 = n.normalize(msg2, SESSION_ID)
    assert len(out2) == 1


def test_user_str_content_is_dropped() -> None:
    """UserMessage.content 是字符串（用户原文输入），不回吐。"""
    n = ClaudeNormalizer()
    msg = UserMessage(content="hello")
    assert n.normalize(msg, SESSION_ID) == []


# ---------------------------------------------------------------------------
# StreamEvent
# ---------------------------------------------------------------------------


def test_stream_event_text_delta_translates() -> None:
    n = ClaudeNormalizer()
    msg = StreamEvent(
        uuid="uuid-1",
        session_id="sid",
        event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Hel"},
        },
        parent_tool_use_id=None,
    )
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    assert out[0]["kind"] == "stream_delta"
    assert out[0]["content"] == "Hel"
    assert out[0]["deltaType"] == "text"


def test_stream_event_thinking_delta_translates() -> None:
    n = ClaudeNormalizer()
    msg = StreamEvent(
        uuid="uuid-2",
        session_id="sid",
        event={
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": "rea"},
        },
        parent_tool_use_id=None,
    )
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    assert out[0]["kind"] == "stream_delta"
    assert out[0]["content"] == "rea"
    assert out[0]["deltaType"] == "thinking"


def test_stream_event_input_json_delta_translates() -> None:
    """v0.2 stream-progress：``input_json_delta`` 不再丢弃，产出
    ``stream_delta(deltaType="input_json")``——前端用于增量渲染工具入参。
    """
    n = ClaudeNormalizer()
    msg = StreamEvent(
        uuid="uuid-input-json",
        session_id="sid",
        event={
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
        },
        parent_tool_use_id=None,
    )
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    assert out[0]["kind"] == "stream_delta"
    assert out[0]["content"] == '{"path":'
    assert out[0]["deltaType"] == "input_json"


def test_stream_event_signature_delta_dropped() -> None:
    n = ClaudeNormalizer()
    msg = StreamEvent(
        uuid="uuid-3",
        session_id="sid",
        event={
            "type": "content_block_delta",
            "delta": {"type": "signature_delta", "signature": "..."},
        },
        parent_tool_use_id=None,
    )
    assert n.normalize(msg, SESSION_ID) == []


def test_stream_event_block_stop_emits_stream_end() -> None:
    n = ClaudeNormalizer()
    msg = StreamEvent(
        uuid="uuid-4",
        session_id="sid",
        event={"type": "content_block_stop", "index": 0},
        parent_tool_use_id=None,
    )
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    assert out[0]["kind"] == "stream_end"


def test_stream_event_message_start_emits_status() -> None:
    """v0.2 stream-progress：``message_start`` 从丢弃改为产出
    ``stream_status(phase="responding", model=...)``——首 token 前给前端开始信号。
    """
    n = ClaudeNormalizer()
    msg = StreamEvent(
        uuid="uuid-5",
        session_id="sid",
        event={
            "type": "message_start",
            "message": {"id": "msg_x", "model": "claude-sonnet-4-6"},
        },
        parent_tool_use_id=None,
    )
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    assert out[0]["kind"] == "stream_status"
    assert out[0]["phase"] == "responding"
    assert out[0]["model"] == "claude-sonnet-4-6"


def test_stream_event_message_start_without_model_omits_model_field() -> None:
    """``message_start.message.model`` 缺失时不携带 ``model`` 字段（不产 None）。"""
    n = ClaudeNormalizer()
    msg = StreamEvent(
        uuid="uuid-no-model",
        session_id="sid",
        event={"type": "message_start", "message": {}},
        parent_tool_use_id=None,
    )
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    assert out[0]["kind"] == "stream_status"
    assert out[0]["phase"] == "responding"
    assert "model" not in out[0]


def test_stream_event_block_start_thinking_emits_status() -> None:
    """``content_block_start(thinking)`` → ``stream_status(phase="thinking")``。"""
    n = ClaudeNormalizer()
    msg = StreamEvent(
        uuid="uuid-blk-think",
        session_id="sid",
        event={
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking"},
        },
        parent_tool_use_id=None,
    )
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    assert out[0]["kind"] == "stream_status"
    assert out[0]["phase"] == "thinking"
    assert out[0]["blockIndex"] == 0


def test_stream_event_block_start_text_emits_status() -> None:
    """``content_block_start(text)`` → ``stream_status(phase="responding")``。"""
    n = ClaudeNormalizer()
    msg = StreamEvent(
        uuid="uuid-blk-text",
        session_id="sid",
        event={
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "text"},
        },
        parent_tool_use_id=None,
    )
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    assert out[0]["kind"] == "stream_status"
    assert out[0]["phase"] == "responding"
    assert out[0]["blockIndex"] == 1


def test_stream_event_block_start_tool_use_emits_status_with_tool_name() -> None:
    """``content_block_start(tool_use)`` → ``stream_status(phase="tool_calling", toolName, toolId)``。"""
    n = ClaudeNormalizer()
    msg = StreamEvent(
        uuid="uuid-blk-tool",
        session_id="sid",
        event={
            "type": "content_block_start",
            "index": 2,
            "content_block": {
                "type": "tool_use",
                "id": "tu_1",
                "name": "Read",
                "input": {},
            },
        },
        parent_tool_use_id=None,
    )
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    assert out[0]["kind"] == "stream_status"
    assert out[0]["phase"] == "tool_calling"
    assert out[0]["blockIndex"] == 2
    assert out[0]["toolName"] == "Read"
    assert out[0]["toolId"] == "tu_1"


def test_stream_event_block_start_tool_use_missing_id_omits_tool_id() -> None:
    """``content_block.id`` 缺失时 ``toolId`` 字段不写入（让前端走兜底匹配）。"""
    n = ClaudeNormalizer()
    msg = StreamEvent(
        uuid="uuid-blk-tool-no-id",
        session_id="sid",
        event={
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "name": "Read",
                "input": {},
            },
        },
        parent_tool_use_id=None,
    )
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    assert out[0]["kind"] == "stream_status"
    assert out[0]["phase"] == "tool_calling"
    assert out[0]["toolName"] == "Read"
    assert "toolId" not in out[0]


def test_stream_event_block_start_server_tool_use_carries_tool_id() -> None:
    """``server_tool_use`` 同样产出 ``stream_status(phase=tool_calling, toolId)``。"""
    n = ClaudeNormalizer()
    msg = StreamEvent(
        uuid="uuid-blk-srv",
        session_id="sid",
        event={
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "server_tool_use",
                "id": "stu_42",
                "name": "WebSearch",
                "input": {},
            },
        },
        parent_tool_use_id=None,
    )
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    assert out[0]["kind"] == "stream_status"
    assert out[0]["phase"] == "tool_calling"
    assert out[0]["toolName"] == "WebSearch"
    assert out[0]["toolId"] == "stu_42"


def test_stream_event_block_start_unknown_type_dropped() -> None:
    """未知 ``content_block.type`` 保守降级为不产出（避免发未识别 phase 给前端）。"""
    n = ClaudeNormalizer()
    msg = StreamEvent(
        uuid="uuid-blk-unk",
        session_id="sid",
        event={
            "type": "content_block_start",
            "index": 3,
            "content_block": {"type": "future_block_type"},
        },
        parent_tool_use_id=None,
    )
    assert n.normalize(msg, SESSION_ID) == []


# ---------------------------------------------------------------------------
# ResultMessage
# ---------------------------------------------------------------------------


def test_result_success_translates_to_complete() -> None:
    n = ClaudeNormalizer()
    msg = ResultMessage(
        subtype="success",
        duration_ms=1234,
        duration_api_ms=1000,
        is_error=False,
        num_turns=2,
        session_id="sid",
        stop_reason="end_turn",
        total_cost_usd=0.0023,
        usage={"input_tokens": 10, "output_tokens": 5},
        result="ok",
    )
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    assert out[0]["kind"] == "complete"
    assert out[0]["exitCode"] == 0
    assert out[0]["durationMs"] == 1234
    assert out[0]["tokenBudget"]["inputTokens"] == 10
    assert out[0]["tokenBudget"]["outputTokens"] == 5
    assert out[0]["tokenBudget"]["totalCostUsd"] == 0.0023


def test_result_unknown_snake_keys_auto_camelized() -> None:
    """e2e smoke 暴露的 Bug 2：SDK 实际 usage 含 ``server_tool_use`` /
    ``service_tier`` / ``cache_creation`` 等未在 key_map 里的 snake_case 字段，
    normalizer 应自动转 camelCase 而不是原样保留——避免出站消息字段命名混搭。
    """
    n = ClaudeNormalizer()
    msg = ResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=80,
        is_error=False,
        num_turns=1,
        session_id="sid",
        stop_reason="end_turn",
        total_cost_usd=0.01,
        usage={
            "input_tokens": 6,
            "output_tokens": 14,
            "server_tool_use": {"web_search_requests": 0},
            "service_tier": "standard",
            "cache_creation": {"ephemeral_5m_input_tokens": 0},
            "inference_geo": "",
        },
    )
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    tb = out[0]["tokenBudget"]
    # 已知字段：通过 key_map
    assert tb["inputTokens"] == 6
    assert tb["outputTokens"] == 14
    # 未知 snake_case 字段：自动 camel
    assert "serverToolUse" in tb
    assert "serviceTier" in tb
    assert "cacheCreation" in tb
    assert "inferenceGeo" in tb
    # snake_case 残留不应再出现
    assert "server_tool_use" not in tb
    assert "service_tier" not in tb
    assert "cache_creation" not in tb
    assert "inference_geo" not in tb


def test_result_error_translates_with_exit_code_1() -> None:
    n = ClaudeNormalizer()
    msg = ResultMessage(
        subtype="error_max_turns",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=True,
        num_turns=2,
        session_id="sid",
        stop_reason="end_turn",
        errors=["Reached maximum number of turns"],
    )
    out = n.normalize(msg, SESSION_ID)
    assert len(out) == 1
    assert out[0]["kind"] == "complete"
    assert out[0]["exitCode"] == 1
    assert "Reached maximum" in out[0]["error"]


# ---------------------------------------------------------------------------
# 其他类型 / reset
# ---------------------------------------------------------------------------


def test_unknown_message_type_dropped() -> None:
    n = ClaudeNormalizer()
    assert n.normalize(object(), SESSION_ID) == []


def test_reset_clears_state() -> None:
    n = ClaudeNormalizer()
    n.add_pending_deny("a")
    msg = _make_assistant([TextBlock(text="x")])
    n.normalize(msg, SESSION_ID)
    n.reset()
    # reset 后 deny 集合空：同 id 的 ToolResult 应当透传
    msg2 = UserMessage(
        content=[ToolResultBlock(tool_use_id="a", content="r", is_error=False)],
    )
    out = n.normalize(msg2, SESSION_ID)
    assert len(out) == 1
