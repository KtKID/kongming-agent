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
        """重连时替换 WS 引用 + 重置 ``_closed``。"""
        self._ws = new_ws
        self._closed = False

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


__all__ = ["WSEventSink"]
