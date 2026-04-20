"""unit：core.message 基本行为。

验证 :class:`core.message.Message` 的 factory helper 能构造出形状正确的消息，
:class:`ToolCall` 字段默认值合理。
"""

from __future__ import annotations

import pytest

from core.message import Message, ToolCall


@pytest.mark.unit
def test_user_factory_sets_role_and_content() -> None:
    m = Message.user("hi")
    assert m.role == "user"
    assert m.content == "hi"
    assert m.tool_calls is None
    assert m.tool_call_id is None
    assert m.metadata == {}


@pytest.mark.unit
def test_system_factory_sets_role_and_content() -> None:
    m = Message.system("SYS")
    assert m.role == "system"
    assert m.content == "SYS"


@pytest.mark.unit
def test_assistant_factory_with_content_only() -> None:
    m = Message.assistant(content="ok")
    assert m.role == "assistant"
    assert m.content == "ok"
    assert m.tool_calls is None


@pytest.mark.unit
def test_assistant_factory_with_tool_calls() -> None:
    call = ToolCall(call_id="c1", tool_name="read_file", arguments={"path": "/x"})
    m = Message.assistant(content=None, tool_calls=[call])
    assert m.role == "assistant"
    assert m.content is None
    assert m.tool_calls is not None
    assert len(m.tool_calls) == 1
    assert m.tool_calls[0].call_id == "c1"
    # tool_calls 总被转成不可变元组
    assert isinstance(m.tool_calls, tuple)


@pytest.mark.unit
def test_tool_result_factory_requires_tool_call_id() -> None:
    m = Message.tool_result(tool_call_id="c1", content="ok", name="read_file")
    assert m.role == "tool"
    assert m.tool_call_id == "c1"
    assert m.content == "ok"
    assert m.name == "read_file"


@pytest.mark.unit
def test_tool_result_factory_metadata_defaults_to_empty() -> None:
    m = Message.tool_result(tool_call_id="c", content="x")
    assert m.metadata == {}


@pytest.mark.unit
def test_tool_call_default_arguments_is_empty_dict() -> None:
    tc = ToolCall(call_id="c", tool_name="t")
    assert tc.arguments == {}


@pytest.mark.unit
def test_message_is_frozen() -> None:
    """Message 是 frozen dataclass；不能就地赋值。"""
    m = Message.user("x")
    with pytest.raises(Exception):
        # frozen → FrozenInstanceError（Exception 子类）
        m.content = "mutated"  # type: ignore[misc]
