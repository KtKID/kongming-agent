"""审批 inbox 共享 helper。

manager（阶段 1）和 ApprovalBridge（阶段 2）都调用本模块；
保证 payload 字段在两条路径下一致，避免 contract 漂移。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

# 注：broadcaster 类型用 Any 避免 safety→web 跨层 import（.importlinter Contract 3
# layered-dependency 禁 safety→web）。运行期实际传入的是
# web.approvals.global_inbox.broadcaster.ApprovalInboxBroadcaster（duck typing）。
# 同款解耦在 inbox_event_sink.py 用 _BroadcasterProto Protocol。

logger = logging.getLogger(__name__)


def to_inbox_payload(
    *,
    channel: str,
    thread_id: str,
    request_id: str,
    tool_name: str,
    tool_input: Mapping[str, Any],
    cwd: str,
    arrived_at_ms: int,
    timeout_ms: int,
    severity: str = "standard",
    blocked_by_rule: str | None = None,
    danger: bool = False,
    remember_allowed: bool = False,
    remember_rule: Mapping[str, str | None] | None = None,
    auto_approve_at_ms: int | None = None,
    auto_reject_at_ms: int | None = None,
) -> dict[str, Any]:
    """构造 inbox.add 帧的 payload dict。

    所有通道共用此函数，确保字段与协议帧 schema 一致。
    broadcaster.emit_add 会自动补 kind 字段，所以本函数返回 dict 不含 kind。

    Args:
        channel: 通道名（claude_code / generic_chat / cron / evolution / cli）
        thread_id: 通道内 thread 标识
        request_id: 全局唯一请求 ID
        tool_name: 工具名（首字母大写，如 "Bash" / "Edit"）
        tool_input: 工具参数原始 dict
        cwd: 触发审批的工作目录
        arrived_at_ms: 到达后端的时间戳（毫秒）
        timeout_ms: 用户决策超时阈值（毫秒）；与 pending.timeout_ms 一致。
            前端在 ``autoApproveAtMs`` / ``autoRejectAtMs`` 均为 None（generic_chat
            阶段 1 场景）时用本字段做 fallback 倒计时；命名采用 camelCase
            与现有 ``autoApproveAtMs`` / ``autoRejectAtMs`` 风格一致。
        severity: "standard" / "elevated"；映射到 isElevated 布尔
        auto_approve_at_ms: 安全路径倒计时到点 ms；非 claude_code 通道传 None
        auto_reject_at_ms: 危险路径倒计时到点 ms；非 claude_code 通道传 None
        blocked_by_rule: 命中规则 ID；非 claude_code 通道传 None

    Returns:
        dict 形如 {"requestId": ..., "threadId": ..., ...}，可直接传 broadcaster.emit_add
    """
    return {
        "requestId": request_id,
        "threadId": thread_id,
        "toolName": tool_name,
        "toolInput": dict(tool_input),
        "timeoutMs": timeout_ms,
        "autoApproveAtMs": auto_approve_at_ms,
        "autoRejectAtMs": auto_reject_at_ms,
        "blockedByRule": blocked_by_rule,
        "isElevated": severity == "elevated",
        "channel": channel,
        "cwd": cwd,
        "arrivedAtMs": arrived_at_ms,
        "danger": danger,
        "rememberAllowed": remember_allowed,
        "rememberRule": dict(remember_rule) if remember_rule is not None else None,
    }


async def emit_remove_safe(
    broadcaster: Any,
    request_id: str,
    reason: str,
) -> None:
    """对 broadcaster.emit_remove 的安全包装：吞所有异常。

    审批主流程绝不能因 inbox 广播异常而断（与 ApprovalBridge._inbox_emit_remove_safe
    一致的设计：fan-out 失败只 warn，不向上抛）。

    Args:
        broadcaster: 已实例化的 ApprovalInboxBroadcaster
        request_id: 对应已发出的 add 帧
        reason: "user_decided" | "timeout" | "cancelled" | "auto_allowed" | "auto_rejected"
    """
    try:
        await broadcaster.emit_remove(request_id, reason)
    except Exception:
        logger.exception(
            "inbox.emit_remove_safe failed (request_id=%s reason=%s, non-fatal)",
            request_id,
            reason,
        )
