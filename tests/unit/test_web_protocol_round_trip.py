"""Round-trip 单元测试：v0.1.5 web 协议层（任务 #10）。

覆盖：
- 18 个 WS 帧的 ``model_dump_json`` ↔ ``model_validate_json`` round-trip
- 8 个 REST DTO 的 round-trip
- ``WSFrameC2SAdapter`` 与 ``WSFrameS2CAdapter`` 的 discriminator 分派
- 各帧 / DTO 的 ``kind`` / ``schema_version`` 默认值

设计要点：
- 用 ``pytest.parametrize`` 把 25 个帧/DTO round-trip 收敛为 2 个参数化测试
- adapter 分派测试单独写 2 个，验证 union 能正确按 kind 还原具体类
- 帧字段严格按 :mod:`web.protocol.ws_frames` 与 :mod:`web.protocol.rest_models`
  的真实声明（必填字段全填，可选字段挑代表性场景覆盖）
"""

from __future__ import annotations

import pytest

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
# Fixtures：构造器返回 (instance, expected_kind) 对
# ---------------------------------------------------------------------------


def _make_user_input() -> UserInputFrame:
    return UserInputFrame(text="hello", request_id="req-1")


def _make_approval_ack() -> ApprovalAckFrame:
    return ApprovalAckFrame(call_id="call-1", action="accept_once")


def _make_ping() -> PingFrame:
    return PingFrame()


def _make_thread_history() -> ThreadHistoryFrame:
    msg = HistoryMessageDTO(
        role="user",
        content="hi",
        turn=0,
        timestamp_ms=1_700_000_000_000,
    )
    return ThreadHistoryFrame(timestamp_ms=1_700_000_000_001, messages=[msg])


def _make_assistant_final() -> AssistantFinalFrame:
    return AssistantFinalFrame(
        timestamp_ms=1_700_000_000_002,
        content="done",
        turn=1,
    )


def _make_content_delta() -> ContentDeltaFrame:
    return ContentDeltaFrame(
        timestamp_ms=1_700_000_000_003,
        delta="he",
        turn=1,
        seq=0,
    )


def _make_reasoning_delta() -> ReasoningDeltaFrame:
    return ReasoningDeltaFrame(
        timestamp_ms=1_700_000_000_004,
        delta="thinking...",
        turn=1,
        seq=2,
    )


def _make_tool_call_start() -> ToolCallStartFrame:
    return ToolCallStartFrame(
        timestamp_ms=1_700_000_000_005,
        tool_name="read_file",
        call_id="call-2",
        turn=1,
        arguments={"path": "/tmp/x"},
    )


def _make_tool_call_end() -> ToolCallEndFrame:
    return ToolCallEndFrame(
        timestamp_ms=1_700_000_000_006,
        call_id="call-2",
        turn=1,
        ok=True,
    )


def _make_approval_request() -> ApprovalRequestFrame:
    return ApprovalRequestFrame(
        timestamp_ms=1_700_000_000_007,
        call_id="call-3",
        tool_name="shell",
        arguments={"cmd": "ls"},
        reason="needs sudo",
        turn=2,
    )


def _make_approval_decision() -> ApprovalDecisionFrame:
    return ApprovalDecisionFrame(
        timestamp_ms=1_700_000_000_008,
        call_id="call-3",
        outcome="approved",
        turn=2,
    )


def _make_usage() -> UsageFrame:
    return UsageFrame(
        timestamp_ms=1_700_000_000_009,
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        turn=1,
    )


def _make_error() -> ErrorFrame:
    return ErrorFrame(
        timestamp_ms=1_700_000_000_010,
        error_code="llm_error",
        message="boom",
        turn=1,
    )


def _make_turn_start() -> TurnStartFrame:
    return TurnStartFrame(timestamp_ms=1_700_000_000_011, turn=1)


def _make_turn_end() -> TurnEndFrame:
    return TurnEndFrame(timestamp_ms=1_700_000_000_012, turn=1)


def _make_pong() -> PongFrame:
    return PongFrame(timestamp_ms=1_700_000_000_013)


def _make_system_notice() -> SystemNoticeFrame:
    return SystemNoticeFrame(
        timestamp_ms=1_700_000_000_014,
        notice_key="self_evolution.review",
        source="self_evolution",
        status="completed",
        title="Self-evolution review completed",
        message="Review finished and wrote evolution nutrients.",
        details={"review_id": "evo-review:run-1", "write_status": "written"},
        icon="check-circle",
        run_id="run-1",
    )


def _make_cell_evicted() -> CellEvictedFrame:
    return CellEvictedFrame(
        timestamp_ms=1_700_000_000_015,
        thread_id="thread-aaaaaaaaaaaa",
        reason="idle",
        message="60s no activity",
    )


# REST DTO factories --------------------------------------------------------


def _make_history_message() -> HistoryMessageDTO:
    return HistoryMessageDTO(
        role="tool",
        content="ok",
        turn=1,
        timestamp_ms=1_700_000_000_100,
        tool_call_id="call-x",
    )


def _make_thread_metadata() -> ThreadMetadataDTO:
    return ThreadMetadataDTO(
        id="thread-abcdef012345",
        name="my thread",
        preset_id="preset-default",
        backend_kind="generic_chat",
        claude_thread_id="",
        codex_thread_id="",
        cwd="",
        created_at=1_700_000_000.0,
        updated_at=1_700_000_010.5,
        message_count=3,
        cumulative_prompt_tokens=1200,
        cumulative_completion_tokens=340,
        cumulative_total_tokens=1540,
        schema_version=6,
    )


def _make_create_thread_request() -> CreateThreadRequest:
    return CreateThreadRequest(name="new chat", preset_id="preset-default")


def _make_rename_thread_request() -> RenameThreadRequest:
    return RenameThreadRequest(name="renamed")


def _make_llm_preset() -> LLMPresetDTO:
    return LLMPresetDTO(
        id="preset-1",
        display_name="GPT-4 Turbo",
        model="gpt-4-turbo",
        base_url_summary="api.openai.com",
        requires_api_key=True,
    )


def _make_login_request() -> LoginRequest:
    return LoginRequest(password="hunter2")


def _make_cell_summary() -> CellSummaryDTO:
    return CellSummaryDTO(
        thread_id="thread-abcdef012345",
        thread_name="t",
        preset_id="preset-1",
        created_at=1_700_000_000.0,
        last_active_at=1_700_000_100.0,
        current_turn=2,
        pending_approval_count=0,
        status="idle",
    )


def _make_error_response() -> ErrorResponseDTO:
    return ErrorResponseDTO(
        error_code="internal",
        message="oops",
        details={"trace_id": "abc"},
    )


# ---------------------------------------------------------------------------
# Round-trip 参数化：WS 帧（18 个）
# ---------------------------------------------------------------------------


WS_FRAME_FACTORIES = [
    pytest.param(_make_user_input, id="user_input_frame"),
    pytest.param(_make_approval_ack, id="approval_ack_frame"),
    pytest.param(_make_ping, id="ping_frame"),
    pytest.param(_make_thread_history, id="thread_history_frame"),
    pytest.param(_make_assistant_final, id="assistant_final_frame"),
    pytest.param(_make_content_delta, id="content_delta_frame"),
    pytest.param(_make_reasoning_delta, id="reasoning_delta_frame"),
    pytest.param(_make_tool_call_start, id="tool_call_start_frame"),
    pytest.param(_make_tool_call_end, id="tool_call_end_frame"),
    pytest.param(_make_approval_request, id="approval_request_frame"),
    pytest.param(_make_approval_decision, id="approval_decision_frame"),
    pytest.param(_make_usage, id="usage_frame"),
    pytest.param(_make_error, id="error_frame"),
    pytest.param(_make_turn_start, id="turn_start_frame"),
    pytest.param(_make_turn_end, id="turn_end_frame"),
    pytest.param(_make_pong, id="pong_frame"),
    pytest.param(_make_system_notice, id="system_notice_frame"),
    pytest.param(_make_cell_evicted, id="cell_evicted_frame"),
]


@pytest.mark.parametrize("factory", WS_FRAME_FACTORIES)
def test_round_trip_ws_frame(factory):
    """每个 WS 帧最小有效实例 → JSON → 还原 → 等价。"""
    original = factory()
    json_blob = original.model_dump_json()
    reconstructed = type(original).model_validate_json(json_blob)
    assert original == reconstructed


# ---------------------------------------------------------------------------
# Round-trip 参数化：REST DTO（8 个）
# ---------------------------------------------------------------------------


REST_DTO_FACTORIES = [
    pytest.param(_make_history_message, id="history_message_dto"),
    pytest.param(_make_thread_metadata, id="thread_metadata_dto"),
    pytest.param(_make_create_thread_request, id="create_thread_request"),
    pytest.param(_make_rename_thread_request, id="rename_thread_request"),
    pytest.param(_make_llm_preset, id="llm_preset_dto"),
    pytest.param(_make_login_request, id="login_request"),
    pytest.param(_make_cell_summary, id="cell_summary_dto"),
    pytest.param(_make_error_response, id="error_response_dto"),
]


@pytest.mark.parametrize("factory", REST_DTO_FACTORIES)
def test_round_trip_rest_dto(factory):
    """每个 REST DTO 最小有效实例 → JSON → 还原 → 等价。"""
    original = factory()
    json_blob = original.model_dump_json()
    reconstructed = type(original).model_validate_json(json_blob)
    assert original == reconstructed


# ---------------------------------------------------------------------------
# Adapter 分派：C2S（3 帧）
# ---------------------------------------------------------------------------


C2S_DISPATCH_CASES = [
    pytest.param(
        {"kind": "user.input", "text": "hi", "request_id": "r-1"},
        UserInputFrame,
        id="user_input_dispatch",
    ),
    pytest.param(
        {"kind": "approval.ack", "call_id": "c-1", "action": "reject"},
        ApprovalAckFrame,
        id="approval_ack_dispatch",
    ),
    pytest.param({"kind": "ping"}, PingFrame, id="ping_dispatch"),
]


@pytest.mark.parametrize("payload, expected_cls", C2S_DISPATCH_CASES)
def test_ws_c2s_adapter_dispatch(payload, expected_cls):
    """``WSFrameC2SAdapter`` 按 ``kind`` 分派到正确帧类。"""
    obj = WSFrameC2SAdapter.validate_python(payload)
    assert isinstance(obj, expected_cls)
    assert obj.kind == payload["kind"]


# ---------------------------------------------------------------------------
# Adapter 分派：S2C（15 帧）
# ---------------------------------------------------------------------------


S2C_DISPATCH_CASES = [
    pytest.param(
        {
            "kind": "thread.history",
            "timestamp_ms": 1,
            "messages": [],
        },
        ThreadHistoryFrame,
        id="thread_history_dispatch",
    ),
    pytest.param(
        {
            "kind": "assistant.final",
            "timestamp_ms": 1,
            "content": "x",
            "turn": 1,
        },
        AssistantFinalFrame,
        id="assistant_final_dispatch",
    ),
    pytest.param(
        {
            "kind": "content.delta",
            "timestamp_ms": 1,
            "delta": "x",
            "turn": 1,
            "seq": 0,
        },
        ContentDeltaFrame,
        id="content_delta_dispatch",
    ),
    pytest.param(
        {
            "kind": "reasoning.delta",
            "timestamp_ms": 1,
            "delta": "x",
            "turn": 1,
            "seq": 0,
        },
        ReasoningDeltaFrame,
        id="reasoning_delta_dispatch",
    ),
    pytest.param(
        {
            "kind": "tool.call.start",
            "timestamp_ms": 1,
            "tool_name": "t",
            "call_id": "c",
            "turn": 1,
            "arguments": {},
        },
        ToolCallStartFrame,
        id="tool_call_start_dispatch",
    ),
    pytest.param(
        {
            "kind": "tool.call.end",
            "timestamp_ms": 1,
            "call_id": "c",
            "turn": 1,
            "ok": True,
        },
        ToolCallEndFrame,
        id="tool_call_end_dispatch",
    ),
    pytest.param(
        {
            "kind": "approval.request",
            "timestamp_ms": 1,
            "call_id": "c",
            "tool_name": "t",
            "arguments": {},
            "turn": 1,
        },
        ApprovalRequestFrame,
        id="approval_request_dispatch",
    ),
    pytest.param(
        {
            "kind": "approval.decision",
            "timestamp_ms": 1,
            "call_id": "c",
            "outcome": "rejected",
            "turn": 1,
        },
        ApprovalDecisionFrame,
        id="approval_decision_dispatch",
    ),
    pytest.param(
        {
            "kind": "usage",
            "timestamp_ms": 1,
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
            "turn": 1,
        },
        UsageFrame,
        id="usage_dispatch",
    ),
    pytest.param(
        {
            "kind": "error",
            "timestamp_ms": 1,
            "error_code": "network",
            "message": "m",
        },
        ErrorFrame,
        id="error_dispatch",
    ),
    pytest.param(
        {"kind": "turn.start", "timestamp_ms": 1, "turn": 1},
        TurnStartFrame,
        id="turn_start_dispatch",
    ),
    pytest.param(
        {"kind": "turn.end", "timestamp_ms": 1, "turn": 1},
        TurnEndFrame,
        id="turn_end_dispatch",
    ),
    pytest.param(
        {"kind": "pong", "timestamp_ms": 1},
        PongFrame,
        id="pong_dispatch",
    ),
    pytest.param(
        {
            "kind": "system.notice",
            "timestamp_ms": 1,
            "notice_key": "self_evolution.review",
            "source": "self_evolution",
            "status": "running",
            "title": "Self-evolution review started",
            "message": "Review evo-review:run-1 is collecting a transcript window.",
            "details": {"review_id": "evo-review:run-1"},
            "icon": "sparkles",
            "run_id": "run-1",
        },
        SystemNoticeFrame,
        id="system_notice_dispatch",
    ),
    pytest.param(
        {
            "kind": "cell.evicted",
            "timestamp_ms": 1,
            "thread_id": "thread-abcdef012345",
            "reason": "manual_stop",
        },
        CellEvictedFrame,
        id="cell_evicted_dispatch",
    ),
]


@pytest.mark.parametrize("payload, expected_cls", S2C_DISPATCH_CASES)
def test_ws_s2c_adapter_dispatch(payload, expected_cls):
    """``WSFrameS2CAdapter`` 按 ``kind`` 分派到正确帧类。"""
    obj = WSFrameS2CAdapter.validate_python(payload)
    assert isinstance(obj, expected_cls)
    assert obj.kind == payload["kind"]


# ---------------------------------------------------------------------------
# kind / schema_version 默认值
# ---------------------------------------------------------------------------


def test_kind_default_user_input():
    assert UserInputFrame(text="x", request_id="r").kind == "user.input"


def test_kind_default_approval_ack():
    assert ApprovalAckFrame(call_id="c", action="accept_once").kind == "approval.ack"


def test_kind_default_ping():
    assert PingFrame().kind == "ping"


def test_kind_default_thread_history():
    frame = ThreadHistoryFrame(timestamp_ms=1, messages=[])
    assert frame.kind == "thread.history"


def test_kind_default_assistant_final():
    frame = AssistantFinalFrame(timestamp_ms=1, content="x", turn=1)
    assert frame.kind == "assistant.final"


def test_kind_default_content_delta():
    frame = ContentDeltaFrame(timestamp_ms=1, delta="x", turn=1, seq=0)
    assert frame.kind == "content.delta"


def test_kind_default_reasoning_delta():
    frame = ReasoningDeltaFrame(timestamp_ms=1, delta="x", turn=1, seq=0)
    assert frame.kind == "reasoning.delta"


def test_kind_default_tool_call_start():
    frame = ToolCallStartFrame(timestamp_ms=1, tool_name="t", call_id="c", turn=1, arguments={})
    assert frame.kind == "tool.call.start"


def test_kind_default_tool_call_end():
    frame = ToolCallEndFrame(timestamp_ms=1, call_id="c", turn=1, ok=True)
    assert frame.kind == "tool.call.end"


def test_kind_default_approval_request():
    frame = ApprovalRequestFrame(timestamp_ms=1, call_id="c", tool_name="t", arguments={}, turn=1)
    assert frame.kind == "approval.request"


def test_kind_default_approval_decision():
    frame = ApprovalDecisionFrame(timestamp_ms=1, call_id="c", outcome="approved", turn=1)
    assert frame.kind == "approval.decision"


def test_kind_default_usage():
    frame = UsageFrame(
        timestamp_ms=1,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        turn=1,
    )
    assert frame.kind == "usage"


def test_kind_default_error():
    frame = ErrorFrame(timestamp_ms=1, error_code="internal", message="m")
    assert frame.kind == "error"


def test_kind_default_turn_start():
    assert TurnStartFrame(timestamp_ms=1, turn=1).kind == "turn.start"


def test_kind_default_turn_end():
    assert TurnEndFrame(timestamp_ms=1, turn=1).kind == "turn.end"


def test_kind_default_pong():
    assert PongFrame(timestamp_ms=1).kind == "pong"


def test_kind_default_system_notice():
    frame = SystemNoticeFrame(
        timestamp_ms=1,
        notice_key="self_evolution.review",
        source="self_evolution",
        status="running",
        title="t",
        message="m",
        details={},
        icon="sparkles",
    )
    assert frame.kind == "system.notice"


def test_kind_default_cell_evicted():
    frame = CellEvictedFrame(timestamp_ms=1, thread_id="thread-abcdef012345", reason="idle")
    assert frame.kind == "cell.evicted"


def test_thread_metadata_schema_version_default():
    """``ThreadMetadataDTO.schema_version`` 默认 ``6``（v0.2.2 bump，加 codex thread 字段）。"""
    dto = ThreadMetadataDTO(
        id="thread-abcdef012345",
        name="x",
        preset_id="p",
        backend_kind="generic_chat",
        created_at=0.0,
        updated_at=0.0,
        message_count=0,
    )
    assert dto.schema_version == 6
