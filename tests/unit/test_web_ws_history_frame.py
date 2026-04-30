"""unit：``_send_history_frame`` 把 tool 角色 Message 完整转 HistoryMessageDTO。

v0.1.6 修 web 历史重放路径"工具卡片显示空"bug 的回归保护：之前 DTO 只透传
role/content/turn/tool_call_id；前端从历史重建 tool 卡片时丢 toolName / ok /
data / errorMessage（即便 Message 里都有）。本测试覆盖：

1. tool role：DTO 正确填 tool_name（取自 Message.name）+ ok / data /
   error_message（取自 Message.metadata）+ content（ToolResult.content）
2. user / assistant role：tool_name / ok / data / error_message 全为 None
3. metadata 缺字段或类型错误时，DTO 字段安全降级为 None
"""

from __future__ import annotations

from typing import Any

import pytest

from core.message import Message
from web.protocol.ws_frames import ThreadHistoryFrame
from web.ws import _send_history_frame


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
    assert payload["kind"] == "thread.history"
    return ThreadHistoryFrame.model_validate(payload)


@pytest.mark.asyncio
async def test_history_frame_tool_message_carries_full_metadata() -> None:
    """tool 角色 Message：tool_name / ok / data / error_message 全部传出。"""
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
    dto = frame.messages[0]
    assert dto.role == "tool"
    assert dto.content == "stdout text"
    assert dto.tool_call_id == "call-1"
    assert dto.tool_name == "run_shell"
    assert dto.ok is True
    assert dto.data == {"exit_code": 0, "lines": 2}
    assert dto.error_message is None


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

    dto = _last_frame(ws).messages[0]
    assert dto.tool_name == "write_file"
    assert dto.ok is False
    assert dto.error_message == "permission denied"


@pytest.mark.asyncio
async def test_history_frame_user_and_assistant_have_none_tool_fields() -> None:
    """非 tool 角色：tool_name / ok / data / error_message 必须 None。"""
    user_msg = Message(role="user", content="hello")
    asst_msg = Message(role="assistant", content="world")
    ws = _FakeWS()
    cell = _FakeCell([user_msg, asst_msg])
    await _send_history_frame(ws, cell)  # type: ignore[arg-type]

    msgs = _last_frame(ws).messages
    assert len(msgs) == 2
    for dto in msgs:
        assert dto.tool_name is None
        assert dto.ok is None
        assert dto.data is None
        assert dto.error_message is None


@pytest.mark.asyncio
async def test_history_frame_tool_with_missing_metadata_falls_to_none() -> None:
    """tool message 没 metadata / metadata 类型错误 → 字段安全降级 None，不抛。"""
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

    dto = _last_frame(ws).messages[0]
    assert dto.tool_name == "echo"
    assert dto.ok is None
    assert dto.data is None
    assert dto.error_message is None


@pytest.mark.asyncio
async def test_history_frame_tool_with_non_dict_metadata_data_returns_none() -> None:
    """metadata.data 不是 dict 时 frame 写 None（防御损坏数据）。"""
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

    dto = _last_frame(ws).messages[0]
    assert dto.tool_name == "run_shell"
    assert dto.ok is True
    assert dto.data is None  # 非 dict 兜底


@pytest.mark.asyncio
async def test_history_frame_assistant_with_none_content_renders_empty_string() -> None:
    """v0.1.6 修：assistant 只发 tool_calls 时 ``Message.content=None``，DTO
    必须落空字符串而不是字面 ``"None"``——之前 ``str(content)`` 把 None 转成
    字符串 "None"，前端用户消息后会跟一个白框写着 "None"，体感像 bug。
    """
    msg = Message(role="assistant", content=None)
    ws = _FakeWS()
    cell = _FakeCell([msg])
    await _send_history_frame(ws, cell)  # type: ignore[arg-type]

    dto = _last_frame(ws).messages[0]
    assert dto.role == "assistant"
    assert dto.content == ""  # 不能是 "None"
    # 防御性强校验：彻底排除字面 "None" 字符串泄漏
    assert dto.content != "None"


@pytest.mark.asyncio
async def test_history_frame_user_with_none_content_renders_empty_string() -> None:
    """user role 同样的防御：理论上不会 None，但兜底 "" 不能是 "None"。"""
    msg = Message(role="user", content=None)
    ws = _FakeWS()
    cell = _FakeCell([msg])
    await _send_history_frame(ws, cell)  # type: ignore[arg-type]

    dto = _last_frame(ws).messages[0]
    assert dto.content == ""
    assert dto.content != "None"
