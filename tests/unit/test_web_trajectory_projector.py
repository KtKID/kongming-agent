"""unit：TrajectoryProjector 双源投影测试（track-timeline-v0.1 · task-trajectory-backend）。

验证 trace.jsonl 事件流 + session 真源 → ``TrajectoryResponseDTO`` 的投影。

**fixture 形状对齐真实落盘**（v0.1.1 教训：此前 fixture 伪造了
``llm.request`` 携带完整 messages，测试全绿但生产数据里用户输入永远
派生不出来——trace 事件的消息是脱敏摘要，正文只在 session 真源）：

- ``llm.request`` payload: ``request`` 只含 ``message_count`` /
  ``message_roles`` / ``tool_names`` 等摘要字段，**无 messages 正文**；
- ``llm.response`` payload: ``response.message`` 只含 ``content_chars`` /
  ``tool_call_count`` 脱敏摘要；
- 用户输入 / assistant 正文与思考 / 系统提示词全文走
  ``session_messages`` / ``system_prompt`` 参数（session 真源）。

覆盖：

1. 双 run 完整轨迹（trace 时序 + session 正文融合 + 分段统计）
2. 损坏行 → kind=corrupted 占位记录（保留 seq），summary.corrupted_count
3. 空输入 → 空轨迹
4. run.start 无 run.end → running（进行中）
5. run.cancelled + run.end(status=cancelled) → cancelled
6. 无 trace 仅 session 真源 → user_input 记录仍产出（老 thread 兜底）
7. 纯文本轮的 llm 摘要退回 trace 脱敏字段（content_chars）

关键函数：

- ``_ev``：构造 Event 并序列化成 trace.jsonl 行（dataclasses.asdict +
  json.dumps，与 JsonlTraceSink 落盘形状一致）。
- ``_llm_request_payload`` / ``_llm_response_payload`` / ``_usage_payload``：
  按 runner 真实 emit 的脱敏形状构造。
- ``_msg``：构造 session 真源消息值对象。
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from core.contracts import Event
from hosts.web.trajectory.models import (
    SessionMessageRecord,
    SystemPromptSnapshot,
)
from hosts.web.trajectory.projector import TrajectoryProjector

# ---------------------------------------------------------------------------
# fixture 构造（对齐真实落盘形状）
# ---------------------------------------------------------------------------


def _ev(
    kind: str,
    *,
    run_id: str = "t-1",
    turn: int | None = None,
    ts: int = 0,
    payload: dict[str, Any] | None = None,
) -> str:
    """构造一条与 JsonlTraceSink 落盘形状一致的 trace 行。"""
    event = Event(
        kind=kind,
        run_id=run_id,
        turn=turn,
        payload=payload or {},
        timestamp_ms=1_700_000_000_000 + ts,
    )
    return json.dumps(dataclasses.asdict(event), ensure_ascii=False)


def _llm_request_payload(*roles: str) -> dict[str, Any]:
    """构造 runner llm.request payload 的脱敏形状（无 messages 正文）。"""
    return {
        "request": {
            "model": "test-model",
            "message_count": len(roles),
            "message_roles": list(roles),
            "tool_count": 0,
            "tool_names": [],
        },
        "model": "test-model",
        "message_count": len(roles),
        "tool_count": 0,
        "original_message_count": len(roles),
        "tool_contract_attempt": 1,
    }


def _llm_response_payload(
    *,
    content_chars: int = 0,
    tool_call_count: int = 0,
    reasoning: str | None = None,
) -> dict[str, Any]:
    """构造 runner llm.response payload 的脱敏形状（message 无正文）。"""
    provider_metadata: dict[str, Any] = {}
    if reasoning is not None:
        provider_metadata["reasoning_content"] = reasoning
    return {
        "response": {
            "message": {
                "role": "assistant",
                "content_chars": content_chars,
                "tool_call_count": tool_call_count,
                "tool_names": [],
            }
        },
        "finish_reason": "tool_calls" if tool_call_count else "stop",
        "has_tool_calls": bool(tool_call_count),
        "usage": None,
        "provider_metadata": provider_metadata,
    }


def _usage_payload(
    *,
    input_total: int = 100,
    output_total: int = 30,
    reasoning: int | None = 8,
) -> dict[str, Any]:
    """构造 ProviderUsageSnapshot.to_payload 形状（metrics 嵌套 value）。"""
    metrics: dict[str, Any] = {
        "input_total_tokens": {"value": input_total},
        "output_total_tokens": {"value": output_total},
    }
    if reasoning is not None:
        metrics["reasoning_tokens"] = {"value": reasoning}
    return {"family": "openai", "metrics": metrics}


def _msg(
    role: str,
    content: str,
    *,
    ts_ms: int = 0,
    reasoning: str | None = None,
) -> SessionMessageRecord:
    """构造 session 真源消息（created_at 毫秒，基准与 trace 行一致）。"""
    return SessionMessageRecord(
        role=role,
        content=content,
        created_at_ms=1_700_000_000_000 + ts_ms,
        reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# 场景 1：双 run 完整轨迹（trace 时序 + session 正文融合）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_project_full_two_run_trajectory_with_session_fusion() -> None:
    """run1（user + llm + tool 成败）+ run2（user + 纯文本 llm）。

    trace 提供时序 / 耗时 / token；session 提供 user 输入全文与
    assistant 正文 / 思考回填。
    """
    trace_lines = [
        _ev("run.start", ts=0, payload={"session_id": "s", "model": "m"}),
        _ev("turn.start", turn=1, ts=1),
        _ev(
            "llm.request",
            turn=1,
            ts=10,
            payload=_llm_request_payload("system", "user"),
        ),
        _ev("llm.chunk.first", turn=1, ts=130, payload={"elapsed_ms": 120}),
        _ev(
            "llm.response",
            turn=1,
            ts=810,
            payload=_llm_response_payload(content_chars=5, tool_call_count=2),
        ),
        _ev("usage", turn=1, ts=811, payload=_usage_payload()),
        _ev(
            "tool.call.start",
            turn=1,
            ts=900,
            payload={
                "call_id": "call_0",
                "tool_name": "read_file",
                "arguments": {"path": "setting.yaml"},
            },
        ),
        _ev(
            "tool.call.end",
            turn=1,
            ts=1000,
            payload={
                "call_id": "call_0",
                "tool_name": "read_file",
                "ok": True,
                "content": "debug: true",
                "data": None,
                "error_message": None,
            },
        ),
        _ev(
            "tool.call.start",
            turn=1,
            ts=1010,
            payload={
                "call_id": "call_1",
                "tool_name": "run_shell",
                "arguments": {"cmd": "ls"},
            },
        ),
        _ev(
            "tool.call.end",
            turn=1,
            ts=1100,
            payload={
                "call_id": "call_1",
                "tool_name": "run_shell",
                "ok": False,
                "content": "",
                "data": None,
                "error_message": "command not found",
            },
        ),
        _ev("turn.end", turn=1, ts=1101),
        _ev("run.end", turn=1, ts=1102, payload={"status": "completed"}),
        # ---- run 2 ----
        _ev("run.start", run_id="t-2", ts=2000, payload={"session_id": "s"}),
        _ev("turn.start", run_id="t-2", turn=1, ts=2001),
        _ev(
            "llm.request",
            run_id="t-2",
            turn=1,
            ts=2010,
            payload=_llm_request_payload("system", "user", "tool", "user"),
        ),
        _ev(
            "llm.response",
            run_id="t-2",
            turn=1,
            ts=2110,
            payload=_llm_response_payload(content_chars=12),
        ),
        _ev(
            "usage",
            run_id="t-2",
            turn=1,
            ts=2111,
            payload=_usage_payload(input_total=200, output_total=10, reasoning=None),
        ),
        _ev("turn.end", run_id="t-2", turn=1, ts=2112),
        _ev(
            "run.end",
            run_id="t-2",
            turn=1,
            ts=2113,
            payload={"status": "completed"},
        ),
    ]
    session_messages = [
        _msg("user", "帮我读配置", ts_ms=-5),
        _msg("assistant", "我来读配置", ts_ms=810, reasoning="用户要读配置"),
        _msg("user", "总结一下", ts_ms=1995),
        _msg("assistant", "总结：debug 开启", ts_ms=2110),
    ]

    result = TrajectoryProjector().project(
        trace_lines,
        session_messages=session_messages,
        system_prompt=SystemPromptSnapshot(
            content="# system prompt",
            created_at_ms=1_700_000_000_000 - 1000,
        ),
    )

    # run 分段（来自 trace）
    assert [run.run_index for run in result.runs] == [1, 2]
    assert result.runs[0].run_id == "t-1"
    assert result.runs[0].status == "completed"
    assert result.runs[0].duration_ms == 1102
    assert result.runs[0].turn_count == 1
    assert result.runs[0].tokens is not None
    assert result.runs[0].tokens.input_total == 100
    assert result.runs[0].tokens.reasoning == 8
    assert result.runs[1].tokens is not None
    assert result.runs[1].tokens.input_total == 200

    # 记录顺序与 kind：system 最前，user_input 由 session 融合到各 run 头部
    kinds = [record.kind for record in result.records]
    assert kinds == [
        "system",
        "user_input",
        "llm",
        "tool",
        "tool",
        "user_input",
        "llm",
    ]

    # 系统提示词记录（seq=-1，全文）
    system = result.records[0]
    assert system.seq == -1
    assert system.summary == "系统提示词 · 15 字"
    assert system.input_detail == "# system prompt"

    # user_input：来自 session 真源，归属正确 run（seq 负数递减唯一）
    user1 = result.records[1]
    assert user1.seq == -2
    assert user1.run_index == 1
    assert user1.turn == 1
    assert user1.input_detail == "帮我读配置"
    user2 = result.records[5]
    assert user2.seq == -3
    assert user2.input_detail == "总结一下"
    assert user2.run_index == 2

    # llm 记录：session 正文回填 + trace 的 ttft / tokens / finish_reason
    llm1 = result.records[2]
    assert llm1.seq == 2  # trace 行号
    assert llm1.ttft_ms == 120
    assert llm1.duration_ms == 800
    assert llm1.finish_reason == "tool_calls"
    assert llm1.tokens is not None
    assert llm1.tokens.input_total == 100
    assert llm1.tokens.output_total == 30
    assert llm1.tokens.reasoning == 8
    # assistant 正文 / 思考按序回填（trace 里只有 content_chars）
    assert llm1.output_detail == "我来读配置"
    assert llm1.reasoning_detail == "用户要读配置"
    assert llm1.summary == "我来读配置"
    llm2 = result.records[6]
    assert llm2.output_detail == "总结：debug 开启"

    # tool 记录：成功（args/content）与失败（error）
    tool_ok = result.records[3]
    assert tool_ok.tool_name == "read_file"
    assert tool_ok.ok is True
    assert tool_ok.input_detail == '{"path": "setting.yaml"}'
    assert tool_ok.output_detail == "debug: true"
    assert tool_ok.duration_ms == 100
    tool_err = result.records[4]
    assert tool_err.ok is False
    assert tool_err.error_message == "command not found"

    # 总览统计
    assert result.summary.status == "completed"
    assert result.summary.tool_call_count == 2
    assert result.summary.turn_count == 2
    assert result.summary.corrupted_count == 0
    assert result.summary.tokens is not None
    assert result.summary.tokens.input_total == 300
    assert result.summary.total_duration_ms == 2113


# ---------------------------------------------------------------------------
# 场景 2：损坏行占位
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_project_corrupted_lines_keep_placeholder() -> None:
    """无法解析的行产出 kind=corrupted 占位记录（保留 seq），不中断投影。"""
    lines = [
        _ev("run.start", ts=0),
        "{ 不是合法 JSON",
        json.dumps({"kind": "llm.request"}),  # 缺 timestamp_ms 等必要结构
        _ev("run.end", turn=1, ts=100, payload={"status": "completed"}),
    ]

    result = TrajectoryProjector().project(lines)

    assert result.summary.corrupted_count == 2
    corrupted = [r for r in result.records if r.kind == "corrupted"]
    assert [r.seq for r in corrupted] == [1, 2]
    assert all(r.summary for r in corrupted)
    # 损坏行不中断：run 仍被投影
    assert len(result.runs) == 1
    assert result.runs[0].status == "completed"


# ---------------------------------------------------------------------------
# 场景 3：空输入
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_project_empty_input_returns_empty_trajectory() -> None:
    """无 trace 行且无 session 源 → 空轨迹（runs/records 空，统计全零）。"""
    result = TrajectoryProjector().project([])
    assert result.runs == []
    assert result.records == []
    assert result.summary.tool_call_count == 0
    assert result.summary.total_duration_ms == 0


# ---------------------------------------------------------------------------
# 场景 4：进行中 run
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_project_open_run_marks_running() -> None:
    """run.start 后无 run.end（llm 仍打开）→ run 与总览均 running。"""
    lines = [
        _ev("run.start", ts=0),
        _ev(
            "llm.request",
            turn=1,
            ts=10,
            payload=_llm_request_payload("system", "user"),
        ),
    ]

    result = TrajectoryProjector().project(
        lines,
        session_messages=[_msg("user", "进行中的问题", ts_ms=-5)],
    )

    assert result.runs[0].status == "running"
    assert result.runs[0].ended_at_ms is None
    assert result.summary.status == "running"
    user = result.records[0]
    assert user.kind == "user_input"
    assert user.input_detail == "进行中的问题"


# ---------------------------------------------------------------------------
# 场景 5：取消 run
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_project_cancelled_run() -> None:
    """run.cancelled + run.end(status=cancelled) → run 状态 cancelled。"""
    lines = [
        _ev("run.start", ts=0),
        _ev("run.cancelled", turn=1, ts=50, payload={"cancelled_at_turn": 1}),
        _ev("run.end", turn=1, ts=60, payload={"status": "cancelled"}),
    ]

    result = TrajectoryProjector().project(lines)

    assert result.runs[0].status == "cancelled"
    assert result.summary.status == "cancelled"


# ---------------------------------------------------------------------------
# 场景 6：无 trace 仅 session 真源（老 thread 兜底）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_project_session_only_without_trace() -> None:
    """无 trace.jsonl（老 thread）→ 仍产出 system + user_input 记录。"""
    result = TrajectoryProjector().project(
        [],
        session_messages=[
            _msg("user", "第一条", ts_ms=0),
            _msg("assistant", "回复一", ts_ms=500),
            _msg("user", "第二条", ts_ms=1000),
        ],
        system_prompt=SystemPromptSnapshot(content="你是孔明", created_at_ms=None),
    )

    assert result.runs == []
    kinds = [record.kind for record in result.records]
    assert kinds == ["system", "user_input", "user_input"]
    assert result.records[0].summary == "系统提示词 · 4 字"
    assert [r.input_detail for r in result.records[1:]] == ["第一条", "第二条"]
    assert result.summary.status == "completed"


# ---------------------------------------------------------------------------
# 场景 7：纯文本轮的 llm 摘要退回 trace 脱敏字段
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_project_llm_summary_falls_back_to_trace_summary_fields() -> None:
    """session 无对应 assistant 正文时，摘要退回 content_chars 脱敏字段。"""
    lines = [
        _ev("run.start", ts=0),
        _ev(
            "llm.request",
            turn=1,
            ts=10,
            payload=_llm_request_payload("system", "user", "tool"),
        ),
        _ev(
            "llm.response",
            turn=1,
            ts=110,
            payload=_llm_response_payload(content_chars=17),
        ),
        _ev("run.end", turn=1, ts=200, payload={"status": "completed"}),
    ]

    result = TrajectoryProjector().project(lines)

    llm = result.records[-1]
    assert llm.kind == "llm"
    assert llm.summary == "（模型回复 17 字符）"
    assert llm.output_detail is None
