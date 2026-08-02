"""CLI 交互循环。

本模块直接持有 ``HostDispatcher`` + ``CommandService``：
投递即回、命令 task、普通排队、显式 send-now、EOF drain、Ctrl-C 打断后继续读输入。

关键流程：
1. ``run_loop`` 从 adapter 读取输入。
2. ``send`` 对命令输入创建 ``_handle_command`` 命令 task。
3. ``send`` 对普通文本创建 ``HostDispatcher.submit(QUEUE)`` task。
4. ``send_now`` 对文本先调用 ``HostDispatcher.submit(IMMEDIATE)``，未命中再排队。
5. ``aclose`` 负责收编 CLI 命令 task 和 root agent session。

关键函数：
- ``CLIInteractiveLoop.send``：按普通排队语义投递一条 CLI 输入并立即返回回执。
- ``CLIInteractiveLoop.send_now``：按显式立即发送语义投递一条 CLI 输入。
- ``CLIInteractiveLoop.run_loop``：CLI 主交互循环。
- ``CLIInteractiveLoop.aclose``：收尾命令 task 与 root agent 生命周期。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from application.agents.manager import SubmitMode
from commands.models import CommandResult
from commands.parser import parse_input
from commands.service import CommandService, build_execution_context
from core.result import Result
from hosts.shared.base import HostAdapter
from hosts.shared.host_dispatcher import HostDispatcher

logger = logging.getLogger(__name__)


class SendDelivery(StrEnum):
    """CLI 输入投递结果枚举。

    输入为空；输出为封闭的投递结果值，供调用方区分命令、send-now 和 queue 路径。
    """

    COMMAND = "command"
    SEND_NOW = "send_now"
    QUEUED = "queued"


@dataclass(frozen=True)
class SendReceipt:
    """CLI 投递回执。

    输入由 ``send`` 生成；输出给调用方判断本次投递走命令、send-now 还是 queue 路径。
    """

    delivery: SendDelivery


class CLIInteractiveLoop:
    """CLI 交互生命周期 owner。

    输入为 HostDispatcher 和 CommandService；输出为可运行 CLI REPL 的对象。
    """

    def __init__(
        self,
        *,
        host_dispatcher: HostDispatcher,
        command_service: CommandService,
        adapter: HostAdapter,
    ) -> None:
        """初始化 CLI 交互循环。

        输入为 host_dispatcher、command_service 和 adapter；输出为持有命令 task 集合的
        循环对象。本类直接持有 HostDispatcher + CommandService。
        """
        self._host_dispatcher = host_dispatcher
        self._command_service = command_service
        self._adapter = adapter
        self._command_tasks: set[asyncio.Task[Any]] = set()
        self._submit_tasks: set[asyncio.Task[Any]] = set()

    async def _handle_command(
        self,
        user_input: str,
        *,
        reasoning_effort: str | None = None,
    ) -> Result | CommandResult:
        """内联命令处理。

        输入为用户原始命令文本和可选 reasoning 覆盖；输出为 CommandService 返回的
        Result 或 CommandResult。结果按类型渲染：CommandResult 且有 output_text 时调
        ``adapter.write_output``；非 CommandResult（即 Result）交给 ``adapter.render_result``。
        """
        context = build_execution_context(
            session_id=self._host_dispatcher.session_id,
            adapter=self._adapter,
            reasoning_effort=reasoning_effort,
        )
        result = await self._command_service.handle_command(
            user_input,
            execution_context=context,
            references=None,
        )
        if isinstance(result, CommandResult):
            if result.output_text:
                await self._adapter.write_output(result.output_text)
        else:
            await self._adapter.render_result(result)
        return result

    async def send(
        self,
        user_input: str,
        *,
        reasoning_effort: str | None = None,
    ) -> SendReceipt:
        """按普通排队语义投递一条 CLI 输入并立即返回。

        输入为用户原始文本和可选 reasoning 覆盖；输出为投递回执。
        """
        parsed = parse_input(user_input)
        if parsed.kind != "text":
            self._spawn_command_task(user_input, reasoning_effort=reasoning_effort)
            return SendReceipt(delivery=SendDelivery.COMMAND)

        self._spawn_submit_task(user_input, mode=SubmitMode.QUEUE)
        return SendReceipt(delivery=SendDelivery.QUEUED)

    async def send_now(
        self,
        user_input: str,
        *,
        reasoning_effort: str | None = None,
    ) -> SendReceipt:
        """显式立即发送一条 CLI 输入。

        输入为用户原始文本和可选 reasoning 覆盖；输出为投递回执。命令仍走控制面，
        普通文本先尝试插入当前活跃 run，未命中则回落普通排队。
        """
        parsed = parse_input(user_input)
        if parsed.kind != "text":
            self._spawn_command_task(user_input, reasoning_effort=reasoning_effort)
            return SendReceipt(delivery=SendDelivery.COMMAND)

        receipt = await self._host_dispatcher.submit(user_input, mode=SubmitMode.IMMEDIATE)
        if receipt.merged:
            await self._adapter.write_output("[send-now] merged into current run")
            return SendReceipt(delivery=SendDelivery.SEND_NOW)

        self._spawn_submit_task(user_input, mode=SubmitMode.QUEUE)
        return SendReceipt(delivery=SendDelivery.QUEUED)

    def _spawn_submit_task(
        self,
        user_input: str,
        *,
        mode: SubmitMode,
    ) -> None:
        """创建普通文本提交 task。

        输入为用户文本和提交模式；输出为空，副作用是创建 task 并纳入 CLI loop 收编。
        """
        task: asyncio.Task[Any] = asyncio.create_task(
            self._host_dispatcher.submit(user_input, mode=mode),
            name=f"cli-submit-{mode.value}-{self._host_dispatcher.session_id}",
        )
        self._submit_tasks.add(task)

        def _discard_submit_task(done: asyncio.Task[Any] = task) -> None:
            """提交 task 结束回调。

            输入为已完成 task；输出为空，副作用是释放引用并记录异常。
            """
            self._submit_tasks.discard(done)
            if not done.cancelled():
                exc = done.exception()
                if exc is not None:
                    logger.error("cli submit task failed: %r", exc)

        task.add_done_callback(_discard_submit_task)

    def _spawn_command_task(
        self,
        user_input: str,
        *,
        reasoning_effort: str | None = None,
    ) -> None:
        """创建命令处理 task。

        输入为用户输入；输出为空，副作用是创建 task 并纳入 CLI loop 收编。
        """
        task: asyncio.Task[Any] = asyncio.create_task(
            self._handle_command(user_input, reasoning_effort=reasoning_effort),
            name=f"cli-command-send-{self._host_dispatcher.session_id}",
        )
        self._command_tasks.add(task)

        def _discard_command_task(done: asyncio.Task[Any] = task) -> None:
            """命令 task 结束回调。

            输入为已完成 task；输出为空，副作用是释放引用并记录异常。
            """
            self._command_tasks.discard(done)
            if not done.cancelled():
                exc = done.exception()
                if exc is not None:
                    logger.error("cli command task failed: %r", exc)

        task.add_done_callback(_discard_command_task)

    def _has_active_work(self) -> bool:
        """判断 CLI 是否有活跃工作。

        输入为空；输出 bool，用于 Ctrl-C 行为分支。
        """
        if any(not task.done() for task in self._command_tasks):
            return True
        if any(not task.done() for task in self._submit_tasks):
            return True
        return self._host_dispatcher.has_active_work()

    async def _interrupt_inflight(self) -> None:
        """打断当前 root agent 工作。

        输入为空；输出为空。实际打断由 HostDispatcher 执行。
        """
        await self._host_dispatcher.interrupt()

    async def _reset_for_reuse(self) -> None:
        """重置 CLI 与 root agent 生命周期。

        输入为空；输出为空。取消未完成命令 task 并重建 root agent 会话状态。
        """
        for task in list(self._command_tasks):
            if not task.done():
                task.cancel()
        for task in list(self._submit_tasks):
            if not task.done():
                task.cancel()
        await self._host_dispatcher.reset_for_reuse()

    async def run_loop(self) -> None:
        """运行 CLI 交互主循环。

        输入为空；输出为空。循环读取 adapter 输入，按投递即回语义转交 ``send``。
        """
        try:
            while True:
                try:
                    user_input = await self._adapter.read_input()
                except EOFError:
                    break
                except KeyboardInterrupt:
                    if self._has_active_work():
                        await self._interrupt_inflight()
                        await self._reset_for_reuse()
                        await self._adapter.write_output("[interrupted]")
                        continue
                    break
                if user_input is None:
                    break
                if not user_input.strip():
                    continue
                try:
                    await self.send(user_input)
                except (KeyboardInterrupt, EOFError):
                    await self._interrupt_inflight()
                    await self._reset_for_reuse()
                    await self._adapter.write_output("[interrupted]")
                    continue
        finally:
            await self.aclose(drain=True)
            await self._adapter.close()

    async def aclose(self, *, drain: bool = False) -> None:
        """关闭 CLI 交互循环。

        输入为 drain 开关；输出为空。drain 时等待命令 task 收尾，再关闭
        root session。
        """
        if not drain:
            await self._interrupt_inflight()
            for task in list(self._command_tasks):
                if not task.done():
                    task.cancel()
            for task in list(self._submit_tasks):
                if not task.done():
                    task.cancel()

        pending_tasks = [
            task for task in (*self._command_tasks, *self._submit_tasks) if not task.done()
        ]
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        await self._host_dispatcher.aclose(drain=drain)


__all__ = ["CLIInteractiveLoop", "SendDelivery", "SendReceipt"]
