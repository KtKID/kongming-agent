"""Round-trip 单元测试：v0.1.5 web 协议层（任务 #10）。

覆盖：
- 18 个 WS 帧的 ``model_dump_json`` ↔ ``model_validate_json`` round-trip
- 8 个 REST DTO 的 round-trip
- ``WSFrameC2SAdapter`` 与 ``WSFrameS2CAdapter`` 的 discriminator 分派
- 各帧 / DTO 的 ``frame_type`` / ``schema_version`` 默认值

设计要点：
- 用 ``pytest.parametrize`` 把 25 个帧/DTO round-trip 收敛为 2 个参数化测试
- adapter 分派测试单独写 2 个，验证 union 能正确按 frame_type 还原具体类
- 帧字段严格按 :mod:`web.protocol.ws_frames` 与 :mod:`web.protocol.rest_models`
  的真实声明（必填字段全填，可选字段挑代表性场景覆盖）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from hosts.web.protocol.rest_models import (
    CellSummaryDTO,
    CreateCronTaskRequest,
    CreateGenericThreadFromFirstMessageRequest,
    CreateGenericThreadFromFirstMessageResponse,
    CreateThreadRequest,
    CronRunDTO,
    CronRunMessagesResponse,
    CronRunsPage,
    CronTaskDTO,
    ErrorResponseDTO,
    LLMPresetDTO,
    LoginRequest,
    RenameThreadRequest,
    RunNowResponse,
    ThreadMetadataDTO,
    ThreadPermissionsDTO,
    ThreadSubAgentItemDTO,
    ThreadSubAgentListDTO,
    UpdateCronTaskRequest,
    UpdateThreadPermissionsRequest,
)
from hosts.web.protocol.ws_frames import (
    AbortSessionFrame,
    ApprovalDecisionFrame,
    ApprovalInboxAddFrame,
    ApprovalInboxItem,
    ApprovalInboxRemoveFrame,
    ApprovalInboxResolveFrame,
    ApprovalInboxResolveResultFrame,
    ApprovalInboxSnapshotFrame,
    AssistantFinalFrame,
    AutoApprovalQueryFrame,
    AutoApprovalSetModeFrame,
    AutoApprovalStateFrame,
    CellEvictedFrame,
    CheckSessionStatusFrame,
    CodexC2SAdapter,
    CodexCommandFrame,
    CodexCommandOptions,
    CodexS2CAdapter,
    ContentDeltaFrame,
    ErrorFrame,
    InterruptFrame,
    PendingInputDTO,
    PendingInputSendNowFrame,
    PendingInputSteeredFrame,
    PingFrame,
    PongFrame,
    ReasoningDeltaFrame,
    RunInterruptedFrame,
    SessionStatusFrame,
    SystemNoticeFrame,
    ThreadHistoryFrame,
    ThreadStatusC2SAdapter,
    ThreadStatusFrame,
    ThreadStatusS2CAdapter,
    ThreadStatusSnapshotFrame,
    ToolCallEndFrame,
    ToolCallStartFrame,
    TurnEndFrame,
    TurnStartFrame,
    UsageFrame,
    UsageSummaryUpdatedFrame,
    UserInputFrame,
    WSFrameC2SAdapter,
    WSFrameS2CAdapter,
)


def test_thread_subagent_rest_wrapper_round_trip_and_strictness() -> None:
    """Python 真源固定 wrapper 必填字段、未知字段与状态枚举。"""
    dto = _thread_subagent_list()
    restored = ThreadSubAgentListDTO.model_validate_json(dto.model_dump_json())

    assert restored == dto
    with pytest.raises(ValidationError):
        ThreadSubAgentListDTO.model_validate({"thread_id": dto.thread_id, "subagents": []})
    with pytest.raises(ValidationError):
        ThreadSubAgentListDTO.model_validate(
            {
                **dto.model_dump(),
                "legacy": [],
            }
        )
    bad_status = dto.model_dump()
    bad_status["subagents"][0]["status"] = "unknown"
    with pytest.raises(ValidationError):
        ThreadSubAgentListDTO.model_validate(bad_status)


def test_scheduler_rest_dto_round_trip_and_strictness() -> None:
    """Scheduler Python 真源固定生命周期、run 状态和未知字段边界。"""
    task = CronTaskDTO(
        task_id="task-1",
        name="daily",
        lifecycle="scheduled",
        latest_run_status="failed",
        live_runtime_status="idle",
        trigger_type="cron",
        trigger_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        next_run_at="2026-07-30T01:00:00Z",
        last_run_at="2026-07-29T01:00:05Z",
        thread_id="thread-abcdef012345",
        preset_id="preset-a",
        created_by="user",
        input_text="daily summary",
        agent_name="default",
    )
    run = CronRunDTO(
        run_id="run-1",
        task_id="task-1",
        task_name="daily",
        session_id="session-1",
        thread_id="thread-abcdef012345",
        scheduled_for="2026-07-29T01:00:00Z",
        started_at="2026-07-29T01:00:01Z",
        finished_at="2026-07-29T01:00:05Z",
        status="failed",
        failure_reason="runner_exception",
        final_message_excerpt=None,
        delivery_status="failed",
        delivery_error="offline",
    )

    assert CronTaskDTO.model_validate_json(task.model_dump_json()) == task
    assert CronRunDTO.model_validate_json(run.model_dump_json()) == run
    assert CreateCronTaskRequest.model_validate(
        {
            "name": "daily",
            "agent_name": "default",
            "input_text": "summary",
            "schedule_type": "cron",
        }
    ) == CreateCronTaskRequest(
        name="daily",
        agent_name="default",
        input_text="summary",
        schedule_type="cron",
        timezone="UTC",
        concurrency_policy="forbid",
    )
    assert UpdateCronTaskRequest.model_validate({"name": None, "preset_id": None}).model_dump(
        exclude_unset=True
    ) == {"name": None, "preset_id": None}
    assert CronRunMessagesResponse(messages=[]).model_dump() == {"messages": []}
    normalized_messages = CronRunMessagesResponse(
        messages=[
            {
                "id": "message-1",
                "provider": "generic_chat",
                "frame_type": "text",
                "content": "hello",
            }
        ]
    )
    assert normalized_messages.messages[0]["frame_type"] == "text"
    assert CronRunsPage(runs=[run], next_cursor=None).runs == [run]
    assert RunNowResponse(run_id="pending-1", status="PENDING").status == "PENDING"
    with pytest.raises(ValidationError):
        CronRunMessagesResponse(
            messages=[{"frame_type": "scheduler-private-message"}]  # type: ignore[list-item]
        )
    with pytest.raises(ValidationError):
        CronRunMessagesResponse(messages=[{"content": "missing discriminant"}])  # type: ignore[list-item]
    with pytest.raises(ValidationError):
        RunNowResponse.model_validate({"run_id": "pending-1"})
    with pytest.raises(ValidationError):
        CronTaskDTO.model_validate({**task.model_dump(), "state": "failed"})
    with pytest.raises(ValidationError):
        CronRunDTO.model_validate({**run.model_dump(), "status": "success"})
    with pytest.raises(ValidationError):
        UpdateCronTaskRequest.model_validate({"enabled": False})


# ---------------------------------------------------------------------------
# Fixtures：构造器返回 (instance, expected_frame_type) 对
# ---------------------------------------------------------------------------


def _thread_subagent_list() -> ThreadSubAgentListDTO:
    """构造严格 wrapper，覆盖完整 child identity 与 lifecycle 字段。"""
    return ThreadSubAgentListDTO(
        schema_version=1,
        thread_id="thread-abcdef012345",
        subagents=[
            ThreadSubAgentItemDTO(
                id="task-1",
                agent_id="child-1",
                thread_id="thread-abcdef012345",
                source="workflow",
                workflow_id="wf-1",
                workflow_task_id="step-1",
                task_id="task-1",
                task_run_id="run-1",
                task_name="Step 1",
                session_id="session-1",
                status="running",
                started_at="2026-07-29T00:00:00Z",
                updated_at="2026-07-29T00:00:01Z",
                finished_at=None,
                started_at_ms=1,
                updated_at_ms=2,
                finished_at_ms=None,
                error_message=None,
            )
        ],
    )


def _make_user_input() -> UserInputFrame:
    return UserInputFrame(text="hello", request_id="req-1")


def _make_ping() -> PingFrame:
    return PingFrame()


def _make_interrupt() -> InterruptFrame:
    return InterruptFrame(run_id="run-thread-abc-1")


def _make_pending_input_send_now() -> PendingInputSendNowFrame:
    return PendingInputSendNowFrame(pending_input_id="pin-1", request_id="send-now-1")


def _make_pending_input_steered() -> PendingInputSteeredFrame:
    return PendingInputSteeredFrame(
        timestamp_ms=1_700_000_000_010,
        thread_id="thread-abcdef012345",
        pending_input_id="pin-1",
        pending_input=PendingInputDTO(
            id="pin-1",
            thread_id="thread-abcdef012345",
            source="user_input",
            priority="user_message",
            content="send now",
            preview="send now",
            status="starting",
            created_at_ms=1_700_000_000_001,
            updated_at_ms=1_700_000_000_002,
            sequence=1,
        ),
        active_run_id="run-1",
        run_id="run-1",
        turn=2,
        version=2,
    )


def _make_run_interrupted() -> RunInterruptedFrame:
    return RunInterruptedFrame(
        timestamp_ms=1_700_000_000_099,
        run_id="run-thread-abc-1",
        cancelled_at_turn=2,
        cancelled_tool_call_id="call-x",
    )


def _make_thread_history() -> ThreadHistoryFrame:
    msg = {
        "id": "msg-1",
        "sessionId": None,
        "timestamp": "2026-06-04T00:00:00Z",
        "provider": "generic_chat",
        "frame_type": "text",
        "role": "user",
        "content": "hi",
    }
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
        turn=1,
        run_id="run-xxx",
        usage={
            "provider": "claude",
            "input_tokens": 6,
            "output_tokens": 881,
            "cache_read_input_tokens": 341086,
            "cache_creation_input_tokens": 431,
            "cache_creation": {
                "ephemeral_1h_input_tokens": 431,
                "ephemeral_5m_input_tokens": 0,
            },
            "context_usage": 341523,
            "model": "claude-opus-4",
            "context_window": 1_000_000,
        },
    )


def _make_usage_summary_updated() -> UsageSummaryUpdatedFrame:
    return UsageSummaryUpdatedFrame(
        threadId="thread-aabbccddeeff",
        usage={
            "provider": "claude",
            "input_tokens": 6,
            "output_tokens": 881,
            "cache_read_input_tokens": 341086,
            "cache_creation_input_tokens": 431,
            "cache_creation": {
                "ephemeral_1h_input_tokens": 431,
                "ephemeral_5m_input_tokens": 0,
            },
            "context_usage": 341523,
            "model": "claude-opus-4",
            "context_window": 1_000_000,
        },
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


def _make_thread_status() -> ThreadStatusFrame:
    return ThreadStatusFrame(
        threadId="thread-aaaaaaaaaaaa",
        phase="idle",
        sequence=1,
        runId="run-1",
        runGeneration=1,
        run_end_reason=8,
    )


def _make_approval_inbox_item() -> ApprovalInboxItem:
    return ApprovalInboxItem(
        requestId="req-1",
        threadId="thread-aaaaaaaaaaaa",
        toolName="Bash",
        toolInput={"command": "ls"},
        blockedByRule=None,
        isElevated=False,
        channel="generic_chat",
        cwd="/proj/x",
        arrivedAtMs=1_700_000_000_016,
        timeoutMs=60_000,
        rememberRule=None,
        danger=True,
        rememberAllowed=False,
    )


def _make_approval_inbox_add() -> ApprovalInboxAddFrame:
    return ApprovalInboxAddFrame.model_validate(
        {"frame_type": "approval.inbox.add", **_make_approval_inbox_item().model_dump()},
    )


def _make_approval_inbox_remove() -> ApprovalInboxRemoveFrame:
    return ApprovalInboxRemoveFrame(requestId="req-1", reason="timeout")


def _make_approval_inbox_snapshot() -> ApprovalInboxSnapshotFrame:
    return ApprovalInboxSnapshotFrame(items=[_make_approval_inbox_item()])


def _make_approval_inbox_resolve() -> ApprovalInboxResolveFrame:
    return ApprovalInboxResolveFrame(
        threadId="thread-aaaaaaaaaaaa",
        requestId="req-1",
        allow=True,
        remember=False,
    )


def _make_approval_inbox_resolve_result() -> ApprovalInboxResolveResultFrame:
    return ApprovalInboxResolveResultFrame(
        requestId="req-1",
        accepted=False,
        message="规则保存失败，请重试",
    )


def _make_auto_approval_set_mode() -> AutoApprovalSetModeFrame:
    return AutoApprovalSetModeFrame(cwd="/proj/x", mode="llm")


def _make_auto_approval_query() -> AutoApprovalQueryFrame:
    return AutoApprovalQueryFrame(cwd="/proj/x")


def _make_auto_approval_state() -> AutoApprovalStateFrame:
    return AutoApprovalStateFrame(
        channel="generic_chat",
        cwd="/proj/x",
        mode="llm",
        timeoutMs=10_000,
        ruleOverrides={"safe_bash": True},
    )


def _make_abort_session() -> AbortSessionFrame:
    return AbortSessionFrame(sessionId="session-1", provider="codex")


def _make_check_session_status() -> CheckSessionStatusFrame:
    return CheckSessionStatusFrame(sessionId="session-1")


def _make_session_status() -> SessionStatusFrame:
    return SessionStatusFrame(sessionId="session-1", isProcessing=True)


def _make_codex_command() -> CodexCommandFrame:
    return CodexCommandFrame(
        command="explain this repo",
        options=CodexCommandOptions(
            cwd="/proj/x",
            permissionMode="acceptEdits",
            reasoningEffort="medium",
        ),
    )


# REST DTO factories --------------------------------------------------------


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
        forked_from_id="thread-111111111111",
        forked_from_history_index=2,
        # v13 (完整对话 fork 时间线定位): 加 forked_from_history_index
        schema_version=13,
    )


def _make_create_thread_request() -> CreateThreadRequest:
    return CreateThreadRequest(name="new chat", preset_id="preset-default")


def _make_create_generic_thread_from_first_message_request() -> (
    CreateGenericThreadFromFirstMessageRequest
):
    return CreateGenericThreadFromFirstMessageRequest(
        text="hello",
        preset_id="preset-default",
        cwd="/tmp/project-a",
        reasoning_effort="medium",
    )


def _make_create_generic_thread_from_first_message_response() -> (
    CreateGenericThreadFromFirstMessageResponse
):
    return CreateGenericThreadFromFirstMessageResponse(thread=_make_thread_metadata())


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


def _make_thread_permissions() -> ThreadPermissionsDTO:
    """构造 thread permissions REST 快照。"""
    return ThreadPermissionsDTO(
        thread_id="thread-abcdef012345",
        revision=2,
        allow=[{"expression": "read_file", "scope_cwd": None}],
        deny=[{"expression": "run_shell(curl:*)", "scope_cwd": None}],
        updated_at="2026-07-16T08:00:00Z",
    )


def _make_update_thread_permissions() -> UpdateThreadPermissionsRequest:
    """构造带 revision CAS 的整本替换请求。"""
    return UpdateThreadPermissionsRequest(
        thread_id="thread-abcdef012345",
        revision=2,
        allow=[{"expression": "read_file", "scope_cwd": None}],
        deny=[],
    )


# ---------------------------------------------------------------------------
# Round-trip 参数化：WS 帧
# ---------------------------------------------------------------------------


WS_FRAME_FACTORIES = [
    pytest.param(_make_user_input, id="user_input_frame"),
    pytest.param(_make_ping, id="ping_frame"),
    pytest.param(_make_interrupt, id="interrupt_frame"),
    pytest.param(_make_pending_input_send_now, id="pending_input_send_now_frame"),
    pytest.param(_make_pending_input_steered, id="pending_input_steered_frame"),
    pytest.param(_make_thread_history, id="thread_history_frame"),
    pytest.param(_make_assistant_final, id="assistant_final_frame"),
    pytest.param(_make_content_delta, id="content_delta_frame"),
    pytest.param(_make_reasoning_delta, id="reasoning_delta_frame"),
    pytest.param(_make_tool_call_start, id="tool_call_start_frame"),
    pytest.param(_make_tool_call_end, id="tool_call_end_frame"),
    pytest.param(_make_approval_decision, id="approval_decision_frame"),
    pytest.param(_make_usage, id="usage_frame"),
    pytest.param(_make_usage_summary_updated, id="usage_summary_updated_frame"),
    pytest.param(_make_error, id="error_frame"),
    pytest.param(_make_turn_start, id="turn_start_frame"),
    pytest.param(_make_turn_end, id="turn_end_frame"),
    pytest.param(_make_pong, id="pong_frame"),
    pytest.param(_make_system_notice, id="system_notice_frame"),
    pytest.param(_make_cell_evicted, id="cell_evicted_frame"),
    pytest.param(_make_run_interrupted, id="run_interrupted_frame"),
    pytest.param(_make_thread_status, id="thread_status_frame"),
    pytest.param(_make_approval_inbox_item, id="approval_inbox_item"),
    pytest.param(_make_approval_inbox_add, id="approval_inbox_add_frame"),
    pytest.param(_make_approval_inbox_remove, id="approval_inbox_remove_frame"),
    pytest.param(_make_approval_inbox_snapshot, id="approval_inbox_snapshot_frame"),
    pytest.param(_make_approval_inbox_resolve, id="approval_inbox_resolve_frame"),
    pytest.param(
        _make_approval_inbox_resolve_result,
        id="approval_inbox_resolve_result_frame",
    ),
    pytest.param(_make_auto_approval_set_mode, id="auto_approval_set_mode_frame"),
    pytest.param(_make_auto_approval_query, id="auto_approval_query_frame"),
    pytest.param(_make_auto_approval_state, id="auto_approval_state_frame"),
    pytest.param(_make_abort_session, id="abort_session_frame"),
    pytest.param(_make_check_session_status, id="check_session_status_frame"),
    pytest.param(_make_session_status, id="session_status_frame"),
    pytest.param(_make_codex_command, id="codex_command_frame"),
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
    pytest.param(_make_thread_metadata, id="thread_metadata_dto"),
    pytest.param(_make_create_thread_request, id="create_thread_request"),
    pytest.param(
        _make_create_generic_thread_from_first_message_request,
        id="create_generic_thread_from_first_message_request",
    ),
    pytest.param(
        _make_create_generic_thread_from_first_message_response,
        id="create_generic_thread_from_first_message_response",
    ),
    pytest.param(_make_rename_thread_request, id="rename_thread_request"),
    pytest.param(_make_llm_preset, id="llm_preset_dto"),
    pytest.param(_make_login_request, id="login_request"),
    pytest.param(_make_cell_summary, id="cell_summary_dto"),
    pytest.param(_make_error_response, id="error_response_dto"),
    pytest.param(_make_thread_permissions, id="thread_permissions_dto"),
    pytest.param(
        _make_update_thread_permissions,
        id="update_thread_permissions_request",
    ),
]


@pytest.mark.parametrize("factory", REST_DTO_FACTORIES)
def test_round_trip_rest_dto(factory):
    """每个 REST DTO 最小有效实例 → JSON → 还原 → 等价。"""
    original = factory()
    json_blob = original.model_dump_json()
    reconstructed = type(original).model_validate_json(json_blob)
    assert original == reconstructed


def test_permissions_and_remember_contract_reject_legacy_fields() -> None:
    """v0.6 DTO 严格拒绝旧 scope 与未知 permissions 字段。"""
    with pytest.raises(ValidationError):
        ApprovalInboxResolveFrame.model_validate(
            {
                "threadId": "thread-abcdef012345",
                "requestId": "req-1",
                "allow": True,
                "remember": True,
                "rememberScope": "session",
            }
        )
    with pytest.raises(ValidationError):
        UpdateThreadPermissionsRequest.model_validate(
            {
                "thread_id": "thread-abcdef012345",
                "revision": 0,
                "allow": [],
                "deny": [],
                "scope": "global",
            }
        )


# ---------------------------------------------------------------------------
# Adapter 分派：C2S（3 帧）
# ---------------------------------------------------------------------------


C2S_DISPATCH_CASES = [
    pytest.param(
        {"frame_type": "user.input", "text": "hi", "request_id": "r-1"},
        UserInputFrame,
        id="user_input_dispatch",
    ),
    pytest.param({"frame_type": "ping"}, PingFrame, id="ping_dispatch"),
    pytest.param({"frame_type": "interrupt"}, InterruptFrame, id="interrupt_dispatch"),
    pytest.param(
        {
            "frame_type": "pending-input.send-now",
            "pending_input_id": "pin-1",
            "request_id": "send-now-1",
        },
        PendingInputSendNowFrame,
        id="pending_input_send_now_dispatch",
    ),
    pytest.param(
        {"frame_type": "interrupt", "run_id": "run-abc-1"},
        InterruptFrame,
        id="interrupt_with_run_id_dispatch",
    ),
    pytest.param(
        {"frame_type": "auto-approval-set-mode", "cwd": "/proj/x", "mode": "llm"},
        AutoApprovalSetModeFrame,
        id="auto_approval_set_mode_dispatch",
    ),
    pytest.param(
        {"frame_type": "auto-approval-query", "cwd": "/proj/x"},
        AutoApprovalQueryFrame,
        id="auto_approval_query_dispatch",
    ),
]


@pytest.mark.parametrize("payload, expected_cls", C2S_DISPATCH_CASES)
def test_ws_c2s_adapter_dispatch(payload, expected_cls):
    """``WSFrameC2SAdapter`` 按 ``frame_type`` 分派到正确帧类。"""
    obj = WSFrameC2SAdapter.validate_python(payload)
    assert isinstance(obj, expected_cls)
    assert obj.frame_type == payload["frame_type"]


# ---------------------------------------------------------------------------
# Adapter 分派：S2C（15 帧）
# ---------------------------------------------------------------------------


S2C_DISPATCH_CASES = [
    pytest.param(
        {
            "frame_type": "thread.history",
            "timestamp_ms": 1,
            "messages": [],
        },
        ThreadHistoryFrame,
        id="thread_history_dispatch",
    ),
    pytest.param(
        {
            "frame_type": "assistant.final",
            "timestamp_ms": 1,
            "content": "x",
            "turn": 1,
        },
        AssistantFinalFrame,
        id="assistant_final_dispatch",
    ),
    pytest.param(
        {
            "frame_type": "content.delta",
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
            "frame_type": "reasoning.delta",
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
            "frame_type": "tool.call.start",
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
            "frame_type": "tool.call.end",
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
            "frame_type": "approval.decision",
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
            "frame_type": "usage",
            "timestamp_ms": 1,
            "turn": 1,
            "usage": {
                "provider": "openai",
                "last": {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cached_input_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "total_tokens": 3,
                },
                "model": "",
                "context_window": 0,
            },
        },
        UsageFrame,
        id="usage_dispatch",
    ),
    pytest.param(
        {
            "frame_type": "usage_summary_updated",
            "threadId": "thread-aabbccddeeff",
            "usage": {"provider": "openai", "total": {"input_tokens": 1}},
        },
        UsageSummaryUpdatedFrame,
        id="usage_summary_updated_dispatch",
    ),
    pytest.param(
        {
            "frame_type": "error",
            "timestamp_ms": 1,
            "error_code": "network",
            "message": "m",
        },
        ErrorFrame,
        id="error_dispatch",
    ),
    pytest.param(
        {"frame_type": "turn.start", "timestamp_ms": 1, "turn": 1},
        TurnStartFrame,
        id="turn_start_dispatch",
    ),
    pytest.param(
        {"frame_type": "turn.end", "timestamp_ms": 1, "turn": 1},
        TurnEndFrame,
        id="turn_end_dispatch",
    ),
    pytest.param(
        {
            "frame_type": "pending-input.steered",
            "timestamp_ms": 1,
            "thread_id": "thread-abcdef012345",
            "pending_input_id": "pin-1",
            "pending_input": {
                "id": "pin-1",
                "thread_id": "thread-abcdef012345",
                "source": "user_input",
                "priority": "user_message",
                "content": "send now",
                "preview": "send now",
                "status": "starting",
                "created_at_ms": 1,
                "updated_at_ms": 2,
                "sequence": 1,
                "metadata": {},
            },
            "active_run_id": "run-1",
            "run_id": "run-1",
            "turn": 2,
            "version": 2,
        },
        PendingInputSteeredFrame,
        id="pending_input_steered_dispatch",
    ),
    pytest.param(
        {"frame_type": "pong", "timestamp_ms": 1},
        PongFrame,
        id="pong_dispatch",
    ),
    pytest.param(
        {
            "frame_type": "system.notice",
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
            "frame_type": "cell.evicted",
            "timestamp_ms": 1,
            "thread_id": "thread-abcdef012345",
            "reason": "manual_stop",
        },
        CellEvictedFrame,
        id="cell_evicted_dispatch",
    ),
    pytest.param(
        {
            "frame_type": "auto_approval_state",
            "channel": "generic_chat",
            "cwd": "/proj/x",
            "mode": "llm",
            "timeoutMs": 10_000,
            "ruleOverrides": {},
        },
        AutoApprovalStateFrame,
        id="auto_approval_state_dispatch",
    ),
]


@pytest.mark.parametrize("payload, expected_cls", S2C_DISPATCH_CASES)
def test_ws_s2c_adapter_dispatch(payload, expected_cls):
    """``WSFrameS2CAdapter`` 按 ``frame_type`` 分派到正确帧类。"""
    obj = WSFrameS2CAdapter.validate_python(payload)
    assert isinstance(obj, expected_cls)
    assert obj.frame_type == payload["frame_type"]


THREAD_STATUS_C2S_DISPATCH_CASES = [
    pytest.param({"frame_type": "ping", "ts": 1}, PingFrame, id="thread_status_ping"),
    pytest.param(
        {
            "frame_type": "approval.inbox.resolve",
            "threadId": "thread-aaaaaaaaaaaa",
            "requestId": "req-1",
            "allow": True,
        },
        ApprovalInboxResolveFrame,
        id="thread_status_approval_inbox_resolve",
    ),
]


@pytest.mark.parametrize("payload, expected_cls", THREAD_STATUS_C2S_DISPATCH_CASES)
def test_thread_status_c2s_adapter_dispatch(payload, expected_cls):
    """``ThreadStatusC2SAdapter`` 只接受 thread-status 通道入站帧。"""
    obj = ThreadStatusC2SAdapter.validate_python(payload)
    assert isinstance(obj, expected_cls)
    assert obj.frame_type == payload["frame_type"]


THREAD_STATUS_S2C_DISPATCH_CASES = [
    pytest.param(
        {"frame_type": "pong", "timestamp_ms": 1, "ts": 1},
        PongFrame,
        id="thread_status_pong",
    ),
    pytest.param(
        {
            "frame_type": "thread-status",
            "threadId": "thread-aaaaaaaaaaaa",
            "phase": "idle",
            "sequence": 1,
            "runId": "run-1",
            "runGeneration": 1,
            "run_end_reason": 8,
        },
        ThreadStatusFrame,
        id="thread_status_phase",
    ),
    pytest.param(
        {
            "frame_type": "thread-status.snapshot",
            "watermark": 1,
            "items": [
                {
                    "frame_type": "thread-status",
                    "threadId": "thread-aaaaaaaaaaaa",
                    "phase": "responding",
                    "sequence": 1,
                    "runId": "run-1",
                    "runGeneration": 1,
                }
            ],
        },
        ThreadStatusSnapshotFrame,
        id="thread_status_snapshot",
    ),
    pytest.param(
        {
            "frame_type": "approval.inbox.add",
            **_make_approval_inbox_item().model_dump(),
        },
        ApprovalInboxAddFrame,
        id="thread_status_approval_inbox_add",
    ),
    pytest.param(
        {
            "frame_type": "approval.inbox.remove",
            "requestId": "req-1",
            "reason": "cancelled",
        },
        ApprovalInboxRemoveFrame,
        id="thread_status_approval_inbox_remove",
    ),
    pytest.param(
        {
            "frame_type": "approval.inbox.snapshot",
            "items": [_make_approval_inbox_item().model_dump()],
        },
        ApprovalInboxSnapshotFrame,
        id="thread_status_approval_inbox_snapshot",
    ),
    pytest.param(
        {
            "frame_type": "approval.inbox.resolve_result",
            "requestId": "req-1",
            "accepted": False,
            "message": "规则保存失败，请重试",
        },
        ApprovalInboxResolveResultFrame,
        id="thread_status_approval_inbox_resolve_result",
    ),
]


@pytest.mark.parametrize("payload, expected_cls", THREAD_STATUS_S2C_DISPATCH_CASES)
def test_thread_status_s2c_adapter_dispatch(payload, expected_cls):
    """``ThreadStatusS2CAdapter`` 按全局 WS 通道帧集合分派。"""
    obj = ThreadStatusS2CAdapter.validate_python(payload)
    assert isinstance(obj, expected_cls)
    assert obj.frame_type == payload["frame_type"]


def test_thread_status_terminal_fixture_matches_pydantic_serialization() -> None:
    """共享 terminal fixture 必须等于 Python Pydantic 的真实出站 shape。"""
    fixture_path = (
        Path(__file__).parents[1] / "fixtures" / "web_protocol" / "thread-status-terminal.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    frame = ThreadStatusFrame(
        threadId="thread-terminal-fixture",
        phase="idle",
        sequence=9,
        runId="run-terminal",
        runGeneration=3,
        run_end_reason=8,
    )

    assert frame.model_dump(exclude_none=True) == fixture


def test_thread_status_rejects_legacy_camel_case_reason() -> None:
    """旧 camelCase 字段必须被 extra=forbid 拒绝，避免形成双读协议。"""
    with pytest.raises(ValidationError):
        ThreadStatusFrame.model_validate(
            {
                "frame_type": "thread-status",
                "threadId": "thread-terminal-fixture",
                "phase": "idle",
                "sequence": 9,
                "runId": "run-terminal",
                "runGeneration": 3,
                "runEndReason": 8,
            }
        )


def test_thread_status_optional_fields_follow_exclude_none_wire_shape() -> None:
    """普通状态帧省略两个 None 字段，显式 null 仍可由模型验证。"""
    frame = ThreadStatusFrame(
        threadId="thread-optional-fields",
        phase="responding",
        sequence=4,
        runId="run-optional",
        runGeneration=2,
    )

    assert frame.model_dump(exclude_none=True) == {
        "frame_type": "thread-status",
        "threadId": "thread-optional-fields",
        "phase": "responding",
        "sequence": 4,
        "runId": "run-optional",
        "runGeneration": 2,
    }
    validated = ThreadStatusFrame.model_validate(
        {
            "frame_type": "thread-status",
            "threadId": "thread-optional-fields",
            "phase": "responding",
            "sequence": 4,
            "runId": "run-optional",
            "runGeneration": 2,
            "toolName": None,
            "run_end_reason": None,
        }
    )
    assert validated.toolName is None
    assert validated.run_end_reason is None


CODEX_C2S_DISPATCH_CASES = [
    pytest.param(
        {"frame_type": "codex-command", "command": "hi"},
        CodexCommandFrame,
        id="codex_command",
    ),
    pytest.param(
        {"frame_type": "abort-session", "sessionId": "sid-1", "provider": "codex"},
        AbortSessionFrame,
        id="codex_abort_session",
    ),
    pytest.param(
        {"frame_type": "check-session-status", "sessionId": "sid-1"},
        CheckSessionStatusFrame,
        id="codex_check_session_status",
    ),
]


@pytest.mark.parametrize("payload, expected_cls", CODEX_C2S_DISPATCH_CASES)
def test_codex_c2s_adapter_dispatch(payload, expected_cls):
    """``CodexC2SAdapter`` 按 Codex 通道入站帧集合分派。"""
    obj = CodexC2SAdapter.validate_python(payload)
    assert isinstance(obj, expected_cls)
    assert obj.frame_type == payload["frame_type"]


CODEX_S2C_DISPATCH_CASES = [
    pytest.param(
        {"frame_type": "session-status", "sessionId": "sid-1", "isProcessing": False},
        SessionStatusFrame,
        id="codex_session_status",
    ),
    pytest.param(
        {
            "frame_type": "complete",
            "provider": "codex",
            "sessionId": "sid-1",
            "aborted": True,
        },
        dict,
        id="codex_normalized_complete",
    ),
]


@pytest.mark.parametrize("payload, expected_cls", CODEX_S2C_DISPATCH_CASES)
def test_codex_s2c_adapter_dispatch(payload, expected_cls):
    """``CodexS2CAdapter`` 接受 session-status 与 NormalizedMessage。"""
    obj = CodexS2CAdapter.validate_python(payload)
    assert isinstance(obj, expected_cls)
    if hasattr(obj, "frame_type"):
        assert obj.frame_type == payload["frame_type"]
    else:
        assert obj["frame_type"] == payload["frame_type"]


# ---------------------------------------------------------------------------
# frame_type / schema_version 默认值
# ---------------------------------------------------------------------------


def test_frame_type_default_user_input():
    assert UserInputFrame(text="x", request_id="r").frame_type == "user.input"


def test_frame_type_default_ping():
    assert PingFrame().frame_type == "ping"


def test_frame_type_default_thread_history():
    frame = ThreadHistoryFrame(timestamp_ms=1, messages=[])
    assert frame.frame_type == "thread.history"


def test_frame_type_default_assistant_final():
    frame = AssistantFinalFrame(timestamp_ms=1, content="x", turn=1)
    assert frame.frame_type == "assistant.final"


def test_frame_type_default_content_delta():
    frame = ContentDeltaFrame(timestamp_ms=1, delta="x", turn=1, seq=0)
    assert frame.frame_type == "content.delta"


def test_frame_type_default_reasoning_delta():
    frame = ReasoningDeltaFrame(timestamp_ms=1, delta="x", turn=1, seq=0)
    assert frame.frame_type == "reasoning.delta"


def test_frame_type_default_tool_call_start():
    frame = ToolCallStartFrame(timestamp_ms=1, tool_name="t", call_id="c", turn=1, arguments={})
    assert frame.frame_type == "tool.call.start"


def test_frame_type_default_tool_call_end():
    frame = ToolCallEndFrame(timestamp_ms=1, call_id="c", turn=1, ok=True)
    assert frame.frame_type == "tool.call.end"


def test_frame_type_default_approval_decision():
    frame = ApprovalDecisionFrame(timestamp_ms=1, call_id="c", outcome="approved", turn=1)
    assert frame.frame_type == "approval.decision"


def test_frame_type_default_usage():
    frame = UsageFrame(
        timestamp_ms=1,
        turn=1,
        usage={
            "provider": "openai",
            "last": {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens": 0,
            },
            "model": "",
            "context_window": 0,
        },
    )
    assert frame.frame_type == "usage"


def test_frame_type_default_usage_summary_updated():
    frame = UsageSummaryUpdatedFrame(
        threadId="thread-aabbccddeeff",
        usage={"provider": "openai", "total": {"input_tokens": 1}},
    )
    assert frame.frame_type == "usage_summary_updated"


def test_frame_type_default_error():
    frame = ErrorFrame(timestamp_ms=1, error_code="internal", message="m")
    assert frame.frame_type == "error"


def test_frame_type_default_turn_start():
    assert TurnStartFrame(timestamp_ms=1, turn=1).frame_type == "turn.start"


def test_frame_type_default_turn_end():
    assert TurnEndFrame(timestamp_ms=1, turn=1).frame_type == "turn.end"


def test_frame_type_default_pong():
    assert PongFrame(timestamp_ms=1).frame_type == "pong"


def test_frame_type_default_system_notice():
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
    assert frame.frame_type == "system.notice"


def test_frame_type_default_cell_evicted():
    frame = CellEvictedFrame(timestamp_ms=1, thread_id="thread-abcdef012345", reason="idle")
    assert frame.frame_type == "cell.evicted"


def test_thread_metadata_schema_version_default():
    """``ThreadMetadataDTO.schema_version`` 默认 ``13``（fork 时间线边界）。"""
    dto = ThreadMetadataDTO(
        id="thread-abcdef012345",
        name="x",
        preset_id="p",
        backend_kind="generic_chat",
        created_at=0.0,
        updated_at=0.0,
        message_count=0,
    )
    assert dto.schema_version == 13
