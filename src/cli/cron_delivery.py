"""cli — cron 投递 sink (v0.3 M5)。

CLI 投递语义：cron 触发完成后**不打断**用户当前 prompt 输入；消息排队
到 buffer，REPL 主循环在下一次 prompt 之前调 :meth:`drain_pending`
一次性 flush 到 stdout。

设计决策（plan.md M5 锁定）：

- buffer 用 ``collections.deque(maxlen=100)``：超过 100 行自动丢最旧（FIFO）
- ``asyncio.Lock`` 保护并发：ticker 协程写 / REPL 协程读
- 每条投递格式：``\\n📌 [cron:<task.name>] <final_message>\\n``
- ``drain_pending`` 返回拼接的字符串；空 buffer 返回空串

边界：

- 不直接打印（所有 stdout 操作在 REPL 主循环里做，便于 prompt_toolkit
  集成）；本 sink 只负责把消息塞进 buffer
- ``deliver`` 始终返回 ``DeliveryResult.delivered()``；buffer 满丢最旧
  不视为投递失败（消息已"接收"，只是显示队列管理）
"""

from __future__ import annotations

import asyncio
from collections import deque

from scheduler.delivery import DeliveryResult, DeliverySink
from scheduler.domain import ScheduledRun, ScheduledTask


class CliDeliverySink(DeliverySink):
    """v0.3 cron-delivery M5：CLI 投递 sink。

    使用方式（cli/main.py REPL 主循环）::

        cli_sink = CliDeliverySink()
        # 装配进 DeliveryDispatcher → build_cron_execution_bridge
        ...
        while True:
            pending = await cli_sink.drain_pending()
            if pending:
                print(pending, end="", flush=True)
            user_input = await session.prompt_async(">> ")
            ...
    """

    MAX_BUFFER_LINES: int = 100
    """buffer 上限；超过自动丢最旧（FIFO 队列语义）。"""

    def __init__(self) -> None:
        self._buffer: deque[str] = deque(maxlen=self.MAX_BUFFER_LINES)
        self._lock = asyncio.Lock()

    async def deliver(
        self,
        task: ScheduledTask,
        run: ScheduledRun,
        final_message: str,
    ) -> DeliveryResult:
        """把 final_message 排进 buffer；返回 DELIVERED。

        即便 buffer 已满（即将丢最旧）也返回 DELIVERED —— 消息已被本 sink
        "接收"，只是显示队列管理；溢出不算投递失败。

        但溢出时记 ``logger.warning`` 给运维 / 调试留线索（R2 round 1 P1-3
        修复）：长时间不敲 prompt 会积累大量 cron 通知，buffer 溢出会丢最旧
        的消息，用户期待"50 分提醒"等内容可能被覆盖丢掉。
        """
        line = f"\n📌 [cron:{task.name}] {final_message}\n"
        async with self._lock:
            will_overflow = len(self._buffer) >= self._buffer.maxlen
            self._buffer.append(line)
        if will_overflow:
            import logging

            logging.getLogger(__name__).warning(
                "CliDeliverySink buffer at capacity (%d); oldest cron message "
                "dropped while appending task=%s run=%s",
                self._buffer.maxlen,
                task.task_id,
                run.run_id,
            )
        return DeliveryResult.delivered()

    async def drain_pending(self) -> str:
        """一次性返回 buffer 全部内容并清空；空 buffer 返回 ``""``。"""
        async with self._lock:
            if not self._buffer:
                return ""
            out = "".join(self._buffer)
            self._buffer.clear()
            return out


__all__ = ["CliDeliverySink"]
