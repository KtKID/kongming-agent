"""WSEventSink 单测（Phase 1 #7）。

覆盖 :class:`web.websocket.event_sink.WSEventSink._translate` 的全部 dispatch
分支 + emit 容错 + attach_ws：

- content.delta / reasoning.delta（含 seq / turn 默认值）
- turn.start / turn.end
- tool.call.start / tool.call.end（含 ok=False / error_message）
- approval.decision（approved / rejected / cancelled / 未知 outcome 兜底）
- usage / error（含 error_type → ErrorCode 映射）
- 不识别 kind 静默丢（例：run.start / approval.request / memory.* / 自造）
- send 失败 → 标 closed + 不抛
- attach_ws 重置 closed
- closed 后 emit 静默丢

stub WS：``unittest.mock.AsyncMock``。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from core.contracts import Event
from hosts.web.websocket.event_sink import WSEventSink


def _make_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send_json = AsyncMock(return_value=None)
    ws.close = AsyncMock(return_value=None)
    return ws


# ---------------------------------------------------------------------------
# 流式增量
# ---------------------------------------------------------------------------


async def test_emit_content_delta() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="content.delta",
            run_id="r",
            turn=2,
            payload={"delta": "hello", "seq": 5},
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "content.delta"
    assert sent["delta"] == "hello"
    assert sent["turn"] == 2
    assert sent["seq"] == 5


async def test_emit_reasoning_delta() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="reasoning.delta",
            run_id="r",
            turn=3,
            payload={"delta": "thinking..."},
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "reasoning.delta"
    assert sent["delta"] == "thinking..."
    # seq 缺省 → 0
    assert sent["seq"] == 0


# ---------------------------------------------------------------------------
# turn 边界
# ---------------------------------------------------------------------------


async def test_emit_turn_start() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(Event(kind="turn.start", run_id="r", turn=1))
    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "turn.start"
    assert sent["turn"] == 1


async def test_emit_turn_end() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(Event(kind="turn.end", run_id="r", turn=1))
    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "turn.end"


# ---------------------------------------------------------------------------
# 工具调用
# ---------------------------------------------------------------------------


async def test_emit_tool_call_start() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="tool.call.start",
            run_id="r",
            turn=1,
            payload={
                "tool_name": "ReadFile",
                "call_id": "c1",
                "arguments": {"path": "/x"},
            },
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "tool.call.start"
    assert sent["tool_name"] == "ReadFile"
    assert sent["call_id"] == "c1"
    assert sent["arguments"] == {"path": "/x"}


async def test_emit_tool_call_end_success() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="tool.call.end",
            run_id="r",
            turn=1,
            payload={"call_id": "c1", "ok": True},
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "tool.call.end"
    assert sent["ok"] is True
    assert sent["error_message"] is None


async def test_emit_tool_call_end_business_failure_with_message() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="tool.call.end",
            run_id="r",
            turn=1,
            payload={
                "call_id": "c1",
                "ok": False,
                "error_message": "permission denied",
            },
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["ok"] is False
    assert sent["error_message"] == "permission denied"


async def test_emit_tool_call_end_passes_content_through() -> None:
    """v0.1.6: ``tool.call.end`` 必须把 ToolResult.content 写入 frame，
    否则 web UI 无法显示工具产出（之前 bug：UI 永远显示空 ``{}``）。
    """
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="tool.call.end",
            run_id="r",
            turn=1,
            payload={
                "call_id": "c1",
                "tool_name": "run_shell",
                "ok": True,
                "content": "stdout\nline2\n",
                "data": {"stdout": "stdout\nline2\n", "exit_code": 0},
            },
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "tool.call.end"
    assert sent["content"] == "stdout\nline2\n"
    assert sent["data"] == {"stdout": "stdout\nline2\n", "exit_code": 0}


async def test_emit_tool_call_end_missing_content_defaults_empty() -> None:
    """payload 缺 content/data 时 frame 用安全默认值（空串 / None），不抛异常。"""
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="tool.call.end",
            run_id="r",
            turn=1,
            payload={"call_id": "c1", "ok": True},
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["content"] == ""
    assert sent["data"] is None


async def test_emit_tool_call_end_non_dict_data_falls_to_none() -> None:
    """payload.data 不是 dict 时 frame 写 None（防御 sink 不崩）。"""
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="tool.call.end",
            run_id="r",
            turn=1,
            payload={"call_id": "c1", "ok": True, "content": "x", "data": "not a dict"},
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["content"] == "x"
    assert sent["data"] is None


async def test_emit_usage_includes_run_id() -> None:
    """v2：UsageFrame.usage 是 channel-specific DTO dict（含 provider discriminator）。

    无 ``provider_kind`` 时走 fallback openai 分支：``prompt_tokens`` → ``input_tokens``、
    ``completion_tokens`` → ``output_tokens``。
    """
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="usage",
            run_id="run-42",
            turn=3,
            payload={
                "prompt_tokens": 100,
                "completion_tokens": 25,
                "total_tokens": 125,
            },
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "usage"
    assert sent["run_id"] == "run-42"
    assert sent["turn"] == 3
    usage = sent["usage"]
    assert usage["provider"] == "openai"
    assert usage["last"]["input_tokens"] == 100
    assert usage["last"]["output_tokens"] == 25


# ---------------------------------------------------------------------------
# 审批决策
# ---------------------------------------------------------------------------


async def test_emit_approval_decision_approved() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="approval.decision",
            run_id="r",
            turn=1,
            payload={"call_id": "c1", "outcome": "approved"},
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "approval.decision"
    assert sent["outcome"] == "approved"


async def test_emit_approval_decision_rejected() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="approval.decision",
            run_id="r",
            turn=1,
            payload={"call_id": "c1", "outcome": "rejected"},
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["outcome"] == "rejected"


async def test_emit_approval_decision_cancelled() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="approval.decision",
            run_id="r",
            turn=1,
            payload={"call_id": "c1", "outcome": "cancelled"},
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["outcome"] == "cancelled"


async def test_emit_approval_decision_unknown_outcome_falls_back_to_cancelled() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="approval.decision",
            run_id="r",
            turn=1,
            payload={"call_id": "c1", "outcome": "weird-outcome"},
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["outcome"] == "cancelled"


# ---------------------------------------------------------------------------
# 用量
# ---------------------------------------------------------------------------


async def test_emit_usage() -> None:
    """v2：UsageFrame.usage 是 channel-specific DTO dict（含 provider）。"""
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="usage",
            run_id="r",
            turn=1,
            payload={
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
            },
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "usage"
    usage = sent["usage"]
    assert usage["provider"] == "openai"
    assert usage["last"]["input_tokens"] == 100
    assert usage["last"]["output_tokens"] == 50


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


async def test_emit_error_known_type_maps_to_code() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="error",
            run_id="r",
            turn=1,
            payload={"type": "NetworkError", "message": "DNS failed"},
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "error"
    assert sent["error_code"] == "network"
    assert sent["message"] == "DNS failed"
    assert sent["turn"] == 1


async def test_emit_error_unknown_type_falls_back_to_internal() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="error",
            run_id="r",
            turn=None,
            payload={"type": "WhateverError", "message": "boom"},
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["error_code"] == "internal"
    assert sent["turn"] is None


async def test_emit_error_provider_error_maps_to_llm_error() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="error",
            run_id="r",
            turn=1,
            payload={"type": "ProviderError", "message": "5xx"},
        )
    )
    assert ws.send_json.await_args.args[0]["error_code"] == "llm_error"


async def test_emit_error_tool_error_maps() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="error",
            run_id="r",
            turn=1,
            payload={"type": "ToolError", "message": "x"},
        )
    )
    assert ws.send_json.await_args.args[0]["error_code"] == "tool_error"


async def test_emit_error_approval_timeout_maps() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="error",
            run_id="r",
            turn=1,
            payload={"type": "ApprovalTimeout", "message": "timeout"},
        )
    )
    assert ws.send_json.await_args.args[0]["error_code"] == "approval_timeout"


# ---------------------------------------------------------------------------
# self-evolution system notice
# ---------------------------------------------------------------------------


async def test_emit_evolution_review_started_notice() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="evolution.review.started",
            run_id="run-parent-1",
            payload={
                "review_id": "evo-review:run-parent-1",
                "session_id": "sess-1",
                "user_turn_count": 6,
                "included_turns": [3, 4, 5],
                "timeout_seconds": 30.0,
            },
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "system.notice"
    assert sent["notice_key"] == "self_evolution.review"
    assert sent["source"] == "self_evolution"
    assert sent["status"] == "started"
    assert sent["title"] == "进化复盘"
    assert sent["message"] == "后台复盘中"
    assert sent["icon"] == "running"
    assert sent["run_id"] == "run-parent-1"
    assert sent["details"] == {
        "review_id": "evo-review:run-parent-1",
        "session_id": "sess-1",
        "user_turn_count": 6,
        "included_turns": [3, 4, 5],
        "timeout_seconds": 30.0,
    }


async def test_emit_evolution_review_completed_notice() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="evolution.review.completed",
            run_id="run-parent-2",
            payload={
                "review_id": "evo-review:run-parent-2",
                "review_run_id": "run-child-9",
                "session_id": "sess-2",
                "write_status": "written",
                "duration_ms": 3456,
                "timeout_hit": False,
                "timeout_seconds": 30.0,
                "nutrients_written": 2,
                "written_nutrient_ids": ["n-1", "n-2"],
                "review_summary": "Git 提交规范和测试隔离经验",
                "nutrient_summaries": ["commit 前验证测试", "路径参数需 resolve"],
            },
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "system.notice"
    assert sent["status"] == "completed"
    assert sent["title"] == "进化复盘"
    assert (
        sent["message"] == "Git 提交规范和测试隔离经验\n• commit 前验证测试\n• 路径参数需 resolve"
    )
    assert sent["icon"] == "success"
    assert sent["details"] == {
        "review_id": "evo-review:run-parent-2",
        "review_run_id": "run-child-9",
        "session_id": "sess-2",
        "write_status": "written",
        "duration_ms": 3456,
        "timeout_hit": False,
        "timeout_seconds": 30.0,
        "nutrients_written": 2,
        "written_nutrient_ids": ["n-1", "n-2"],
        "review_summary": "Git 提交规范和测试隔离经验",
        "nutrient_summaries": ["commit 前验证测试", "路径参数需 resolve"],
    }


async def test_emit_evolution_review_failed_notice() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="evolution.review.failed",
            run_id="run-parent-3",
            payload={
                "review_id": "evo-review:run-parent-3",
                "session_id": "sess-3",
                "error_kind": "RuntimeError",
                "message": "write path failed",
                "duration_ms": 30005,
                "timeout_hit": True,
                "timeout_seconds": 30.0,
            },
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "system.notice"
    assert sent["status"] == "failed"
    assert sent["title"] == "进化复盘"
    assert sent["message"] == "本轮未写入：write path failed"
    assert sent["icon"] == "error"
    assert sent["details"] == {
        "review_id": "evo-review:run-parent-3",
        "session_id": "sess-3",
        "error_kind": "RuntimeError",
        "error_message": "write path failed",
        "duration_ms": 30005,
        "timeout_hit": True,
        "timeout_seconds": 30.0,
    }


async def test_emit_evolution_review_drain_timeout_notice() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="evolution.review.drain_timeout",
            run_id="runtime-close",
            payload={
                "pending_review_ids": ["evo-review:run-1", "evo-review:run-2"],
                "timeout_seconds": 3.5,
            },
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "system.notice"
    assert sent["status"] == "drain_timeout"
    assert sent["title"] == "进化复盘"
    assert sent["message"] == "关闭前复盘超时，仍有 2 条待处理复盘"
    assert sent["icon"] == "warning"
    assert sent["details"] == {
        "pending_review_ids": ["evo-review:run-1", "evo-review:run-2"],
        "timeout_seconds": 3.5,
    }


# ---------------------------------------------------------------------------
# 静默丢弃未识别 kind
# ---------------------------------------------------------------------------


async def test_emit_unknown_kind_silently_dropped() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(Event(kind="run.start", run_id="r"))
    ws.send_json.assert_not_called()


async def test_emit_approval_request_silently_dropped() -> None:
    """approval.request 由 host adapter 推，sink 不重复推。"""
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="approval.request",
            run_id="r",
            turn=1,
            payload={"tool_name": "x"},
        )
    )
    ws.send_json.assert_not_called()


async def test_emit_memory_event_silently_dropped() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(Event(kind="memory.snapshot.refreshed", run_id="r"))
    ws.send_json.assert_not_called()


async def test_emit_tool_silently_allowed_dropped() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="tool.silently_allowed",
            run_id="r",
            turn=1,
            payload={"tool_name": "ReadFile"},
        )
    )
    ws.send_json.assert_not_called()


# ---------------------------------------------------------------------------
# send 失败容错
# ---------------------------------------------------------------------------


async def test_emit_swallows_send_exception_and_marks_closed() -> None:
    ws = _make_ws()
    ws.send_json = AsyncMock(side_effect=ConnectionError("ws gone"))
    sink = WSEventSink(ws)
    await sink.emit(Event(kind="turn.start", run_id="r", turn=1))
    assert sink.closed is True
    # 后续 emit 静默丢
    await sink.emit(Event(kind="turn.end", run_id="r", turn=1))


async def test_emit_after_close_silently_drops() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    # 模拟先因 send 失败标 closed
    ws.send_json = AsyncMock(side_effect=RuntimeError())
    await sink.emit(Event(kind="turn.start", run_id="r", turn=1))
    ws.send_json.reset_mock()
    # closed 后再 emit 不会调用 send_json
    ws.send_json = AsyncMock(return_value=None)  # 重置，但 sink 仍 closed
    sink._ws = ws  # 保持 ws 引用
    await sink.emit(Event(kind="turn.end", run_id="r", turn=1))
    ws.send_json.assert_not_called()


# ---------------------------------------------------------------------------
# attach_ws 重置 closed
# ---------------------------------------------------------------------------


async def test_attach_ws_replaces_and_resets_closed() -> None:
    old_ws = _make_ws()
    old_ws.send_json = AsyncMock(side_effect=ConnectionError())
    sink = WSEventSink(old_ws)
    await sink.emit(Event(kind="turn.start", run_id="r", turn=1))
    assert sink.closed is True

    new_ws = _make_ws()
    sink.attach_ws(new_ws)
    assert sink.closed is False
    await sink.emit(Event(kind="turn.start", run_id="r", turn=1))
    new_ws.send_json.assert_awaited_once()


# ---------------------------------------------------------------------------
# 边界：payload 缺字段时不抛
# ---------------------------------------------------------------------------


async def test_emit_content_delta_with_empty_payload_uses_defaults() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(Event(kind="content.delta", run_id="r", turn=None, payload={}))
    sent = ws.send_json.await_args.args[0]
    assert sent["delta"] == ""
    assert sent["turn"] == 0
    assert sent["seq"] == 0


async def test_emit_tool_call_start_with_no_arguments() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="tool.call.start",
            run_id="r",
            turn=1,
            payload={"tool_name": "X", "call_id": "c"},
        )
    )
    sent = ws.send_json.await_args.args[0]
    # arguments 缺失 → 空 dict
    assert sent["arguments"] == {}


async def test_emit_usage_with_missing_fields_defaults_to_zero() -> None:
    """v2：空 payload → usage DTO 全 0；走 fallback openai provider 分支。"""
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(Event(kind="usage", run_id="r", turn=1, payload={}))
    sent = ws.send_json.await_args.args[0]
    usage = sent["usage"]
    assert usage["provider"] == "openai"
    assert usage["last"]["input_tokens"] == 0
    assert usage["last"]["output_tokens"] == 0
    assert usage["last"]["total_tokens"] == 0


# ---------------------------------------------------------------------------
# run_id 透传：7 个 S2C 帧应当从 Event.run_id 复制到 frame.run_id
# ---------------------------------------------------------------------------


async def test_run_id_propagates_to_content_delta() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="content.delta",
            run_id="run-sess-3",
            turn=1,
            payload={"delta": "hi"},
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["run_id"] == "run-sess-3"


async def test_run_id_propagates_to_reasoning_delta() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="reasoning.delta",
            run_id="run-r-2",
            turn=1,
            payload={"delta": "thinking"},
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["run_id"] == "run-r-2"


async def test_run_id_propagates_to_turn_start_and_end() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(Event(kind="turn.start", run_id="run-t-1", turn=1))
    await sink.emit(Event(kind="turn.end", run_id="run-t-1", turn=1))
    sent_frames = [
        c.args[0]
        for c in ws.send_json.await_args_list
        if c.args[0]["frame_type"] in ("turn.start", "turn.end")
    ]
    assert len(sent_frames) == 2
    assert all(s["run_id"] == "run-t-1" for s in sent_frames)


async def test_run_id_propagates_to_tool_call_start_and_end() -> None:
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="tool.call.start",
            run_id="run-tc-7",
            turn=1,
            payload={"tool_name": "X", "call_id": "c1"},
        )
    )
    await sink.emit(
        Event(
            kind="tool.call.end",
            run_id="run-tc-7",
            turn=1,
            payload={"call_id": "c1", "ok": True, "content": "out"},
        )
    )
    sent_kinds = {
        c.args[0]["frame_type"]: c.args[0]["run_id"] for c in ws.send_json.await_args_list
    }
    assert sent_kinds.get("tool.call.start") == "run-tc-7"
    assert sent_kinds.get("tool.call.end") == "run-tc-7"


async def test_run_id_falls_back_to_empty_string_when_event_run_id_is_none() -> None:
    """Event.run_id 为 None / 空时帧 run_id 兜底空串（前端 buffer key 不接受 None）。"""
    ws = _make_ws()
    sink = WSEventSink(ws)
    # Event.run_id 类型是 str | None；翻译时 `event.run_id or ""` 兜底
    await sink.emit(
        Event(kind="content.delta", run_id=None, turn=1, payload={"delta": "x"})  # type: ignore[arg-type]
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["run_id"] == ""


# ---------------------------------------------------------------------------
# interrupt-run-v0.1：run.cancelled → RunInterruptedFrame
# ---------------------------------------------------------------------------


async def test_emit_run_cancelled_translates_to_run_interrupted_frame() -> None:
    """runner emit ``run.cancelled`` event → 推 :class:`RunInterruptedFrame` 给浏览器。

    payload 字段（cancelled_at_turn / cancelled_tool_call_id / cancel_reason）
    全部透传到 frame；run_id 走通用 ``event.run_id`` 路径。
    """
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="run.cancelled",
            run_id="run-thread-aaa-5",
            turn=3,
            payload={
                "cancelled_at_turn": 3,
                "cancelled_tool_call_id": "call-xyz",
                "cancel_reason": "user_interrupt",
            },
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "run.interrupted"
    assert sent["run_id"] == "run-thread-aaa-5"
    assert sent["cancelled_at_turn"] == 3
    assert sent["cancelled_tool_call_id"] == "call-xyz"
    assert sent["cancel_reason"] == "user_interrupt"


async def test_emit_run_cancelled_with_no_tool_call_id_keeps_none() -> None:
    """打断在 LLM / approval 阶段时 cancelled_tool_call_id 为 None，需正确传成 null。"""
    ws = _make_ws()
    sink = WSEventSink(ws)
    await sink.emit(
        Event(
            kind="run.cancelled",
            run_id="run-x-1",
            turn=2,
            payload={
                "cancelled_at_turn": 2,
                "cancelled_tool_call_id": None,
                "cancel_reason": "user_interrupt",
            },
        )
    )
    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "run.interrupted"
    assert sent["cancelled_tool_call_id"] is None
