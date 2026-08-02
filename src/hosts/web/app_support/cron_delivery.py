"""web — cron 投递 sink (v0.3 M4)。

cron 触发完成后通过 :class:`web.websocket.cron.CronWSBroker` 把 ``cron.run.completed``
事件 broadcast 给所有当前订阅 ``/ws/cron`` 的 web 客户端。

设计：

- 依赖注入：构造时传入 broker；不直接 import 单例 helper（便于测试隔离）
- 落盘动作不归本 sink：``ScheduledRun`` 在 :class:`application.scheduled_runs.execution_bridge.ExecutionBridge`
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
import time
import uuid
from typing import Any, cast

from typing_extensions import override

from hosts.web.protocol import (
    CronMessageAppendedFrame,
    CronRunCompletedFrame,
    CronRunFinishedFrame,
    CronRunStartedFrame,
    CronRunTerminalStatusValue,
)
from hosts.web.websocket.cron import CronWSBroker
from scheduler.delivery import DeliveryResult, DeliverySink
from scheduler.domain import RunStatus, ScheduledRun, ScheduledTask

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _terminal_status_value(status: RunStatus) -> CronRunTerminalStatusValue:
    """把已结束的 scheduler 状态收窄为 Web terminal wire 合同。"""
    if status is RunStatus.RUNNING:
        raise ValueError("running status cannot be emitted as a terminal cron frame")
    return cast(CronRunTerminalStatusValue, status.value)


class WebDeliverySink(DeliverySink):
    """v0.3 cron-delivery M4：Web 投递 sink。

    通过 :class:`CronWSBroker` 把 ``cron.run.completed`` 事件 broadcast 给
    所有当前订阅 ``/ws/cron`` 的客户端。

    使用方式（web/run.py 装配）::

        from hosts.web.websocket.cron import get_broker
        from hosts.web.app_support.cron_delivery import WebDeliverySink
        from scheduler.delivery import DeliveryDispatcher

        web_sink = WebDeliverySink(get_broker())
        dispatcher = DeliveryDispatcher(web_sink=web_sink)
        # 透传给 build_scheduled_run_manager(dispatcher=dispatcher)
    """

    def __init__(self, broker: CronWSBroker) -> None:
        self._broker = broker

    async def run_started(self, task: ScheduledTask, run: ScheduledRun) -> None:
        """广播 cron run 开始事件，供前端卡片实时显示执行中。"""
        await self._broker.broadcast(
            CronRunStartedFrame(
                timestamp_ms=_now_ms(),
                task_id=task.task_id,
                task_name=task.name,
                run_id=run.run_id,
                thread_id=task.thread_id,
                session_id=run.session_id,
                scheduled_for=run.scheduled_for,
                started_at=run.started_at,
                status="running",
            ).model_dump()
        )

    async def run_finished(self, task: ScheduledTask, run: ScheduledRun) -> None:
        """广播 cron run 结束事件，供前端卡片回到空闲并刷新运行记录。"""
        await self._broker.broadcast(
            CronRunFinishedFrame(
                timestamp_ms=_now_ms(),
                task_id=task.task_id,
                task_name=task.name,
                run_id=run.run_id,
                thread_id=task.thread_id,
                session_id=run.session_id,
                scheduled_for=run.scheduled_for,
                started_at=run.started_at,
                finished_at=run.finished_at,
                status=_terminal_status_value(run.status),
                final_message=run.final_message_excerpt,
                error_message=run.error_message,
                delivery_error=run.delivery_error,
                next_run_at=task.next_run_at,
            ).model_dump()
        )

    @override
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
            CronRunCompletedFrame(
                timestamp_ms=_now_ms(),
                task_id=task.task_id,
                task_name=task.name,
                run_id=run.run_id,
                thread_id=task.thread_id,
                session_id=run.session_id,
                final_message=final_message,
                delivered_at_iso=run.finished_at,
                scheduled_for=run.scheduled_for,
                delivery_target=delivery_target,
                next_run_at=task.next_run_at,
                status=_terminal_status_value(run.status),
            ).model_dump()
        )
        return DeliveryResult.delivered()


class ThreadTargetSink:
    """v0.4: 投递到 thread — 解析 target 前缀，调 ThreadManager API。"""

    def __init__(self, thread_manager: Any) -> None:
        self._tm = thread_manager

    async def deliver_to_target(
        self,
        target: str,
        task: ScheduledTask,
        run: ScheduledRun,
        message: str,
    ) -> bool:
        """按 target 前缀路由到具体投递实现。

        当前仅支持 ``thread:<thread_id>`` 前缀；其他前缀静默返回 False。
        """
        if not target.startswith("thread:"):
            return False
        thread_id = target[len("thread:") :]
        try:
            result = await self._tm.append_cron_message(
                thread_id,
                message,
                task_id=task.task_id,
                run_id=run.run_id,
                session_id=run.session_id,
                task_name=task.name,
            )
            return bool(result)
        except Exception as exc:
            logger.warning(
                "ThreadTargetSink: deliver_to_target failed for %s: %s",
                target,
                exc,
            )
            return False


def make_cron_message_frame(
    *,
    thread_id: str,
    task_id: str,
    run_id: str,
    session_id: str,
    content: str,
    task_name: str = "",
) -> dict[str, Any]:
    """构造追加到 thread 的 cron 专用 WS 帧。"""
    message_id = f"cron-msg-{uuid.uuid4().hex[:12]}"
    frame = CronMessageAppendedFrame(
        thread_id=thread_id,
        content=content,
        message_id=message_id,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
        task_name=task_name,
        timestamp_ms=_now_ms(),
    )
    return frame.model_dump()


__all__ = ["ThreadTargetSink", "WebDeliverySink", "make_cron_message_frame"]
