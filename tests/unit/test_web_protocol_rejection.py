"""unit：web 协议层（v0.1.5）拒绝场景测试。

覆盖以下拒绝路径（与任务 #11 一一对应）：

1. 缺必填字段 → ValidationError
2. 字段类型错 → ValidationError
3. ``extra='forbid'`` 生效 → 未知字段 ValidationError
4. ``Literal`` 枚举字段拒绝错误值（ErrorCode / EvictReason / ApprovalOutcome / HistoryMessageRole）
5. ``WSFrameC2SAdapter`` / ``WSFrameS2CAdapter`` discriminator 拒绝未知 ``kind``
6. ``frozen=True`` 生效——构造后赋值会抛异常
7. ``ThreadMetadataDTO.id`` / ``CellSummaryDTO.thread_id`` pattern 约束
8. 数值约束（``LoginRequest.password`` ``min_length=1`` / ``ThreadMetadataDTO.message_count`` ``ge=0``）

所有测试用 ``pytest.raises(ValidationError)`` 风格，且每类相似断言通过
``pytest.parametrize`` 合并为单个函数减少重复。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from web.protocol.rest_models import (
    CellSummaryDTO,
    CreateThreadRequest,
    ErrorResponseDTO,
    HistoryMessageDTO,
    LLMPresetDTO,
    LoginRequest,
    RenameThreadRequest,
    ThreadMetadataDTO,
)
from web.protocol.ws_frames import (
    ApprovalAckFrame,
    ApprovalDecisionFrame,
    ApprovalRequestFrame,
    AssistantFinalFrame,
    CellEvictedFrame,
    ContentDeltaFrame,
    ErrorFrame,
    PingFrame,
    PongFrame,
    ReasoningDeltaFrame,
    SystemNoticeFrame,
    ThreadHistoryFrame,
    ToolCallEndFrame,
    ToolCallStartFrame,
    TurnEndFrame,
    TurnStartFrame,
    UsageFrame,
    UserInputFrame,
    WSFrameC2SAdapter,
    WSFrameS2CAdapter,
)

# ---------------------------------------------------------------------------
# 1. 缺必填字段
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        # UserInputFrame：缺 request_id
        (UserInputFrame, {"text": "hello"}),
        # ApprovalAckFrame：缺 approved
        (ApprovalAckFrame, {"call_id": "call-1"}),
        # HistoryMessageDTO：缺 content/turn/timestamp_ms
        (HistoryMessageDTO, {"role": "user"}),
        # ContentDeltaFrame：缺 seq + timestamp_ms
        (ContentDeltaFrame, {"delta": "x", "turn": 1}),
        # CreateThreadRequest：name + preset_id 都已改为可选，无必填字段可测
        # LoginRequest：完全空
        (LoginRequest, {}),
    ],
    ids=[
        "user_input_missing_request_id",
        "approval_ack_missing_approved",
        "history_message_missing_content_turn_timestamp",
        "content_delta_missing_seq_timestamp",
        "login_request_empty",
    ],
)
def test_reject_missing_required_fields(model: Any, kwargs: dict[str, Any]) -> None:
    """缺必填字段必须被 Pydantic 拒绝。"""
    with pytest.raises(ValidationError):
        model(**kwargs)


# ---------------------------------------------------------------------------
# 2. 字段类型错
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        # ContentDeltaFrame.turn 应为 int
        (
            ContentDeltaFrame,
            {"delta": "x", "turn": "not_int", "seq": 0, "timestamp_ms": 1},
        ),
        # ApprovalAckFrame.approved 应为 bool（"yes" 不是 bool）
        (ApprovalAckFrame, {"call_id": "x", "approved": "yes_string_not_bool"}),
        # UsageFrame.prompt_tokens 应为 int
        (
            UsageFrame,
            {
                "prompt_tokens": "many",
                "completion_tokens": 0,
                "total_tokens": 0,
                "turn": 1,
                "timestamp_ms": 1,
            },
        ),
        # ToolCallStartFrame.arguments 应为 dict
        (
            ToolCallStartFrame,
            {
                "tool_name": "shell",
                "call_id": "c1",
                "turn": 1,
                "arguments": "not_a_dict",
                "timestamp_ms": 1,
            },
        ),
        # ThreadHistoryFrame.messages 应为 list[HistoryMessageDTO]
        (ThreadHistoryFrame, {"messages": "not_a_list", "timestamp_ms": 1}),
        # ThreadMetadataDTO.created_at 应为 float / number
        (
            ThreadMetadataDTO,
            {
                "id": "thread-abcdef123456",
                "name": "x",
                "preset_id": "p",
                "created_at": "not_a_float",
                "updated_at": 0.0,
                "message_count": 0,
            },
        ),
    ],
    ids=[
        "content_delta_turn_str",
        "approval_ack_approved_str",
        "usage_prompt_tokens_str",
        "tool_call_start_arguments_str",
        "thread_history_messages_str",
        "thread_metadata_created_at_str",
    ],
)
def test_reject_wrong_field_type(model: Any, kwargs: dict[str, Any]) -> None:
    """字段类型不符必须被拒绝。"""
    with pytest.raises(ValidationError):
        model(**kwargs)


# ---------------------------------------------------------------------------
# 3. extra='forbid' 生效
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        # PingFrame：仅 timestamp_ms（C2S 基类无 timestamp_ms 字段）+ 未知字段
        (PingFrame, {"extra_field": "leak"}),
        # PongFrame：注入未知字段
        (PongFrame, {"timestamp_ms": 1, "unknown": "x"}),
        # LoginRequest：注入额外字段
        (LoginRequest, {"password": "x", "another": "y"}),
        # CreateThreadRequest：额外字段
        (CreateThreadRequest, {"name": "n", "preset_id": "p", "rogue": True}),
        # TurnStartFrame：额外字段
        (TurnStartFrame, {"turn": 1, "timestamp_ms": 1, "boom": "boom"}),
    ],
    ids=[
        "ping_extra_field",
        "pong_extra_field",
        "login_extra_field",
        "create_thread_extra_field",
        "turn_start_extra_field",
    ],
)
def test_reject_extra_forbidden_fields(model: Any, kwargs: dict[str, Any]) -> None:
    """extra='forbid' 必须拒绝未知字段。"""
    with pytest.raises(ValidationError):
        model(**kwargs)


# ---------------------------------------------------------------------------
# 4. Literal 枚举拒绝错误值
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        # ErrorCode 错误值
        (
            ErrorFrame,
            {"error_code": "invalid_code", "message": "x", "timestamp_ms": 1},
        ),
        # EvictReason 错误值
        (
            CellEvictedFrame,
            {"thread_id": "t", "reason": "bogus", "timestamp_ms": 1},
        ),
        # ApprovalOutcome 错误值
        (
            ApprovalDecisionFrame,
            {"call_id": "x", "outcome": "WAT", "turn": 1, "timestamp_ms": 1},
        ),
        # HistoryMessageRole 错误值
        (
            HistoryMessageDTO,
            {"role": "bot", "content": "x", "turn": 1, "timestamp_ms": 1},
        ),
        # ErrorResponseDTO（REST 端共享 ErrorCode）错误值
        (ErrorResponseDTO, {"error_code": "wrong_kind", "message": "x"}),
        # CellSummaryDTO.status Literal 错误值
        (
            CellSummaryDTO,
            {
                "thread_id": "thread-abcdef123456",
                "thread_name": "x",
                "preset_id": "p",
                "created_at": 0.0,
                "last_active_at": 0.0,
                "pending_approval_count": 0,
                "status": "shut_down",  # 不是 idle/running/awaiting_approval
            },
        ),
    ],
    ids=[
        "error_code_invalid",
        "evict_reason_invalid",
        "approval_outcome_invalid",
        "history_role_invalid",
        "error_response_code_invalid",
        "cell_summary_status_invalid",
    ],
)
def test_reject_literal_enum_invalid_value(model: Any, kwargs: dict[str, Any]) -> None:
    """Literal 枚举字段必须拒绝枚举集合外的值。"""
    with pytest.raises(ValidationError):
        model(**kwargs)


# ---------------------------------------------------------------------------
# 5. discriminator 拒绝未知 kind
# ---------------------------------------------------------------------------


def test_reject_unknown_kind_c2s_adapter() -> None:
    """C2S adapter 必须拒绝未注册的 kind。"""
    with pytest.raises(ValidationError):
        WSFrameC2SAdapter.validate_python({"kind": "unknown.kind", "text": "x", "request_id": "r"})


def test_reject_unknown_kind_s2c_adapter() -> None:
    """S2C adapter 必须拒绝未注册的 kind。"""
    with pytest.raises(ValidationError):
        WSFrameS2CAdapter.validate_python({"kind": "unknown.kind", "timestamp_ms": 1})


def test_reject_c2s_adapter_with_s2c_kind() -> None:
    """C2S 入站 adapter 不应接受 S2C 帧的 kind（如 ``content.delta``）。"""
    with pytest.raises(ValidationError):
        WSFrameC2SAdapter.validate_python(
            {
                "kind": "content.delta",
                "delta": "x",
                "turn": 1,
                "seq": 0,
                "timestamp_ms": 1,
            }
        )


def test_reject_s2c_adapter_with_c2s_kind() -> None:
    """S2C 出站 adapter 不应接受 C2S 帧的 kind（如 ``user.input``）。"""
    with pytest.raises(ValidationError):
        WSFrameS2CAdapter.validate_python({"kind": "user.input", "text": "x", "request_id": "r"})


# ---------------------------------------------------------------------------
# 6. frozen 生效
#
# Pydantic v2 在 frozen=True 模型上对赋值会抛 ValidationError（type='frozen_instance'）。
# 见 https://docs.pydantic.dev/latest/api/config/#pydantic.config.ConfigDict.frozen
# ---------------------------------------------------------------------------


def test_reject_attribute_assignment_on_frozen_frame() -> None:
    """frozen=True 帧构造后不允许字段赋值（C2S：``PingFrame`` 无非 kind 字段，
    选有字段的 ``UserInputFrame``）。"""
    frame = UserInputFrame(text="hi", request_id="r1")
    with pytest.raises(ValidationError):
        frame.text = "mutated"  # type: ignore[misc]


def test_reject_attribute_assignment_on_frozen_dto() -> None:
    """frozen=True DTO 构造后不允许字段赋值。"""
    dto = LoginRequest(password="x")
    with pytest.raises(ValidationError):
        dto.password = "y"  # type: ignore[misc]


def test_reject_attribute_assignment_on_complex_s2c_frame() -> None:
    """带多字段的 S2C 帧同样不允许赋值。"""
    frame = AssistantFinalFrame(content="x", turn=1, timestamp_ms=1)
    with pytest.raises(ValidationError):
        frame.content = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 7. pattern 约束（thread id 形式）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "not-a-thread-id",
        "thread-XYZ123456789",  # 非小写 a-f
        "thread-abcdef",  # 长度不足
        "thread-abcdef1234567",  # 长度超出
        "thread_abcdef123456",  # 用下划线
        "",
    ],
    ids=[
        "freeform",
        "uppercase_hex",
        "too_short",
        "too_long",
        "wrong_separator",
        "empty",
    ],
)
def test_reject_invalid_thread_id_pattern_thread_metadata(bad_id: str) -> None:
    """ThreadMetadataDTO.id 必须匹配 ``^thread-[a-f0-9]{12}$``。"""
    with pytest.raises(ValidationError):
        ThreadMetadataDTO(
            id=bad_id,
            name="n",
            preset_id="p",
            created_at=0.0,
            updated_at=0.0,
            message_count=0,
        )


def test_reject_invalid_thread_id_pattern_cell_summary() -> None:
    """CellSummaryDTO.thread_id 也受同样的 pattern 约束。"""
    with pytest.raises(ValidationError):
        CellSummaryDTO(
            thread_id="bad-id",
            thread_name="x",
            preset_id="p",
            created_at=0.0,
            last_active_at=0.0,
            pending_approval_count=0,
            status="idle",
        )


def test_accept_valid_thread_id_pattern() -> None:
    """合法的 thread id 应能通过（防止上面 pattern 测试的反向退化）。"""
    dto = ThreadMetadataDTO(
        id="thread-abcdef123456",
        name="ok",
        preset_id="p",
        created_at=0.0,
        updated_at=0.0,
        message_count=0,
    )
    assert dto.id == "thread-abcdef123456"


# ---------------------------------------------------------------------------
# 8. 数值 / 字符串约束
# ---------------------------------------------------------------------------


def test_reject_login_request_empty_password() -> None:
    """LoginRequest.password 受 ``min_length=1`` 约束，空串应被拒。"""
    with pytest.raises(ValidationError):
        LoginRequest(password="")


@pytest.mark.parametrize("bad_count", [-1, -100])
def test_reject_thread_metadata_negative_message_count(bad_count: int) -> None:
    """ThreadMetadataDTO.message_count 受 ``ge=0`` 约束。"""
    with pytest.raises(ValidationError):
        ThreadMetadataDTO(
            id="thread-abcdef123456",
            name="n",
            preset_id="p",
            created_at=0.0,
            updated_at=0.0,
            message_count=bad_count,
        )


def test_reject_cell_summary_negative_pending_approval_count() -> None:
    """CellSummaryDTO.pending_approval_count 同样 ``ge=0``。"""
    with pytest.raises(ValidationError):
        CellSummaryDTO(
            thread_id="thread-abcdef123456",
            thread_name="x",
            preset_id="p",
            created_at=0.0,
            last_active_at=0.0,
            pending_approval_count=-1,
            status="idle",
        )


@pytest.mark.parametrize(
    "model",
    [CreateThreadRequest, RenameThreadRequest],
    ids=["create_thread", "rename_thread"],
)
def test_reject_thread_name_exceeds_max_length(model: Any) -> None:
    """``name`` 字段受 ``max_length=200`` 约束，超长应被拒。"""
    too_long = "x" * 201
    if model is CreateThreadRequest:
        with pytest.raises(ValidationError):
            model(name=too_long, preset_id="p")
    else:
        with pytest.raises(ValidationError):
            model(name=too_long)


# ---------------------------------------------------------------------------
# 额外：S2C 帧统一缺 timestamp_ms（_S2CFrameBase 强制必填）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        (PongFrame, {}),
        (TurnEndFrame, {"turn": 1}),
        (ToolCallEndFrame, {"call_id": "c", "turn": 1, "ok": True}),
        (
            ApprovalRequestFrame,
            {"call_id": "c", "tool_name": "t", "arguments": {}, "turn": 1},
        ),
        (ReasoningDeltaFrame, {"delta": "x", "turn": 1, "seq": 0}),
        (
            SystemNoticeFrame,
            {
                "notice_key": "self_evolution.review",
                "source": "self_evolution",
                "status": "running",
                "title": "t",
                "message": "m",
                "details": {},
                "icon": "sparkles",
            },
        ),
    ],
    ids=[
        "pong",
        "turn_end",
        "tool_call_end",
        "approval_request",
        "reasoning_delta",
        "system_notice",
    ],
)
def test_reject_s2c_frame_missing_timestamp_ms(model: Any, kwargs: dict[str, Any]) -> None:
    """S2C 基类强制 ``timestamp_ms`` 必填。"""
    with pytest.raises(ValidationError):
        model(**kwargs)


# ---------------------------------------------------------------------------
# 额外：LLMPresetDTO 故意脱敏 api_key——意外注入应被拒
# ---------------------------------------------------------------------------


def test_reject_llm_preset_with_api_key_field() -> None:
    """LLMPresetDTO 必须脱敏：注入 ``api_key`` 字段会被 ``extra='forbid'`` 拒绝。"""
    with pytest.raises(ValidationError):
        LLMPresetDTO(
            id="p1",
            display_name="GPT",
            model="gpt-4",
            base_url_summary="api.openai.com",
            requires_api_key=True,
            api_key="sk-leak",  # type: ignore[call-arg]
        )
