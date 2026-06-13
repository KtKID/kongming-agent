"""Codex normalizer 主路径映射测试（v0.1 最小覆盖）。

完整 25 边界用例在 T4。
"""

from __future__ import annotations

from hosts.web.integrations.codex.normalizer import INTERNAL_CONTENT_PREFIXES, normalize

SID = "test-sid-019dee"


class TestThreadStarted:
    def test_emits_session_created(self) -> None:
        msgs = normalize({"type": "thread.started", "thread_id": "019dee38"}, SID)
        assert len(msgs) == 1
        m = msgs[0]
        assert m["frame_type"] == "session_created"
        assert m["provider"] == "codex"
        assert m.get("newSessionId") == "019dee38"


class TestTurnLifecycle:
    def test_turn_started_silent(self) -> None:
        # turn.started 不发出（v0.1 砍）
        msgs = normalize({"type": "turn.started"}, SID)
        assert msgs == []

    def test_turn_completed_emits_complete_with_token_budget(self) -> None:
        msgs = normalize(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 50,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 10,
                },
            },
            SID,
        )
        assert len(msgs) == 1
        assert msgs[0]["frame_type"] == "complete"
        assert "tokenBudget" in msgs[0]

    def test_turn_failed_emits_error(self) -> None:
        msgs = normalize({"type": "turn.failed", "error": {"message": "boom"}}, SID)
        assert len(msgs) == 1
        assert msgs[0]["frame_type"] == "error"


class TestAgentMessage:
    def test_agent_message_emits_text(self) -> None:
        msgs = normalize(
            {
                "type": "item.completed",
                "item": {"id": "i0", "type": "agent_message", "text": "hello"},
            },
            SID,
        )
        assert len(msgs) == 1
        assert msgs[0]["frame_type"] == "text"
        assert msgs[0].get("content") == "hello"


class TestReasoning:
    def test_reasoning_emits_thinking(self) -> None:
        msgs = normalize(
            {
                "type": "item.completed",
                "item": {"id": "i1", "type": "reasoning", "text": "let me think..."},
            },
            SID,
        )
        assert len(msgs) == 1
        assert msgs[0]["frame_type"] == "thinking"


class TestCommandExecution:
    def test_started_emits_tool_use(self) -> None:
        msgs = normalize(
            {
                "type": "item.started",
                "item": {
                    "id": "i2",
                    "type": "command_execution",
                    "command": "ls",
                    "aggregated_output": "",
                    "exit_code": None,
                    "status": "in_progress",
                },
            },
            SID,
        )
        assert len(msgs) == 1
        assert msgs[0]["frame_type"] == "tool_use"

    def test_completed_emits_tool_result(self) -> None:
        msgs = normalize(
            {
                "type": "item.completed",
                "item": {
                    "id": "i2",
                    "type": "command_execution",
                    "command": "ls",
                    "aggregated_output": "a\nb",
                    "exit_code": 0,
                    "status": "completed",
                },
            },
            SID,
        )
        # 依实现可能 1 条（仅 tool_result）或 2 条（含一个补充的 tool_use）；至少要有 tool_result
        kinds = [m["frame_type"] for m in msgs]
        assert "tool_result" in kinds

    def test_failed_command_marks_is_error(self) -> None:
        msgs = normalize(
            {
                "type": "item.completed",
                "item": {
                    "id": "i3",
                    "type": "command_execution",
                    "command": "false",
                    "aggregated_output": "",
                    "exit_code": 1,
                    "status": "failed",
                },
            },
            SID,
        )
        # 找到 tool_result 那条
        results = [m for m in msgs if m["frame_type"] == "tool_result"]
        assert len(results) >= 1
        assert results[0].get("isError") is True


class TestUnknownEvent:
    def test_unknown_type_emits_error_with_raw(self) -> None:
        msgs = normalize({"type": "unknown.future.event", "foo": "bar"}, SID)
        assert len(msgs) == 1
        assert msgs[0]["frame_type"] == "error"
        # 必须保留原始事件供前端 raw_dump
        # 字段名实现可能用 content / raw / data 之一，至少包含原始信息
        m = msgs[0]
        raw_repr = str(m)
        assert "unknown.future.event" in raw_repr or "foo" in raw_repr


class TestInternalStrip:
    def test_internal_prefixes_constant_exists(self) -> None:
        # 至少包含 system-reminder 这种典型前缀
        joined = " ".join(INTERNAL_CONTENT_PREFIXES)
        assert "system-reminder" in joined or "system_reminder" in joined.lower()


class TestBaseFields:
    def test_base_fields_populated(self) -> None:
        msgs = normalize(
            {
                "type": "item.completed",
                "item": {"id": "i0", "type": "agent_message", "text": "hi"},
            },
            SID,
        )
        m = msgs[0]
        assert m["id"]  # uuid4 string
        assert m["sessionId"] == SID
        assert m["timestamp"]  # iso8601
        assert m["provider"] == "codex"
        assert m["frame_type"] == "text"
