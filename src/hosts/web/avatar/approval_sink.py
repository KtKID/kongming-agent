"""Avatar 审批消息接收器。

本脚本把 ApprovalManager 的 pending 审批事件注册到 Avatar message registry。
关键流程是 Web 装配层把本 Sink 和 InboxEventSink 并列注册到 ApprovalManager；
当工具调用进入人工审批时，本 Sink 写入一条 source=approval 的 Avatar 消息。
关键类职责：只做 Web 层 DTO 映射和容错，不参与审批决策。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from hosts.web.avatar.manager import AvatarManager
from hosts.web.avatar.models import (
    AvatarMessageAction,
    AvatarMessageInput,
    AvatarMessageLevel,
)

if TYPE_CHECKING:
    from safety.approval.manager import _PendingApproval

logger = logging.getLogger(__name__)

_TOOL_INPUT_PREVIEW_LIMIT = 1000
_BODY_LIMIT = 2000


def _truncate(value: str, limit: int) -> str:
    """截断字符串，保证写入 Avatar DTO 时不超过字段上限。"""
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _tool_input_preview(tool_input: dict[str, Any]) -> str:
    """生成工具参数预览。

    关键输入：ApprovalManager pending 中的 tool_input。
    关键输出：短 JSON 字符串，供 Avatar 消息 body 和 metadata 展示。
    """
    try:
        raw = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
    except TypeError:
        raw = repr(tool_input)
    return _truncate(raw, _TOOL_INPUT_PREVIEW_LIMIT)


def _body_for_pending(pending: _PendingApproval) -> str:
    """生成 Avatar 消息正文。

    关键输入：pending 审批上下文。
    关键输出：不超过 AvatarMessageInput.body 上限的正文。
    """
    parts = [
        f"工具 {pending.tool_name} 等待审批。",
        f"cwd: {pending.cwd}" if pending.cwd else "",
        f"input: {_tool_input_preview(pending.tool_input)}",
    ]
    return _truncate("\n".join(part for part in parts if part), _BODY_LIMIT)


class AvatarApprovalSink:
    """把 pending 审批注册为 Avatar 消息。

    职责：实现 ApprovalEventSink 形状，把 ``emit_approval_required`` 映射为
    ``AvatarManager.register_message``。
    关键输入：ApprovalManager 的 _PendingApproval。
    关键输出：Avatar registry 中 source=approval 的 active 消息。
    """

    def __init__(self, avatar_manager: AvatarManager) -> None:
        """初始化 AvatarApprovalSink。

        关键输入：Web app.state.avatar_manager。
        关键输出：可注册到 ApprovalManager 的事件 sink。
        """
        self._avatar_manager = avatar_manager

    async def emit_approval_required(self, *, pending: _PendingApproval) -> None:
        """注册一条 Avatar 审批消息。

        关键输入：pending 审批上下文。
        关键输出：写入 Avatar message registry；失败只记录日志。
        """
        try:
            is_elevated = pending.severity == "elevated"
            self._avatar_manager.register_message(
                AvatarMessageInput(
                    source="approval",
                    title=f"{pending.tool_name} 等待审批",
                    body=_body_for_pending(pending),
                    level=AvatarMessageLevel.ERROR if is_elevated else AvatarMessageLevel.WARNING,
                    priority=95 if is_elevated else 90,
                    thread_id=pending.thread_id,
                    request_id=pending.request_id,
                    action=AvatarMessageAction(
                        type="open_approval",
                        label="处理审批",
                        target=pending.request_id,
                        payload={
                            "threadId": pending.thread_id,
                            "requestId": pending.request_id,
                            "channel": pending.channel,
                        },
                    ),
                    dedupe_key=f"approval:{pending.request_id}",
                    metadata={
                        "channel": pending.channel,
                        "cwd": pending.cwd,
                        "toolName": pending.tool_name,
                        "toolInputPreview": _tool_input_preview(pending.tool_input),
                        "isElevated": is_elevated,
                        "matchedRule": pending.matched_rule,
                        "autoApproveAtMs": pending.auto_approve_at_ms,
                        "autoRejectAtMs": pending.auto_reject_at_ms,
                        "timeoutMs": pending.timeout_ms,
                        "arrivedAtMs": pending.arrived_at_ms,
                    },
                )
            )
        except Exception:
            logger.exception(
                "AvatarApprovalSink.emit_approval_required failed "
                "(request_id=%s thread_id=%s, non-fatal)",
                pending.request_id,
                pending.thread_id,
            )

    async def emit_approval_removed(self, *, request_id: str, reason: str) -> None:
        """审批结束事件当前不改变 Avatar 消息状态。

        关键输入：审批 request_id 和结束原因。
        关键输出：无。XSpace Avatar 仍通过 ack/consume 控制消息生命周期。
        """
        return None


__all__ = ["AvatarApprovalSink"]
