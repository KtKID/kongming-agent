"""smart-approval-v2-inbox 全局审批 inbox 模块。

独立 package，对外只暴露 ``ApprovalInboxBroadcaster`` 单例 + payload helper。

内部维护：
- subscribers：所有订阅 ``/ws/thread-status`` 的 WebSocket 集合（per-tab）
- pending_snapshot：当前所有 pending 审批的 add payload dict（重连补包用）
- bridge_registry：``thread_id → ApprovalBridge``，inbox.resolve 路由用

不持有 future 真源——future 仍在 ApprovalBridge._pending 里；本模块只做事件路由 + snapshot 补包。
"""

from __future__ import annotations

from web.approvals.global_inbox.broadcaster import (
    ApprovalInboxBroadcaster,
    get_inbox_broadcaster,
    reset_inbox_broadcaster_for_testing,
)

__all__ = [
    "ApprovalInboxBroadcaster",
    "get_inbox_broadcaster",
    "reset_inbox_broadcaster_for_testing",
]
