"""ApprovalInboxBroadcaster：全局审批 inbox 事件路由器（smart-approval-v2-inbox）。

设计目的：让审批弹窗不再绑定 thread-WS（per-thread modal），而是浮在所有
打开 web app 的浏览器 tab 上（跨 thread 聚合）。

结构（per-process 单例）：

- ``_subscribers``：所有订阅 ``/ws/thread-status`` 端点的 WebSocket 集合
  （per-tab；复用现有 thread-status 端点的连接管理）
- ``_pending_snapshot``：当前 pending 审批的完整 add payload dict
  （重连补包用——浏览器重连时 push snapshot 让前端 inbox 与后端一致）
- ``_bridge_registry``：``thread_id → ApprovalBridge`` 映射
  （inbox.resolve 帧从前端发来时按 threadId 路由回正确的 bridge.resolve）

依赖方向：
- broadcaster **不持有** future 真源——future 仍在 ``ApprovalBridge._pending`` 里
- broadcaster 只做事件路由（add/remove 帧 fan-out）+ snapshot 重连补包
- ``ApprovalBridge`` import 本模块，调 emit_add/emit_remove；ws_handler import 本
  模块，调 attach/detach/resolve；都是单向依赖

模块独立于 thread_status_ws.py，按"高内聚 / 单一职责"原则保持分离；
两个 broadcaster 共用 ``/ws/thread-status`` 端点但互不耦合。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import WebSocket

    from web.claude_code.approval import ApprovalBridge

logger = logging.getLogger(__name__)


class ApprovalInboxBroadcaster:
    """全局审批 inbox 事件路由器（单例）。

    线程安全：所有 mutating 操作都在 ``asyncio.Lock`` 下；
    emit_add/emit_remove 失败不抛——审批主流程绝不能因 broadcaster 异常断。
    """

    def __init__(self) -> None:
        self._subscribers: set[Any] = set()
        # request_id → add payload（snapshot 补包用；payload 含 thread_id / autoApproveAtMs / autoRejectAtMs / ...）
        self._pending_snapshot: dict[str, dict[str, Any]] = {}
        # thread_id → ApprovalBridge（resolve 路由用；同 thread_id 多 bridge 时只留最新）
        self._bridge_registry: dict[str, ApprovalBridge] = {}
        self._lock = asyncio.Lock()

    # ----- 订阅者管理 -----

    @property
    def subscriber_count(self) -> int:
        """当前订阅者数（仅供测试 / 监控，不加锁）。"""
        return len(self._subscribers)

    @property
    def pending_count(self) -> int:
        """当前 pending 审批数（仅测试 / 监控用）。"""
        return len(self._pending_snapshot)

    async def attach(self, ws: WebSocket) -> None:
        """加入订阅池（在 ws.accept() 之后调用）。重复 attach 幂等。"""
        async with self._lock:
            self._subscribers.add(ws)

    async def detach(self, ws: WebSocket) -> None:
        """移出订阅池。不存在的连接忽略。"""
        async with self._lock:
            self._subscribers.discard(ws)

    async def push_snapshot(self, ws: WebSocket) -> None:
        """连接建立时把当前所有 pending 审批 push 给单个 ws。

        发送 ``approval.inbox.snapshot`` 帧，包含 ``items`` 列表（payload 与
        emit_add 一致）。前端 store 用 ``applySnapshot`` 全量替换。
        """
        async with self._lock:
            items = list(self._pending_snapshot.values())
        frame = {"kind": "approval.inbox.snapshot", "items": items}
        try:
            await ws.send_json(frame)
        except Exception:
            logger.warning("inbox.push_snapshot: send_json failed; detaching")
            await self.detach(ws)

    # ----- 事件 fan-out -----

    async def emit_add(self, payload: dict[str, Any]) -> None:
        """新审批到达：写入 snapshot + fan-out add 帧给所有订阅者。

        Args:
            payload: 形如
                ``{"requestId", "threadId", "threadDisplayName?", "toolName",
                "toolInput", "autoApproveAtMs", "autoRejectAtMs",
                "blockedByRule", "isElevated", "channel", "cwd",
                "arrivedAtMs"}``。**payload 内不应含 "kind" 字段**——
                由本方法补成 ``approval.inbox.add``。
        """
        request_id = payload.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            logger.warning("inbox.emit_add: missing requestId in payload, skip")
            return
        async with self._lock:
            self._pending_snapshot[request_id] = dict(payload)
            subscribers = list(self._subscribers)
        frame = {"kind": "approval.inbox.add", **payload}
        await self._broadcast(subscribers, frame)

    async def emit_remove(self, request_id: str, reason: str) -> None:
        """审批结束：从 snapshot 清掉 + fan-out remove 帧。

        Args:
            request_id: 对应的 ``request_id``
            reason: ``"user_decided" | "timeout" | "cancelled"``
                （任何 asyncio cancel 来源都映射到 cancelled，前端不区分）
        """
        async with self._lock:
            self._pending_snapshot.pop(request_id, None)
            subscribers = list(self._subscribers)
        frame = {"kind": "approval.inbox.remove", "requestId": request_id, "reason": reason}
        await self._broadcast(subscribers, frame)

    async def _broadcast(self, subscribers: list[Any], frame: dict[str, Any]) -> None:
        """对给定 subscriber 列表 fan-out 帧；失败连接自动 detach。"""
        if not subscribers:
            return
        await asyncio.gather(
            *(self._send_one(ws, frame) for ws in subscribers),
            return_exceptions=True,
        )

    async def _send_one(self, ws: WebSocket, frame: dict[str, Any]) -> None:
        try:
            await ws.send_json(frame)
        except Exception:
            await self.detach(ws)

    # ----- bridge registry（inbox.resolve 路由用）-----

    def register_bridge(self, thread_id: str, bridge: ApprovalBridge) -> None:
        """注册 thread_id → bridge 映射（被 route.py 在构造 bridge 时调用）。

        同 thread_id 多次注册：覆盖（多 tab 场景下取最新连接的 bridge）。
        """
        if not thread_id:
            return
        self._bridge_registry[thread_id] = bridge

    def unregister_bridge(self, thread_id: str, bridge: ApprovalBridge) -> None:
        """注销映射。**只在 registry 里就是这个 bridge 时才删**——防止被新连接
        覆盖后又被老连接的 unregister 误删。
        """
        if not thread_id:
            return
        current = self._bridge_registry.get(thread_id)
        if current is bridge:
            self._bridge_registry.pop(thread_id, None)

    def get_bridge(self, thread_id: str) -> ApprovalBridge | None:
        """按 thread_id 查 bridge；用于路由 inbox.resolve 帧。"""
        return self._bridge_registry.get(thread_id)

    def resolve(
        self,
        thread_id: str,
        request_id: str,
        decision: dict[str, Any],
    ) -> bool:
        """前端 ``approval.inbox.resolve`` 帧到达时，路由回正确 bridge。

        Returns:
            ``True`` 找到 bridge 且成功 set future；``False`` bridge 不存在
            或 future 已 done（重复 resolve / 超时后）。
        """
        bridge = self._bridge_registry.get(thread_id)
        if bridge is None:
            logger.warning(
                "inbox.resolve: no bridge for thread_id=%s (already disconnected?)",
                thread_id,
            )
            return False
        return bridge.resolve(request_id, decision)


# ---------------------------------------------------------------------------
# 模块级单例
# ---------------------------------------------------------------------------

_inbox_singleton: ApprovalInboxBroadcaster | None = None


def get_inbox_broadcaster() -> ApprovalInboxBroadcaster:
    """获取或创建单例。"""
    global _inbox_singleton
    if _inbox_singleton is None:
        _inbox_singleton = ApprovalInboxBroadcaster()
    return _inbox_singleton


def reset_inbox_broadcaster_for_testing() -> None:
    """**仅测试用**：重置单例到 None。"""
    global _inbox_singleton
    _inbox_singleton = None
