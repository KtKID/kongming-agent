"""InboxEventSink：把 manager 的审批事件 fan-out 到 web inbox 浮窗。

v2-inbox 已建好 broadcaster + ws 端点 + 前端组件；本 Sink 只是
``ApprovalManager`` → ``ApprovalInboxBroadcaster`` 的适配器，不重复实现
fan-out / 重连补包 / 协议帧等逻辑。

设计真源：``docs/safety-approval-manager-v0.5/10-architecture.md#5.1``

跨层依赖说明：
- safety 包按 ``.importlinter`` 规则不直接 import web；``broadcaster`` 实例通过 DI
  （构造函数）传入。本模块只引入一个本地 ``Protocol`` 描述 broadcaster 的最小接口
  （duck typing），运行期由 ``src/web/run.py`` 装配点把
  ``web.global_approvals.broadcaster.ApprovalInboxBroadcaster`` 实例注进来——
  结构匹配，零反向 import。
- ``src/web/run.py`` 装配时实例化
  ``InboxEventSink(broadcaster=get_inbox_broadcaster(), manager=mgr)``
  并调 ``mgr.register_event_sink(sink)``。

阶段演进：
- 阶段 1（当前）：仅 generic_chat 通道；emit_approval_removed 不 per-request 注销
  callback（详见 :meth:`InboxEventSink.emit_approval_removed` docstring 折中说明）。
- 阶段 4：``CLIEventSink`` 是并列实现，Protocol 签名不变（spec 60-risk R4）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol, cast

from safety.inbox_helpers import emit_remove_safe, to_inbox_payload

if TYPE_CHECKING:
    from safety.approval_manager import ApprovalManager, _PendingApproval

logger = logging.getLogger(__name__)


class _BroadcasterProto(Protocol):
    """broadcaster 的最小接口（duck typing，避免 safety → web 反向 import）。

    实际类是 ``web.global_approvals.broadcaster.ApprovalInboxBroadcaster``，
    结构匹配本 Protocol，无需显式继承。
    """

    async def emit_add(self, payload: dict[str, Any]) -> None:
        """fan-out add 帧到所有订阅者。"""
        ...

    async def emit_remove(self, request_id: str, reason: str) -> None:
        """fan-out remove 帧到所有订阅者。"""
        ...

    def register_resolve_target(
        self,
        thread_id: str,
        callback: Any,
    ) -> None:
        """注册 ``thread_id → resolve callback`` 路由项。"""
        ...

    def unregister_resolve_target(
        self,
        thread_id: str,
        callback: Any,
    ) -> None:
        """注销 ``thread_id → resolve callback`` 路由项。"""
        ...


class InboxEventSink:
    """实现 ``safety.approval_manager.ApprovalEventSink`` Protocol。

    把 manager 事件转发到 broadcaster（add / remove 帧），同时把 manager.resolve
    callback 注册到 broadcaster，让前端通过 inbox.resolve 帧能路由回 manager。

    生命周期：
    - 装配期实例化一次（per-process），注入 broadcaster + manager 引用
    - ``emit_approval_required``：调 ``broadcaster.emit_add(payload)`` +
      ``register_resolve_target``
    - ``emit_approval_removed``：调 ``broadcaster.emit_remove(req_id, reason)``
      （per-request unregister 留待阶段 2，详见方法 docstring）

    **异常吞掉策略**：所有 emit 失败只 ``logger.exception`` + return，
    绝不向上抛——审批主流程不能因 broadcaster 异常而断
    （与 manager 用 ``asyncio.gather(..., return_exceptions=True)`` 包裹
    sink 调用的设计一致；本 Sink 自己再吞一层做 belt-and-suspenders）。
    """

    def __init__(
        self,
        *,
        broadcaster: _BroadcasterProto,
        manager: ApprovalManager,
    ) -> None:
        """构造 sink。

        Args:
            broadcaster: web 层 broadcaster 实例（装配点注入；safety 不 import web）。
                结构上需满足 :class:`_BroadcasterProto` 的 4 个方法。
            manager: 反向引用 manager，用于把 ``manager.resolve`` 作为 resolve
                callback 注册到 broadcaster——前端 ``approval.inbox.resolve``
                帧按 thread_id 路由回 manager 时用。
        """
        self._broadcaster = broadcaster
        self._manager = manager

    async def emit_approval_required(self, *, pending: _PendingApproval) -> None:
        """fan-out add 帧到所有 ws + 注册 resolve callback 到 broadcaster。

        步骤：

        1. ``inbox_helpers.to_inbox_payload`` 把 pending 字段映射成 add 帧 payload
           （channel/requestId/threadId/toolName/toolInput/isElevated/cwd/
           arrivedAtMs/autoApproveAtMs/autoRejectAtMs/blockedByRule）。
        2. ``broadcaster.register_resolve_target(thread_id, manager.resolve)``
           让前端 inbox.resolve 帧能按 thread_id 路由回 manager。
        3. ``broadcaster.emit_add(payload)`` fan-out 给所有订阅 ws。

        Args:
            pending: manager 内部创建的 :class:`_PendingApproval`，
                字段已完整（severity / matched_rule / auto_*_at_ms 等）。

        异常吞掉：任何步骤抛 → ``logger.exception`` + return（不向上抛）。
        """
        try:
            # pending.timeout_ms 理论上 manager.request 已填实数（_PendingApproval
            # 构造时传 actual_timeout_ms）；此处兜底 60_000 防御未来调用方
            # 直接构造 _PendingApproval 时漏字段，保证前端 timeoutMs 始终是 int。
            timeout_ms = pending.timeout_ms if pending.timeout_ms is not None else 60_000
            payload = to_inbox_payload(
                channel=pending.channel,
                thread_id=pending.thread_id,
                request_id=pending.request_id,
                tool_name=pending.tool_name,
                tool_input=pending.tool_input,
                cwd=pending.cwd,
                arrived_at_ms=pending.arrived_at_ms,
                timeout_ms=timeout_ms,
                severity=pending.severity,
                auto_approve_at_ms=pending.auto_approve_at_ms,
                auto_reject_at_ms=pending.auto_reject_at_ms,
                blocked_by_rule=pending.matched_rule,
            )
            self._broadcaster.register_resolve_target(
                pending.thread_id,
                self._manager.resolve,
            )
            await self._broadcaster.emit_add(payload)
        except Exception:
            logger.exception(
                "InboxEventSink.emit_approval_required failed "
                "(request_id=%s thread_id=%s, non-fatal)",
                pending.request_id,
                pending.thread_id,
            )

    async def emit_approval_removed(
        self,
        *,
        request_id: str,
        reason: str,
    ) -> None:
        """fan-out remove 帧到所有 ws。

        通过 :func:`safety.inbox_helpers.emit_remove_safe` 调
        ``broadcaster.emit_remove``（该 helper 已吞所有异常）。

        Args:
            request_id: 对应 ``emit_approval_required`` 时的 ``pending.request_id``
            reason: ``"user_decided"`` / ``"timeout"`` / ``"cancelled"``

        **设计折中（阶段 1 简化）**：
        ``register_resolve_target`` 在 ``emit_approval_required`` 注册的是
        ``manager.resolve`` —— per-process 单例方法。同一 thread 多个 pending
        会重复注册同一 callback，覆盖语义本来就只保留一份。
        ``broadcaster.unregister_resolve_target`` 是 thread 级注销，
        只有当某 thread 名下所有 pending 都结束后注销才有意义。

        本阶段不在 ``emit_approval_removed`` 里做 per-request 的 unregister：
        留 ``broadcaster._resolve_targets`` 内 callback 残留无害——
        ``manager.resolve`` 在 pending 已 done 时返回 ``False``，幂等。
        阶段 2 ApprovalBridge 接入时再统一处理 thread 级生命周期（跟 ws 一致）。
        """
        # cast 到 Any：inbox_helpers.emit_remove_safe 标注的是
        # ``ApprovalInboxBroadcaster`` 具体类，但本 Sink 持的是 _BroadcasterProto
        # （DI 解耦），二者结构兼容（都有 emit_remove）；用 cast 满足 mypy。
        await emit_remove_safe(cast(Any, self._broadcaster), request_id, reason)


__all__ = ["InboxEventSink"]
