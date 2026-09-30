"""TrajectoryProjector：trace.jsonl + session 真源 → TrajectoryResponseDTO 投影。

设计要点（对齐 ``docs/spec/track-timeline-v0.1/Readme.md``）：

- **纯函数、零 IO**：输入是 trace.jsonl 行序列（``seq`` = 行号，0 起）与
  session 真源值对象（用户/assistant 正文、系统提示词），输出协议 DTO。
  文件定位与读取是 :class:`hosts.web.trajectory.manager.TrajectoryManager`
  的职责。
- **双源融合（v0.1.1 修正）**：trace 事件里的消息是脱敏摘要
  （``message_roles`` / ``content_chars``，不落正文防爆磁盘），因此：

  - 时序 / 耗时 / token / 首字耗时 / 工具入参出参 → 来自 trace.jsonl；
  - 用户输入全文、assistant 回复与思考全文、系统提示词全文 → 来自
    session 真源（session jsonl ``record_type=message`` 行 +
    ``system_prompt.json``）。runner 每个 llm.response 后按序 append 一条
    assistant 消息，故 assistant 正文按出现顺序一一回填 llm 草稿。

- **不动 Runner、不新增事件 kind**：只消费既有事件。
- **两阶段投影**：先在可变草稿上累积（usage 在 ``llm.response`` 之后才
  emit，需要回填），EOF 后统一物化 frozen DTO。
- **损坏行占位不丢弃**：无法解析的行产出 ``kind=corrupted`` 占位记录
  （保留 seq），投影不中断，且融合插入不改变 trace 记录的原有顺序。
- **进行中判定**：run.start 无对应 run.end → status=running。

关键函数 / 类：

- :class:`TrajectoryProjector`：投影器本体，公开方法只有
  :meth:`TrajectoryProjector.project`。
- :func:`_parse_line`：单行解析，失败返回 None（由调用方落 corrupted 占位）。
- :func:`_tokens_from_usage`：usage 事件 metrics 嵌套结构 → token 明细。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from hosts.web.protocol.rest_models import (
    TrajectoryRecordDTO,
    TrajectoryRecordKind,
    TrajectoryResponseDTO,
    TrajectoryRunDTO,
    TrajectoryRunStatus,
    TrajectorySummaryDTO,
    TrajectoryTokensDTO,
)
from hosts.web.trajectory.models import (
    CorruptedDraft,
    LlmDraft,
    ParsedTraceEvent,
    RunDraft,
    SessionMessageRecord,
    SystemPromptDraft,
    SystemPromptSnapshot,
    ToolDraft,
    UserInputDraft,
)

#: 列表行单行摘要的最大长度（详情面板走 ``*_detail`` 全文字段）。
_SUMMARY_MAX_CHARS = 120

#: 系统提示词记录的 seq（非 trace 来源，恒排账本最前）。
_SYSTEM_SEQ = -1

#: usage metrics 键 → TrajectoryTokensDTO 字段名的映射。
_USAGE_METRIC_FIELDS: dict[str, str] = {
    "input_total_tokens": "input_total",
    "output_total_tokens": "output_total",
    "reasoning_tokens": "reasoning",
    "cache_read_tokens": "cache_read",
    "cache_write_tokens": "cache_write",
}


def _truncate_summary(text: str) -> str:
    """把任意文本压成单行摘要：合并空白并在超长时截断。"""
    single_line = " ".join(text.split())
    if len(single_line) <= _SUMMARY_MAX_CHARS:
        return single_line
    return single_line[: _SUMMARY_MAX_CHARS - 1] + "…"


def _parse_line(seq: int, line: str) -> ParsedTraceEvent | None:
    """解析一行 trace；结构不完整（缺 kind / timestamp_ms 等）返回 None。"""
    try:
        raw = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    ts = raw.get("timestamp_ms")
    if not isinstance(kind, str) or not kind:
        return None
    if isinstance(ts, bool) or not isinstance(ts, int):
        return None
    run_id = raw.get("run_id")
    turn = raw.get("turn")
    payload = raw.get("payload")
    return ParsedTraceEvent(
        seq=seq,
        kind=kind,
        timestamp_ms=ts,
        run_id=run_id if isinstance(run_id, str) else "",
        turn=turn if (isinstance(turn, int) and not isinstance(turn, bool)) else None,
        payload=payload if isinstance(payload, dict) else {},
    )


def _metric_value(payload: dict[str, Any], metric_key: str) -> int | None:
    """从 usage metrics 嵌套结构里安全取一个指标值（缺测为 None）。"""
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return None
    metric = metrics.get(metric_key)
    if not isinstance(metric, dict):
        return None
    value = metric.get("value")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _tokens_from_usage(payload: dict[str, Any]) -> dict[str, int]:
    """把 usage 事件 payload 提取成 {DTO 字段名: 数值} 的累积单元。"""
    tokens: dict[str, int] = {}
    for metric_key, field_name in _USAGE_METRIC_FIELDS.items():
        value = _metric_value(payload, metric_key)
        if value is not None:
            tokens[field_name] = value
    return tokens


def _accumulate_tokens(target: dict[str, int], addition: dict[str, int]) -> None:
    """把一组 token 指标累加进目标累积单元（就地修改）。"""
    for field_name, value in addition.items():
        target[field_name] = target.get(field_name, 0) + value


def _tokens_dto(acc: dict[str, int] | None) -> TrajectoryTokensDTO:
    """把累积单元物化为 token DTO（无数据时各指标为 None）。"""
    if not acc:
        return TrajectoryTokensDTO()
    return TrajectoryTokensDTO(
        input_total=acc.get("input_total"),
        output_total=acc.get("output_total"),
        reasoning=acc.get("reasoning"),
        cache_read=acc.get("cache_read"),
        cache_write=acc.get("cache_write"),
    )


def _map_run_status(raw: object) -> TrajectoryRunStatus:
    """run.end payload status → 枚举；未知值保守归为 failed。"""
    if raw == "completed":
        return TrajectoryRunStatus.COMPLETED
    if raw == "cancelled":
        return TrajectoryRunStatus.CANCELLED
    return TrajectoryRunStatus.FAILED


class TrajectoryProjector:
    """trace.jsonl + session 真源 → TrajectoryResponseDTO 的纯投影器（零 IO）。"""

    def project(
        self,
        lines: Sequence[str],
        *,
        session_messages: Sequence[SessionMessageRecord] = (),
        system_prompt: SystemPromptSnapshot | None = None,
    ) -> TrajectoryResponseDTO:
        """双源投影；输出不含 thread_id 的轨迹 DTO（由 Manager 回填）。

        Args:
            lines: trace.jsonl 行序列（seq = 行号）。
            session_messages: session 真源消息（用户输入 / assistant 正文，
                按文件序）。用户消息融合成 user_input 记录；assistant
                正文按出现顺序回填 llm 草稿。
            system_prompt: 系统提示词快照；非 None 时产出账本最前的
                ``system`` 记录（seq=-1）。
        """
        records: list[
            UserInputDraft | LlmDraft | ToolDraft | CorruptedDraft | SystemPromptDraft
        ] = []
        runs: list[RunDraft] = []
        global_tokens: dict[str, int] = {}
        first_ts: int | None = None
        last_ts: int | None = None
        corrupted_count = 0
        tool_call_count = 0
        last_llm: LlmDraft | None = None
        open_tools: dict[str, ToolDraft] = {}

        for seq, line in enumerate(lines):
            event = _parse_line(seq, line)
            if event is None:
                records.append(CorruptedDraft(seq=seq))
                corrupted_count += 1
                continue
            if first_ts is None:
                first_ts = event.timestamp_ms
            last_ts = event.timestamp_ms
            current_run = runs[-1] if runs else None

            if event.kind == "run.start":
                current_run = RunDraft(
                    run_index=len(runs) + 1,
                    run_id=event.run_id,
                    started_at_ms=event.timestamp_ms,
                )
                runs.append(current_run)
            elif event.kind == "run.end" and current_run is not None:
                current_run.status = _map_run_status(event.payload.get("status")).value
                current_run.ended_at_ms = event.timestamp_ms
                if current_run.started_at_ms is not None:
                    current_run.duration_ms = event.timestamp_ms - current_run.started_at_ms
            elif event.kind == "run.cancelled" and current_run is not None:
                current_run.status = TrajectoryRunStatus.CANCELLED.value
            elif event.kind in ("turn.start", "turn.end"):
                if current_run is not None and event.turn is not None:
                    current_run.max_turn = max(current_run.max_turn, event.turn)
            elif event.kind == "llm.request":
                run_index = current_run.run_index if current_run else 0
                run_id = current_run.run_id if current_run else event.run_id
                last_llm = LlmDraft(
                    seq=event.seq,
                    run_index=run_index,
                    run_id=run_id,
                    turn=event.turn,
                    started_at_ms=event.timestamp_ms,
                )
                records.append(last_llm)
            elif event.kind == "llm.chunk.first" and last_llm is not None:
                elapsed = event.payload.get("elapsed_ms")
                if isinstance(elapsed, int) and not isinstance(elapsed, bool):
                    last_llm.ttft_ms = elapsed
            elif event.kind == "llm.response":
                if last_llm is not None and not last_llm.closed:
                    self._close_llm_draft(last_llm, event)
            elif event.kind == "usage":
                tokens = _tokens_from_usage(event.payload)
                if last_llm is not None:
                    _accumulate_tokens(last_llm.tokens, tokens)
                if current_run is not None:
                    _accumulate_tokens(current_run.tokens, tokens)
                _accumulate_tokens(global_tokens, tokens)
            elif event.kind == "tool.call.start":
                run_index = current_run.run_index if current_run else 0
                run_id = current_run.run_id if current_run else event.run_id
                arguments = event.payload.get("arguments")
                call_id = str(event.payload.get("call_id", ""))
                draft = ToolDraft(
                    seq=event.seq,
                    run_index=run_index,
                    run_id=run_id,
                    turn=event.turn,
                    call_id=call_id,
                    tool_name=str(event.payload.get("tool_name", "")),
                    started_at_ms=event.timestamp_ms,
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
                open_tools[call_id] = draft
                records.append(draft)
                tool_call_count += 1
            elif event.kind == "tool.call.end":
                call_id = str(event.payload.get("call_id", ""))
                end_draft = open_tools.pop(call_id) if call_id in open_tools else None
                if end_draft is None:
                    run_index = current_run.run_index if current_run else 0
                    run_id = current_run.run_id if current_run else event.run_id
                    end_draft = ToolDraft(
                        seq=event.seq,
                        run_index=run_index,
                        run_id=run_id,
                        turn=event.turn,
                        call_id=call_id,
                        tool_name=str(event.payload.get("tool_name", "")),
                        started_at_ms=None,
                    )
                    records.append(end_draft)
                self._close_tool_draft(end_draft, event, last_ts)

        # EOF 收口：未配对草稿按“进行中”语义收（ok=None / ended=None）。
        if last_ts is not None:
            if last_llm is not None and not last_llm.closed:
                last_llm.ended_at_ms = last_ts
            for draft in open_tools.values():
                draft.ended_at_ms = last_ts

        # ---- session 真源融合（不改 trace 记录原有顺序） ----
        self._fuse_session_messages(records, runs, session_messages)
        if system_prompt is not None:
            records.insert(
                0,
                SystemPromptDraft(
                    started_at_ms=system_prompt.created_at_ms,
                    text=system_prompt.content,
                ),
            )

        run_dtos = [
            TrajectoryRunDTO(
                run_index=draft.run_index,
                run_id=draft.run_id,
                status=TrajectoryRunStatus(draft.status),
                started_at_ms=draft.started_at_ms,
                ended_at_ms=draft.ended_at_ms,
                duration_ms=draft.duration_ms,
                turn_count=draft.max_turn,
                tokens=_tokens_dto(draft.tokens),
            )
            for draft in runs
        ]
        record_dtos = [self._materialize_record(draft) for draft in records]
        if any(run.status == TrajectoryRunStatus.RUNNING for run in run_dtos):
            summary_status = TrajectoryRunStatus.RUNNING
        elif run_dtos:
            summary_status = run_dtos[-1].status
        else:
            summary_status = TrajectoryRunStatus.COMPLETED

        return TrajectoryResponseDTO(
            thread_id="",
            summary=TrajectorySummaryDTO(
                status=summary_status,
                total_duration_ms=(
                    last_ts - first_ts if first_ts is not None and last_ts is not None else 0
                ),
                turn_count=sum(run.turn_count for run in run_dtos),
                tool_call_count=tool_call_count,
                corrupted_count=corrupted_count,
                tokens=_tokens_dto(global_tokens),
            ),
            runs=run_dtos,
            records=record_dtos,
        )

    # ------------------------------------------------------------------
    # 内部：session 真源融合
    # ------------------------------------------------------------------

    def _fuse_session_messages(
        self,
        records: list[UserInputDraft | LlmDraft | ToolDraft | CorruptedDraft | SystemPromptDraft],
        runs: list[RunDraft],
        session_messages: Sequence[SessionMessageRecord],
    ) -> None:
        """把 session 正文融合进草稿序列（就地修改，不改 trace 顺序）。

        - user 消息 → user_input 草稿，按序消费 run（一次 submit = 一条
          user 消息 + 一个 run；runner 在 run.start 前后 seed user 消息）。
          插入位置 = 归属 run 的记录段头部；无 run 可归属（含无 trace 的
          纯 session 轨迹）→ run_index=0 并插到序列最前。
        - assistant 消息 → 按出现顺序一一回填 llm 草稿的正文与思考
          （runner 每个 llm.response 后按序 append 一条 assistant 消息）。
        """
        llm_drafts = [d for d in records if isinstance(d, LlmDraft)]
        llm_iter = iter(llm_drafts)
        # session 来源记录的稳定唯一 id：负数递减（-1 预留给系统提示词，
        # user_input 从 -2 起）——seq 在前端同时充当 React key / 选中定位
        session_seq = -2

        def _emit_user(record: SessionMessageRecord) -> None:
            nonlocal session_seq
            run = next((r for r in runs if not r.user_input_consumed), None)
            if run is not None:
                run.user_input_consumed = True
                draft = UserInputDraft(
                    seq=session_seq,
                    run_index=run.run_index,
                    run_id=run.run_id,
                    turn=1,
                    started_at_ms=record.created_at_ms,
                    text=record.content,
                )
                # 插到该 run 记录段的头部（第一个 run_index 匹配的记录前）
                insert_at = next(
                    (
                        i
                        for i, existing in enumerate(records)
                        if not isinstance(existing, SystemPromptDraft)
                        and getattr(existing, "run_index", run.run_index) == run.run_index
                    ),
                    len(records),
                )
                records.insert(insert_at, draft)
            else:
                records.append(
                    UserInputDraft(
                        seq=session_seq,
                        run_index=0,
                        run_id="",
                        turn=None,
                        started_at_ms=record.created_at_ms,
                        text=record.content,
                    )
                )
            session_seq -= 1

        for record in session_messages:
            if record.role == "user":
                _emit_user(record)
            elif record.role == "assistant":
                draft = next(llm_iter, None)
                if draft is None:
                    continue
                if record.content:
                    draft.content = record.content
                if record.reasoning:
                    draft.reasoning = record.reasoning

    # ------------------------------------------------------------------
    # 内部：草稿关闭与物化
    # ------------------------------------------------------------------

    def _close_llm_draft(self, draft: LlmDraft, event: ParsedTraceEvent) -> None:
        """用 llm.response 事件回填并关闭 llm 草稿。

        注意：trace 里的 message 是脱敏摘要（``content_chars`` /
        ``tool_call_count``），正文由 session 真源在 EOF 后回填。
        """
        draft.closed = True
        draft.ended_at_ms = event.timestamp_ms
        draft.finish_reason = (
            str(event.payload.get("finish_reason"))
            if event.payload.get("finish_reason") is not None
            else None
        )
        response = event.payload.get("response")
        message = response.get("message") if isinstance(response, dict) else None
        if isinstance(message, dict):
            content_chars = message.get("content_chars")
            if isinstance(content_chars, int) and not isinstance(content_chars, bool):
                draft.content_chars = content_chars
            tool_call_count = message.get("tool_call_count")
            if isinstance(tool_call_count, int) and not isinstance(tool_call_count, bool):
                draft.tool_call_count = tool_call_count
        metadata = event.payload.get("provider_metadata")
        if isinstance(metadata, dict):
            reasoning = metadata.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                draft.reasoning = reasoning

    def _close_tool_draft(
        self,
        draft: ToolDraft,
        event: ParsedTraceEvent,
        fallback_end: int | None,
    ) -> None:
        """用 tool.call.end 事件回填并关闭工具草稿。"""
        draft.closed = True
        draft.ended_at_ms = event.timestamp_ms
        ok = event.payload.get("ok")
        draft.ok = ok if isinstance(ok, bool) else None
        content = event.payload.get("content")
        draft.content = content if isinstance(content, str) else ""
        draft.data = event.payload.get("data")
        error_message = event.payload.get("error_message")
        draft.error_message = error_message if isinstance(error_message, str) else None
        if draft.ended_at_ms is None:
            draft.ended_at_ms = fallback_end

    def _materialize_record(
        self,
        draft: UserInputDraft | LlmDraft | ToolDraft | CorruptedDraft | SystemPromptDraft,
    ) -> TrajectoryRecordDTO:
        """把累积草稿物化成 frozen 的轨迹记录 DTO。"""
        if isinstance(draft, SystemPromptDraft):
            char_count = len(draft.text)
            return TrajectoryRecordDTO(
                seq=draft.seq,
                kind=TrajectoryRecordKind.SYSTEM,
                run_index=0,
                run_id="",
                turn=None,
                summary=f"系统提示词 · {char_count} 字",
                started_at_ms=draft.started_at_ms,
                duration_ms=None,
                ok=None,
                input_detail=draft.text or None,
            )
        if isinstance(draft, UserInputDraft):
            return TrajectoryRecordDTO(
                seq=draft.seq,
                kind=TrajectoryRecordKind.USER_INPUT,
                run_index=draft.run_index,
                run_id=draft.run_id,
                turn=draft.turn,
                summary=_truncate_summary(draft.text),
                started_at_ms=draft.started_at_ms,
                duration_ms=None,
                ok=None,
                input_detail=draft.text,
            )
        if isinstance(draft, LlmDraft):
            duration = (
                draft.ended_at_ms - draft.started_at_ms
                if draft.ended_at_ms is not None and draft.started_at_ms is not None
                else None
            )
            if draft.content:
                summary = _truncate_summary(draft.content)
            elif draft.tool_call_count:
                summary = f"（{draft.tool_call_count} 个工具调用）"
            elif draft.content_chars:
                summary = f"（模型回复 {draft.content_chars} 字符）"
            else:
                summary = ""
            return TrajectoryRecordDTO(
                seq=draft.seq,
                kind=TrajectoryRecordKind.LLM,
                run_index=draft.run_index,
                run_id=draft.run_id,
                turn=draft.turn,
                summary=summary,
                started_at_ms=draft.started_at_ms,
                duration_ms=duration,
                ok=None,
                ttft_ms=draft.ttft_ms,
                finish_reason=draft.finish_reason,
                tokens=_tokens_dto(draft.tokens),
                output_detail=draft.content or None,
                reasoning_detail=draft.reasoning,
            )
        if isinstance(draft, ToolDraft):
            duration = (
                draft.ended_at_ms - draft.started_at_ms
                if draft.ended_at_ms is not None and draft.started_at_ms is not None
                else None
            )
            output = draft.content
            if not output and draft.data is not None:
                try:
                    output = json.dumps(draft.data, ensure_ascii=False)
                except (TypeError, ValueError):
                    output = ""
            return TrajectoryRecordDTO(
                seq=draft.seq,
                kind=TrajectoryRecordKind.TOOL,
                run_index=draft.run_index,
                run_id=draft.run_id,
                turn=draft.turn,
                summary=_truncate_summary(draft.tool_name),
                started_at_ms=draft.started_at_ms,
                duration_ms=duration,
                ok=draft.ok,
                tool_name=draft.tool_name or None,
                call_id=draft.call_id or None,
                input_detail=(
                    json.dumps(draft.arguments, ensure_ascii=False) if draft.arguments else None
                ),
                output_detail=output or None,
                error_message=draft.error_message,
            )
        return TrajectoryRecordDTO(
            seq=draft.seq,
            kind=TrajectoryRecordKind.CORRUPTED,
            summary="记录损坏（无法解析）",
        )
