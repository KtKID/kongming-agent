"""web — cron 投递 sink (v0.3 M4)。

cron 触发完成后通过 :class:`web.websocket.cron.CronWSBroker` 把 ``cron.run.completed``
事件 broadcast 给所有当前订阅 ``/ws/cron`` 的 web 客户端。

设计：

- 依赖注入：构造时传入 broker；不直接 import 单例 helper（便于测试隔离）
- 落盘动作不归本 sink：``ScheduledRun`` 在 :class:`scheduler.execution_bridge.ExecutionBridge`
  的 ``supersede_and_append_run`` 阶段已落盘（M3 完成）；本 sink 仅做"实时
  推送"
- broadcast 失败不抛：broker 内部已 ``return_exceptions=True``；本 sink 仍
  返回 ``DeliveryResult.delivered()``，因为"投递"语义指"消息已交给运输层
  （broker）"，不等于"所有客户端 ack 收到"

事件 payload 字段（与 M7 前端解析对齐）：

- ``frame_type: "cron.run.completed"``（protocol-frame-type-unify-v0.2 后从
  ``kind`` 切到 ``frame_type``，与全局 wire 协议对齐）
- ``task_id`` / ``task_name`` / ``run_id``
- ``final_message``: 完整的（已被 ``_classify_result`` 截断到 excerpt limit）消息
- ``delivered_at_iso``: 投递时间 ISO8601；用 ``run.finished_at`` 替代（一致性）
- ``scheduled_for``: 原计划触发时间 ISO8601
"""

from __future__ import annotations

import logging
from typing import Any

from scheduler.delivery import DeliveryResult, DeliverySink
from scheduler.domain import ScheduledRun, ScheduledTask
from web.websocket.cron import CronWSBroker

logger = logging.getLogger(__name__)


class WebDeliverySink(DeliverySink):
    """v0.3 cron-delivery M4：Web 投递 sink。

    通过 :class:`CronWSBroker` 把 ``cron.run.completed`` 事件 broadcast 给
    所有当前订阅 ``/ws/cron`` 的客户端。

    使用方式（web/run.py 装配）::

        from web.websocket.cron import get_broker
        from web.cron_delivery import WebDeliverySink
        from scheduler.delivery import DeliveryDispatcher

        web_sink = WebDeliverySink(get_broker())
        dispatcher = DeliveryDispatcher(web_sink=web_sink)
        # 透传给 build_cron_execution_bridge(dispatcher=dispatcher)
    """

    def __init__(self, broker: CronWSBroker) -> None:
        self._broker = broker

    async def deliver(
        self,
        task: ScheduledTask,
        run: ScheduledRun,
        final_message: str,
    ) -> DeliveryResult:
        """把 ``cron.run.completed`` 事件 broadcast 给所有订阅者。

        broker.broadcast 失败不抛（内部已 catch）；本方法稳定返回
        ``DeliveryResult.delivered()``。
        """
        delivery_target = task.delivery.target if task.delivery is not None else None
        await self._broker.broadcast(
            {
                "frame_type": "cron.run.completed",
                "task_id": task.task_id,
                "task_name": task.name,
                "run_id": run.run_id,
                "final_message": final_message,
                "delivered_at_iso": run.finished_at,
                "scheduled_for": run.scheduled_for,
                "delivery_target": delivery_target,
                # v0.5 web-cron-router：下游前端不必 N+1 refetch
                "next_run_at": task.next_run_at,
                "status": run.status.value,
            }
        )
        return DeliveryResult.delivered()


class ThreadTargetSink:
    """v0.4: 投递到 thread — 解析 target 前缀，调 ThreadManager API。"""

    def __init__(self, thread_manager: Any) -> None:
        self._tm = thread_manager

    async def deliver_to_target(self, target: str, task_name: str, message: str) -> bool:
        """按 target 前缀路由到具体投递实现。

        当前仅支持 ``thread:<thread_id>`` 前缀；其他前缀静默返回 False。
        """
        if not target.startswith("thread:"):
            return False
        thread_id = target[len("thread:") :]
        formatted = f"\U0001f4cc [cron:{task_name}] {message}"
        try:
            result = await self._tm.append_cron_message(thread_id, formatted)
            return bool(result)
        except Exception as exc:
            logger.warning(
                "ThreadTargetSink: deliver_to_target failed for %s: %s",
                target,
                exc,
            )
            return False


__all__ = ["ThreadTargetSink", "WebDeliverySink"]
