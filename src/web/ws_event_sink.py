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
- **不重复推 ``approval.request``**：runner 发出 ``approval.request``
  Event 时，:class:`web.host_adapter.WebHostAdapter.prompt_approval`
  已经推了 ``ApprovalRequestFrame``；sink 这里识别但不推（避免双发）。
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
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from core.contracts import Event
from web.protocol import (
    ApprovalDecisionFrame,
    ApprovalOutcome,
    ContentDeltaFrame,
    ErrorCode,
    ErrorFrame,
    ReasoningDeltaFrame,
    ToolCallEndFrame,
    ToolCallStartFrame,
    TurnEndFrame,
    TurnStartFrame,
    UsageFrame,
)
from web.protocol._base import _S2CFrameBase
from web.protocol.ws_frames import SystemNoticeFrame

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


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


class UsagePersistSink:
    """每 turn 把 usage 增量写盘的 EventSink。

    只关心 ``kind="usage"`` 事件；其余静默跳过。
    由 ThreadManager._build_cell 注册到 sinks 列表，替代
    _run_once_safely 中的 run 结束一次性持久化，保证刷新页面
    后 hydrateUsageFromThreads 能拿到最新累计值。
    """

    def __init__(
        self,
        thread_id: str,
        persist_fn: Callable[..., Awaitable[Any]],
    ) -> None:
        self._thread_id = thread_id
        self._persist_fn = persist_fn

    async def emit(self, event: Event) -> None:
        if event.kind != "usage":
            return
        payload = event.payload or {}
        prompt = int(payload.get("prompt_tokens", 0) or 0)
        completion = int(payload.get("completion_tokens", 0) or 0)
        total = int(payload.get("total_tokens", 0) or 0)
        if prompt == 0 and completion == 0 and total == 0:
            return
        try:
            await self._persist_fn(
                self._thread_id,
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
            )
        except Exception:
            logger.warning(
                "UsagePersistSink: failed to persist usage for thread %s",
                self._thread_id,
                exc_info=True,
            )


class WSEventSink:
    """实现 :class:`core.contracts.EventSink` 的 WS 推送 sink。

    Attributes:
        _ws: 当前 WS 连接。可在 :meth:`attach_ws` 时替换。
        _closed: 标记 ws 已断 / 不可写。closed 后所有 emit 静默丢。
    """

    def __init__(self, ws: Any) -> None:
        self._ws: Any = ws
        self._closed = False

    async def emit(self, event: Event) -> None:
        """实现 EventSink 协议；把 event 翻成帧推 WS。"""
        if self._closed:
            return
        frame = self._translate(event)
        if frame is None:
            return
        try:
            await self._ws.send_json(frame.model_dump())
        except Exception as exc:
            logger.warning(
                "WSEventSink ws.send_json failed for event.kind=%s: %s; marking closed",
                event.kind,
                exc,
            )
            self._closed = True
            with contextlib.suppress(Exception):
                close_call = self._ws.close()
                # 兼容同步 close (mock) / 异步 close (websocket)
                if asyncio.iscoroutine(close_call):
                    await close_call

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

        # ----- 流式增量 -----
        if kind == "content.delta":
            return ContentDeltaFrame(
                delta=str(payload.get("delta", "")),
                turn=turn,
                seq=int(payload.get("seq", 0) or 0),
                run_id=run_id,
                timestamp_ms=ts,
            )
        if kind == "reasoning.delta":
            return ReasoningDeltaFrame(
                delta=str(payload.get("delta", "")),
                turn=turn,
                seq=int(payload.get("seq", 0) or 0),
                run_id=run_id,
                timestamp_ms=ts,
            )

        # ----- turn 边界 -----
        if kind == "turn.start":
            return TurnStartFrame(turn=turn, run_id=run_id, timestamp_ms=ts)
        if kind == "turn.end":
            return TurnEndFrame(turn=turn, run_id=run_id, timestamp_ms=ts)

        # ----- 工具调用 -----
        if kind == "tool.call.start":
            return ToolCallStartFrame(
                tool_name=str(payload.get("tool_name", "")),
                call_id=str(payload.get("call_id", "")),
                turn=turn,
                arguments=_safe_dict(payload.get("arguments")),
                run_id=run_id,
                timestamp_ms=ts,
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
            )

        # ----- 审批 -----
        # ``approval.request`` 不在这里推（HostAdapter.prompt_approval 已推）
        # 但 ``approval.decision`` 走这里：runner 在审批通过 / 拒绝 / 取消后 emit。
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
            )

        # ----- 用量 -----
        # runtime 在 turn.end 的 metadata.usage 里携带 token 用量；某些版本
        # 也单独 emit ``usage`` event。本 sink 接受 ``usage`` kind，把字段映射到
        # UsageFrame；其它字段缺失时给 0（不抛）。
        if kind == "usage":
            return UsageFrame(
                prompt_tokens=int(payload.get("prompt_tokens", 0) or 0),
                completion_tokens=int(payload.get("completion_tokens", 0) or 0),
                total_tokens=int(payload.get("total_tokens", 0) or 0),
                turn=turn,
                run_id=event.run_id or "",
                timestamp_ms=ts,
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
            )

        # ----- 其它（run.start / run.end / approval.request /
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
        )

    if kind == "evolution.review.completed":
        review_id = _optional_str(payload.get("review_id")) or ""
        review_run_id = _optional_str(payload.get("review_run_id"))
        nutrients_written = _coerce_int(payload.get("nutrients_written"))
        handled_message = (
            f"发现 {nutrients_written} 条进化养料"
            if nutrients_written is not None
            else "已完成复盘并写入进化养料"
        )
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
            },
            icon="success",
            run_id=run_id,
            timestamp_ms=timestamp_ms,
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
