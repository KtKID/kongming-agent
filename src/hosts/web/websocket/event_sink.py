"""EventSink 协议的 WS 推送实现。

把 :class:`core.contracts.Event` 翻译成 :mod:`web.protocol` 的具体帧
推到浏览器 WebSocket。

设计要点：

- **每 cell 私有一个 sink 实例**：runtime ``Event`` 没有 ``session_id``
  字段，跨 cell 共享 sink 时无法路由。v0.1.5 选择"每个 ThreadCell 自带
  sinks 列表"——天然按 cell 隔离，避免按 session_id 分发的复杂度。
- **静默丢弃未识别 kind**：runtime 未来可能新增 EventKind（如 v0.2 又
  加几个流式相关），sink 不识别的直接 return，避免每加一个 kind 都要
  改 sink。
- **审批事件分流**：Runner 的 ``approval.request`` 保留为审计事件，Web 审批由
  ApprovalManager 投影成全局 ``approval.inbox.*`` 帧。
- **不推 ``thread.history`` / ``assistant.final`` / ``cell.evicted``**：
  - ``thread.history``：建连时 ThreadManager 单独推（不走 Event）
  - ``assistant.final``：HostAdapter.write_output 推
  - ``cell.evicted``：ThreadManager.evict_cell 直接推
- **send 失败静默吞**：与 :class:`WebHostAdapter._safe_send_json` 同语义；
  WS 异常不污染 runtime 主链路。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, cast

from core.clock import now_epoch_ms
from core.contracts import Event, ProviderUsageFamily, ProviderUsageSnapshot
from devtools import get_full_logger
from hosts.web.protocol import (
    ApprovalDecisionFrame,
    ApprovalOutcome,
    ChoiceRequestFrame,
    ContentDeltaFrame,
    ErrorCode,
    ErrorFrame,
    ReasoningDeltaFrame,
    RunInterruptedFrame,
    ToolCallEndFrame,
    ToolCallStartFrame,
    TurnEndFrame,
    TurnStartFrame,
    UsageFrame,
)
from hosts.web.protocol._base import _S2CFrameBase
from hosts.web.protocol.ws_frames import SystemNoticeFrame
from network.network_log import log_network_exception

# full-log-v0.1 阶段 1：只对 turn.* 边界帧调 full_logger 记录，
# 阶段 2 #11 取消白名单后全部 frame 都记。
_FULL_LOG_KIND_WHITELIST: frozenset[str] = frozenset({"turn.start", "turn.end"})

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    # agent-tree-v0.1 模块 H：统一走 core.clock.now_epoch_ms（tz-aware），
    # 取代裸 now_epoch_ms()。
    return now_epoch_ms()


# core.contracts.ErrorCode 的 5 类与 web.protocol.ErrorCode 5 类一一对应；
# runtime 内部通常用 ``error_type`` 字符串（如 ``"NetworkError"`` /
# ``"ProviderError"``）。本表把常见 error_type → web ErrorCode 做一次保守映射。
# 不命中的全部归到 ``internal``。
_ERROR_TYPE_TO_CODE: dict[str, ErrorCode] = {
    "NetworkError": "network",
    "ProviderError": "llm_error",
    "LLMError": "llm_error",
    "ToolError": "tool_error",
    "ApprovalTimeout": "approval_timeout",
}

_EVOLUTION_NOTICE_KEY = "self_evolution.review"
_EVOLUTION_NOTICE_SOURCE = "self_evolution"


def _build_usage_dto(payload: dict[str, Any]) -> dict[str, Any]:
    """从 canonical snapshot 构造 generic_chat channel DTO。"""
    snapshot = ProviderUsageSnapshot.from_payload(payload)
    raw = snapshot.raw_usage
    model_raw = raw.get("model")
    model = model_raw if isinstance(model_raw, str) else ""

    if snapshot.family is ProviderUsageFamily.ANTHROPIC_MESSAGES:
        cc_raw = raw.get("cache_creation") or {}
        if not isinstance(cc_raw, dict):
            cc_raw = {}
        return {
            "provider": "claude",
            "input_tokens": snapshot.input_uncached_tokens.value,
            "output_tokens": snapshot.output_total_tokens.value,
            "cache_read_input_tokens": snapshot.cache_read_tokens.value,
            "cache_creation_input_tokens": snapshot.cache_write_tokens.value,
            "cache_creation": {
                "ephemeral_1h_input_tokens": _optional_token(
                    cc_raw.get("ephemeral_1h_input_tokens")
                ),
                "ephemeral_5m_input_tokens": _optional_token(
                    cc_raw.get("ephemeral_5m_input_tokens")
                ),
            },
            "context_usage": snapshot.input_total_tokens.value,
            "model": model,
            "context_window": 0,
        }

    return {
        "provider": "openai",
        "last": {
            "input_tokens": snapshot.input_total_tokens.value,
            "cached_input_tokens": snapshot.cache_read_tokens.value,
            "output_tokens": snapshot.output_total_tokens.value,
            "reasoning_output_tokens": snapshot.reasoning_tokens.value,
            "total_tokens": snapshot.total_tokens.value,
        },
        "model": model,
        "context_window": 0,
    }


def _optional_token(value: object) -> int | None:
    """读取 raw 中的可选 token，输入为开放值，输出为非负整数或 None。"""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


# ⚠️ UsagePersistSink 已在 usage-token-v2-bigbang 删除。
# v2 manager 是无状态门面，不接受外部 push token；所有 token 数据现场从 SDK
# 真源（jsonl/rollout）派生。Event(kind="usage") 不再需要持久化 sink；前端 token
# 显示通过 GET /threads/<tid>/usage 端点拿 v2 manager.get_thread_usage 的派生
# 结果。详见 docs/usage-token-v2/03-core-workflows.md。


class WSEventSink:
    """实现 :class:`core.contracts.EventSink` 的 WS 推送 sink。

    Attributes:
        _ws: 当前 WS 连接。可在 :meth:`attach_ws` 时替换。
        _closed: 标记 ws 已断 / 不可写。closed 后所有 emit 静默丢。
        _thread_id: 当前 sink 服务的 thread id，写入 full_log 时作为记录字段。
            可选——历史构造点（测试 / claude_code evolution sink）不传时为
            ``None``，对应 full_log 记录里 ``thread_id`` 字段为 null。
    """

    def __init__(self, ws: Any, *, thread_id: str | None = None) -> None:
        self._ws: Any = ws
        self._closed = False
        self._thread_id: str | None = thread_id

    async def emit(self, event: Event) -> None:
        """实现 EventSink 协议；把 event 翻成帧推 WS。"""
        if self._closed:
            return
        frame = self._translate(event)
        if frame is None:
            return
        payload = frame.model_dump()
        try:
            await self._ws.send_json(payload)
        except Exception as exc:
            logger.warning(
                "WSEventSink ws.send_json failed for event.kind=%s: %s; marking closed",
                event.kind,
                exc,
            )
            log_network_exception(
                "hosts.web.websocket.event_sink",
                "emit_send_failed",
                exc,
                event_kind=event.kind,
            )
            self._closed = True
            try:
                close_call = self._ws.close()
                # 兼容同步 close (mock) / 异步 close (websocket)
                if asyncio.iscoroutine(close_call):
                    await close_call
            except Exception as close_exc:
                log_network_exception(
                    "hosts.web.websocket.event_sink",
                    "emit_close_failed",
                    close_exc,
                    event_kind=event.kind,
                )
            return

        # full-log-v0.1 阶段 1：send 成功后把 turn.* 帧记录到 full_log。
        # 用 whitelist 控制阶段 1 只接 turn.start / turn.end，阶段 2 #11 移除。
        # full_logger 未启用 / 未 init 时 log() 是 no-op，零开销 + 永不抛。
        if event.kind in _FULL_LOG_KIND_WHITELIST:
            with contextlib.suppress(Exception):
                full_logger = get_full_logger()
                await full_logger.log(
                    "s2c",
                    "ws.threads",
                    payload,
                    thread_id=self._thread_id,
                )

    def attach_ws(self, new_ws: Any) -> None:
        """向 thread fanout 注册一个新的 WS 连接。"""
        attach = getattr(type(self._ws), "attach_ws", None)
        if callable(attach):
            attach(self._ws, new_ws)
        else:
            self._ws = new_ws
        self._closed = False

    def detach_ws(self, ws: Any) -> None:
        """从 thread fanout 注销一个 WS 连接。"""
        detach = getattr(type(self._ws), "detach_ws", None)
        if callable(detach):
            detach(self._ws, ws)

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------
    # 翻译层
    # ------------------------------------------------------------------

    def _translate(self, event: Event) -> _S2CFrameBase | None:
        """把 runtime Event 翻成对应 ws-protocol 帧。

        不识别的 kind 返回 ``None``，由 :meth:`emit` 静默丢。
        """
        kind = event.kind
        payload = event.payload or {}
        ts = _now_ms()
        turn = event.turn or 0
        # Event.run_id 类型为 str | None；前端 buffer key 不接受 None，统一兜底为 ""
        run_id = event.run_id or ""
        # agent-tree-v0.1 模块 G：透传 Event.agent_id 坐标字段到 wire 帧，
        # 让前端能按 agent 归属展示流式帧（多 agent 场景）。
        agent_id = event.agent_id or ""

        # ----- 流式增量 -----
        if kind == "content.delta":
            return ContentDeltaFrame(
                delta=str(payload.get("delta", "")),
                turn=turn,
                seq=int(payload.get("seq", 0) or 0),
                run_id=run_id,
                timestamp_ms=ts,
                agent_id=agent_id,
            )
        if kind == "reasoning.delta":
            return ReasoningDeltaFrame(
                delta=str(payload.get("delta", "")),
                turn=turn,
                seq=int(payload.get("seq", 0) or 0),
                run_id=run_id,
                timestamp_ms=ts,
                agent_id=agent_id,
            )

        # ----- turn 边界 -----
        if kind == "turn.start":
            return TurnStartFrame(turn=turn, run_id=run_id, timestamp_ms=ts, agent_id=agent_id)
        if kind == "turn.end":
            history_index_raw = payload.get("history_index")
            history_index = (
                history_index_raw
                if isinstance(history_index_raw, int)
                and not isinstance(history_index_raw, bool)
                and history_index_raw >= 0
                else None
            )
            return TurnEndFrame(
                turn=turn,
                run_id=run_id,
                history_index=history_index,
                has_tool_calls=bool(payload.get("has_tool_calls", False)),
                timestamp_ms=ts,
                agent_id=agent_id,
            )

        # ----- 工具调用 -----
        if kind == "tool.call.start":
            return ToolCallStartFrame(
                tool_name=str(payload.get("tool_name", "")),
                call_id=str(payload.get("call_id", "")),
                turn=turn,
                arguments=_safe_dict(payload.get("arguments")),
                run_id=run_id,
                timestamp_ms=ts,
                agent_id=agent_id,
            )
        if kind == "tool.call.end":
            # v0.1.6：把 ToolResult 的 content / data 一并写入 frame，前端
            # 才能渲染真实工具产出（之前 schema 漏字段，UI 永远显示空）。
            content_raw = payload.get("content", "")
            data_raw = payload.get("data")
            return ToolCallEndFrame(
                call_id=str(payload.get("call_id", "")),
                turn=turn,
                ok=bool(payload.get("ok", False)),
                error_message=_optional_str(payload.get("error_message")),
                content=str(content_raw) if content_raw is not None else "",
                data=data_raw if isinstance(data_raw, dict) else None,
                run_id=run_id,
                timestamp_ms=ts,
                agent_id=agent_id,
            )

        if kind == "choice.requested":
            return ChoiceRequestFrame(
                request_id=str(payload.get("request_id", "")),
                title=str(payload.get("title", "")),
                description=str(payload.get("description", "")),
                questions=list(payload.get("questions") or []),
                turn=turn,
                run_id=run_id,
                timestamp_ms=ts,
                agent_id=agent_id,
            )

        # ----- 审批 -----
        # Runner 的 ``approval.request`` 是审计事件；generic_chat 审批走
        # ApprovalManager → approval.inbox.*，本 sink 只推 approval.decision。
        if kind == "approval.decision":
            outcome_raw = payload.get("outcome")
            # ApprovalOutcome 枚举与 core 一致：approved / rejected / cancelled
            if outcome_raw not in ("approved", "rejected", "cancelled"):
                # 容错：未知 outcome 视为 cancelled，避免帧构造失败
                outcome: ApprovalOutcome = "cancelled"
            else:
                outcome = cast(ApprovalOutcome, outcome_raw)
            return ApprovalDecisionFrame(
                call_id=str(payload.get("call_id", "")),
                outcome=outcome,
                turn=turn,
                timestamp_ms=ts,
                agent_id=agent_id,
            )

        # ----- 用量（usage-token-v2-bigbang 重构）-----
        if kind == "usage":
            try:
                usage_dto = _build_usage_dto(payload)
            except ValueError:
                logger.warning("WSEventSink received invalid canonical usage payload")
                return None
            return UsageFrame(
                turn=turn,
                run_id=run_id,
                usage=usage_dto,
                timestamp_ms=ts,
                agent_id=agent_id,
            )

        # ----- 错误 -----
        if kind == "error":
            error_type = str(payload.get("type", "") or "")
            code: ErrorCode = _ERROR_TYPE_TO_CODE.get(error_type, "internal")
            message = str(payload.get("message", "") or "unknown error")
            return ErrorFrame(
                error_code=code,
                message=message,
                turn=event.turn,  # 可为 None
                timestamp_ms=ts,
                agent_id=agent_id,
            )

        # ----- 系统 notice -----
        if kind in {
            "evolution.review.started",
            "evolution.review.completed",
            "evolution.review.failed",
            "evolution.review.drain_timeout",
        }:
            return _translate_evolution_notice(
                kind=kind,
                payload=payload,
                run_id=run_id,
                timestamp_ms=ts,
                agent_id=agent_id,
            )

        # ----- interrupt-run-v0.1：run 被用户打断 -----
        # runner 顶层 ``except asyncio.CancelledError`` emit ``run.cancelled``
        # 后 fanout 到这里 → 转 :class:`RunInterruptedFrame` 推给 thread 名下
        # 所有 attach 的 ws（A tab 点 Stop → B tab 自动收到）。
        if kind == "run.cancelled":
            cancelled_id_raw = payload.get("cancelled_tool_call_id")
            cancelled_id: str | None = str(cancelled_id_raw) if cancelled_id_raw else None
            return RunInterruptedFrame(
                run_id=run_id,
                cancelled_at_turn=int(payload.get("cancelled_at_turn", turn) or turn),
                cancelled_tool_call_id=cancelled_id,
                cancel_reason=str(payload.get("cancel_reason", "user_interrupt")),
                timestamp_ms=ts,
                agent_id=agent_id,
            )

        # ----- 其它（run.start / run.end / approval.request audit /
        # tool.silently_allowed / memory.* / safety 决策事件 / llm.* / ...）
        # 一律静默丢；sink 不阻塞 runtime 演化。
        return None


def _safe_dict(raw: Any) -> dict[str, Any]:
    """把 payload 里可能是 dict / 其它的 arguments 字段安全转 dict。"""
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def _optional_str(raw: Any) -> str | None:
    """把 payload 里可能是 str / None / 其它的 error_message 字段保守化。"""
    if raw is None:
        return None
    return str(raw)


def _translate_evolution_notice(
    *,
    kind: str,
    payload: dict[str, Any],
    run_id: str,
    timestamp_ms: int,
    agent_id: str = "",
) -> SystemNoticeFrame:
    if kind == "evolution.review.started":
        review_id = _optional_str(payload.get("review_id")) or ""
        included_turns = _coerce_int_list(payload.get("included_turns"))
        title = "进化复盘"
        message = "后台复盘中"
        return SystemNoticeFrame(
            notice_key=_EVOLUTION_NOTICE_KEY,
            source=_EVOLUTION_NOTICE_SOURCE,
            status="started",
            title=title,
            message=message,
            details={
                "review_id": review_id,
                "session_id": _optional_str(payload.get("session_id")),
                "user_turn_count": _coerce_int(payload.get("user_turn_count")),
                "included_turns": included_turns,
                "timeout_seconds": _coerce_number(payload.get("timeout_seconds")),
            },
            icon="running",
            run_id=run_id,
            timestamp_ms=timestamp_ms,
            agent_id=agent_id,
        )

    if kind == "evolution.review.completed":
        review_id = _optional_str(payload.get("review_id")) or ""
        review_run_id = _optional_str(payload.get("review_run_id"))
        nutrients_written = _coerce_int(payload.get("nutrients_written"))
        review_summary = _optional_str(payload.get("review_summary"))
        nutrient_summaries = payload.get("nutrient_summaries") or []
        if not isinstance(nutrient_summaries, list):
            nutrient_summaries = []

        if review_summary and nutrient_summaries:
            titles = "\n".join(f"• {t}" for t in nutrient_summaries[:5])
            handled_message = f"{review_summary}\n{titles}"
        elif review_summary:
            handled_message = review_summary
        elif nutrients_written is not None:
            handled_message = f"发现 {nutrients_written} 条进化养料"
        else:
            handled_message = "已完成复盘并写入进化养料"
        title = "进化复盘"
        return SystemNoticeFrame(
            notice_key=_EVOLUTION_NOTICE_KEY,
            source=_EVOLUTION_NOTICE_SOURCE,
            status="completed",
            title=title,
            message=handled_message,
            details={
                "review_id": review_id,
                "review_run_id": review_run_id,
                "session_id": _optional_str(payload.get("session_id")),
                "write_status": _optional_str(payload.get("write_status")),
                "duration_ms": _coerce_int(payload.get("duration_ms")),
                "timeout_hit": bool(payload.get("timeout_hit", False)),
                "timeout_seconds": _coerce_number(payload.get("timeout_seconds")),
                "nutrients_written": nutrients_written,
                "written_nutrient_ids": _coerce_str_list(payload.get("written_nutrient_ids")),
                "review_summary": review_summary,
                "nutrient_summaries": nutrient_summaries,
            },
            icon="success",
            run_id=run_id,
            timestamp_ms=timestamp_ms,
            agent_id=agent_id,
        )

    if kind == "evolution.review.failed":
        error_kind = _optional_str(payload.get("error_kind"))
        error_message = _optional_str(payload.get("message"))
        message = "本轮未写入"
        if error_message:
            message = f"本轮未写入：{error_message}"
        elif error_kind:
            message = f"本轮未写入：{error_kind}"
        return SystemNoticeFrame(
            notice_key=_EVOLUTION_NOTICE_KEY,
            source=_EVOLUTION_NOTICE_SOURCE,
            status="failed",
            title="进化复盘",
            message=message,
            details={
                "review_id": _optional_str(payload.get("review_id")),
                "session_id": _optional_str(payload.get("session_id")),
                "error_kind": error_kind,
                "error_message": error_message,
                "duration_ms": _coerce_int(payload.get("duration_ms")),
                "timeout_hit": bool(payload.get("timeout_hit", False)),
                "timeout_seconds": _coerce_number(payload.get("timeout_seconds")),
            },
            icon="error",
            run_id=run_id,
            timestamp_ms=timestamp_ms,
            agent_id=agent_id,
        )

    pending_review_ids = _coerce_str_list(payload.get("pending_review_ids"))
    timeout_seconds = _coerce_number(payload.get("timeout_seconds"))
    pending_count = len(pending_review_ids)
    message = f"关闭前复盘超时，仍有 {pending_count} 条待处理复盘"
    return SystemNoticeFrame(
        notice_key=_EVOLUTION_NOTICE_KEY,
        source=_EVOLUTION_NOTICE_SOURCE,
        status="drain_timeout",
        title="进化复盘",
        message=message,
        details={
            "pending_review_ids": pending_review_ids,
            "timeout_seconds": timeout_seconds,
        },
        icon="warning",
        run_id=run_id,
        timestamp_ms=timestamp_ms,
        agent_id=agent_id,
    )


def _coerce_int(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    return None


def _coerce_number(raw: Any) -> int | float | None:
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, float)):
        return raw
    return None


def _coerce_int_list(raw: Any) -> list[int]:
    if not isinstance(raw, list):
        return []
    result: list[int] = []
    for item in raw:
        coerced = _coerce_int(item)
        if coerced is not None:
            result.append(coerced)
    return result


def _coerce_str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


__all__ = ["WSEventSink"]
