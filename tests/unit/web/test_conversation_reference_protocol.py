"""unit：conversation reference 协议契约。"""

from __future__ import annotations

from core.message import Message
from hosts.web.app_support.generic_history import normalize_generic_history
from hosts.web.protocol.conversation_references import ConversationReferenceDTO
from hosts.web.protocol.ws_frames import UserInputFrame, WSFrameC2SAdapter


def _skill_reference() -> ConversationReferenceDTO:
    """构造 skill reference，输入为空，输出为协议 DTO。"""
    return ConversationReferenceDTO(
        id="ref-1",
        kind="skill",
        ref="skill:skill-creator",
        label="Skill Creator",
        activation="inject_context",
        source_ref="skill:system:skill-creator",
        metadata={"source": "system"},
    )


def test_user_input_frame_references_round_trip() -> None:
    """验证 user.input 支持 references，输入为 skill 引用，输出为稳定字段。"""
    frame = UserInputFrame(
        text="如何设计这个 skill",
        request_id="req-1",
        references=[_skill_reference()],
    )

    payload = frame.model_dump()
    restored = WSFrameC2SAdapter.validate_python(payload)

    assert isinstance(restored, UserInputFrame)
    assert restored.references is not None
    assert restored.references[0].ref == "skill:skill-creator"
    assert restored.references[0].activation == "inject_context"


def test_user_message_stores_references_under_conversation_references_key() -> None:
    """锁住 Message.metadata 字段名，输入为 reference dict，输出为可序列化 metadata。"""
    ref = _skill_reference().model_dump()
    msg = Message.user(
        "hello",
        metadata={"conversation_references": [ref]},
    )

    assert msg.metadata["conversation_references"] == [ref]
    assert msg.metadata["conversation_references"][0]["label"] == "Skill Creator"


def test_generic_history_preserves_conversation_references_metadata() -> None:
    """generic history 归一化保留 user message metadata，供前端历史 chip 还原。"""
    ref = _skill_reference().model_dump()

    messages = normalize_generic_history(
        [
            Message.user(
                "hello",
                metadata={"conversation_references": [ref]},
            )
        ],
        session_id="thread-1",
    )

    assert messages[0]["metadata"]["conversation_references"] == [ref]
