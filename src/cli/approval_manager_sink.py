"""CLI 审批管理器事件接收器。

把审批管理器里的待处理审批转回现有 CLI 终端审批提示。这里只处理
``channel="cli"`` 的审批，因此同一个审批管理器单例仍可在组合进程里挂
Web 审批收件箱接收器。
"""

from __future__ import annotations

import logging
from typing import Any

from core.contracts import ApprovalAction, ApprovalRequest
from safety.approval_manager import ApprovalManager, _PendingApproval
from tools.approval import PromptActionFn

logger = logging.getLogger(__name__)


class CLIApprovalEventSink:
    """把 ``ApprovalManager`` 的待处理请求交给终端用户审批。"""

    def __init__(self, manager: ApprovalManager, action_prompt: PromptActionFn) -> None:
        self._manager = manager
        self._action_prompt = action_prompt

    async def emit_approval_required(self, *, pending: _PendingApproval) -> None:
        """提示终端用户审批，并回写审批管理器里的待处理请求。"""
        if pending.channel != "cli":
            return

        try:
            request = _pending_to_request(pending)
        except Exception:
            logger.exception(
                "CLI 审批请求投影失败，自动拒绝。request_id=%s tool=%s",
                pending.request_id,
                pending.tool_name,
            )
            self._manager.resolve(pending.request_id, {"allow": False})
            return

        try:
            action = await self._action_prompt(request)
        except Exception:
            logger.exception("CLI 审批提示失败，自动拒绝。request_id=%s", pending.request_id)
            action = ApprovalAction.REJECT

        self._manager.resolve(pending.request_id, _action_to_manager_payload(action))

    async def emit_approval_removed(self, *, request_id: str, reason: str) -> None:
        """终端 UI 无需移除事件；请求完成与用户输入同步发生。"""
        del request_id, reason


def _pending_to_request(pending: _PendingApproval) -> ApprovalRequest:
    metadata: dict[str, Any] = dict(pending.metadata or {})
    metadata["cwd"] = pending.cwd
    metadata["approval_channel"] = pending.channel
    metadata["approval_request_id"] = pending.request_id
    metadata["severity"] = pending.severity
    metadata["timeout_ms"] = pending.timeout_ms
    metadata["auto_approve_at_ms"] = pending.auto_approve_at_ms
    metadata["auto_reject_at_ms"] = pending.auto_reject_at_ms
    if pending.matched_rule is not None:
        metadata["matched_rule"] = pending.matched_rule
        metadata["blocked_by_rule"] = pending.matched_rule

    return ApprovalRequest(
        run_id=str(metadata.get("run_id") or ""),
        session_id=str(metadata.get("session_id") or pending.thread_id),
        turn=_coerce_int(metadata.get("turn"), default=0),
        call_id=str(metadata.get("call_id") or pending.request_id),
        tool_name=pending.tool_name,
        arguments=_coerce_tool_input(pending.tool_input),
        reason=str(metadata.get("reason") or "") or None,
        metadata=metadata,
    )


def _action_to_manager_payload(action: ApprovalAction) -> dict[str, Any]:
    if action in {
        ApprovalAction.ACCEPT_ONCE,
        ApprovalAction.ACCEPT_FOR_SESSION,
        ApprovalAction.ACCEPT_PERSIST,
    }:
        return {"allow": True}
    return {"allow": False}


def _coerce_int(value: object, *, default: int) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _coerce_tool_input(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    raise TypeError(f"pending.tool_input 必须是 dict，实际为 {type(value).__name__}")


__all__ = ["CLIApprovalEventSink"]
