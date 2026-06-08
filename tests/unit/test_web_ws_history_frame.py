"""unit：``_send_history_frame`` 把 runtime Message 转 NormalizedMessage history。

complete-generic-channel-manager-handoff：generic history 复用三频道统一的
NormalizedMessage[]，移除旧历史 DTO。

本测试覆盖：

1. user / assistant role：产 text message，保留 role/content
2. assistant tool_calls：产 tool_use message，保留 toolId/toolName/toolInput
3. tool role：产 tool_result message，保留 toolId/toolName/content/isError
"""

from __future__ import annotations

from typing import Any

import pytest

from core.message import Message, ToolCall
from hosts.web.protocol.ws_frames import ThreadHistoryFrame
from hosts.web.websocket.routes import _send_history_frame


class _FakeWS:
    """记录 send_json 调用的最小 WebSocket stub。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


class _FakeSession:
    def __init__(self, messages: list[Message]) -> None:
        self._messages = messages

    async def history(self) -> list[Message]:
        return list(self._messages)


class _FakeRuntime:
    def __init__(self, messages: list[Message]) -> None:
        self._sessions: dict[str, _FakeSession] = {"t1": _FakeSession(messages)}


class _FakeCell:
    def __init__(self, messages: list[Message]) -> None:
        self.thread_id = "t1"
        self.runtime = _FakeRuntime(messages)


def _last_frame(ws: _FakeWS) -> ThreadHistoryFrame:
    """从 ws.sent 取最后一帧并 round-trip 到 pydantic 模型，做强校验。"""
    assert ws.sent, "expected at least one frame"
    payload = ws.sent[-1]
    assert payload["frame_type"] == "thread.history"
    return ThreadHistoryFrame.model_validate(payload)


@pytest.mark.asyncio
async def test_history_frame_tool_message_carries_full_metadata() -> None:
    """tool 角色 Message：toolName / toolId / content / isError 传出。"""
    msg = Message(
        role="tool",
        content="stdout text",
        tool_call_id="call-1",
        name="run_shell",
        metadata={"ok": True, "data": {"exit_code": 0, "lines": 2}},
    )
    ws = _FakeWS()
    cell = _FakeCell([msg])
    await _send_history_frame(ws, cell)  # type: ignore[arg-type]

    frame = _last_frame(ws)
    assert len(frame.messages) == 1
    msg_out = frame.messages[0]
    assert msg_out["provider"] == "generic_chat"
    assert msg_out["frame_type"] == "tool_result"
    assert msg_out["content"] == "stdout text"
    assert msg_out["toolId"] == "call-1"
    assert msg_out["toolName"] == "run_shell"
    assert msg_out["isError"] is False
    assert "id" in msg_out
    assert "timestamp" in msg_out


@pytest.mark.asyncio
async def test_history_frame_tool_error_carries_error_message() -> None:
    """工具错误的 Message：metadata.error_message 传出。"""
    msg = Message(
        role="tool",
        content='{"error": "permission denied"}',
        tool_call_id="call-2",
        name="write_file",
        metadata={"ok": False, "error_message": "permission denied"},
    )
    ws = _FakeWS()
    cell = _FakeCell([msg])
    await _send_history_frame(ws, cell)  # type: ignore[arg-type]

    msg_out = _last_frame(ws).messages[0]
    assert msg_out["toolName"] == "write_file"
    assert msg_out["toolId"] == "call-2"
    assert msg_out["isError"] is True
    assert msg_out["content"] == '{"error": "permission denied"}'


@pytest.mark.asyncio
async def test_history_frame_user_and_assistant_have_none_tool_fields() -> None:
    """非 tool 角色：产 text message。"""
    user_msg = Message(role="user", content="hello")
    asst_msg = Message(role="assistant", content="world")
    ws = _FakeWS()
    cell = _FakeCell([user_msg, asst_msg])
    await _send_history_frame(ws, cell)  # type: ignore[arg-type]

    msgs = _last_frame(ws).messages
    assert len(msgs) == 2
    assert msgs[0]["frame_type"] == "text"
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "hello"
    assert msgs[1]["frame_type"] == "text"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == "world"


@pytest.mark.asyncio
async def test_history_frame_tool_with_missing_metadata_falls_to_none() -> None:
    """tool message 没 metadata → isError=false，不抛。"""
    msg = Message(
        role="tool",
        content="x",
        tool_call_id="call-3",
        name="echo",
        metadata={},  # 没有 ok / data / error_message
    )
    ws = _FakeWS()
    cell = _FakeCell([msg])
    await _send_history_frame(ws, cell)  # type: ignore[arg-type]

    msg_out = _last_frame(ws).messages[0]
    assert msg_out["toolName"] == "echo"
    assert msg_out["isError"] is False


@pytest.mark.asyncio
async def test_history_frame_tool_with_missing_name_uses_unknown() -> None:
    """tool result 缺 name 时仍产合法 NormalizedMessage。"""
    msg = Message.tool_result(
        tool_call_id="call-missing-name",
        content="tool output",
        name=None,
        metadata={"ok": True},
    )
    ws = _FakeWS()
    cell = _FakeCell([msg])
    await _send_history_frame(ws, cell)  # type: ignore[arg-type]

    msg_out = _last_frame(ws).messages[0]
    assert msg_out["frame_type"] == "tool_result"
    assert msg_out["toolId"] == "call-missing-name"
    assert msg_out["toolName"] == "unknown"
    assert msg_out["content"] == "tool output"
    assert msg_out["isError"] is False


@pytest.mark.asyncio
async def test_history_frame_tool_metadata_data_is_not_projected() -> None:
    """metadata.data 没有 NormalizedMessage 承载字段，本路径不透出。"""
    msg = Message(
        role="tool",
        content="x",
        tool_call_id="call-4",
        name="run_shell",
        metadata={"ok": True, "data": "not a dict"},
    )
    ws = _FakeWS()
    cell = _FakeCell([msg])
    await _send_history_frame(ws, cell)  # type: ignore[arg-type]

    msg_out = _last_frame(ws).messages[0]
    assert msg_out["toolName"] == "run_shell"
    assert msg_out["isError"] is False
    assert "data" not in msg_out


@pytest.mark.asyncio
async def test_history_frame_assistant_with_none_content_renders_empty_string() -> None:
    """assistant content=None 时 text content 为空字符串。"""
    msg = Message(role="assistant", content=None)
    ws = _FakeWS()
    cell = _FakeCell([msg])
    await _send_history_frame(ws, cell)  # type: ignore[arg-type]

    msg_out = _last_frame(ws).messages[0]
    assert msg_out["role"] == "assistant"
    assert msg_out["content"] == ""
    # 防御性强校验：彻底排除字面 "None" 字符串泄漏
    assert msg_out["content"] != "None"


@pytest.mark.asyncio
async def test_history_frame_user_with_none_content_renders_empty_string() -> None:
    """user role 同样的防御：理论上不会 None，但兜底 "" 不能是 "None"。"""
    msg = Message(role="user", content=None)
    ws = _FakeWS()
    cell = _FakeCell([msg])
    await _send_history_frame(ws, cell)  # type: ignore[arg-type]

    msg_out = _last_frame(ws).messages[0]
    assert msg_out["content"] == ""
    assert msg_out["content"] != "None"


@pytest.mark.asyncio
async def test_history_frame_assistant_tool_calls_emit_tool_use() -> None:
    """assistant tool_calls 历史产 tool_use，供前端恢复工具调用卡片。"""
    msg = Message.assistant(
        content=None,
        tool_calls=[
            ToolCall(call_id="call-5", tool_name="read_file", arguments={"path": "/tmp/a"})
        ],
    )
    ws = _FakeWS()
    cell = _FakeCell([msg])
    await _send_history_frame(ws, cell)  # type: ignore[arg-type]

    msg_out = _last_frame(ws).messages[0]
    assert msg_out["frame_type"] == "tool_use"
    assert msg_out["toolId"] == "call-5"
    assert msg_out["toolName"] == "read_file"
    assert msg_out["toolInput"] == {"path": "/tmp/a"}
