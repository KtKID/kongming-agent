"""unit：CLIInteractiveLoop 命令分流覆盖（host-dispatch-consolidation 后）。

本文件覆盖 ``CLIInteractiveLoop`` 的 command 分流。命令路由能力位于
``CLIInteractiveLoop._handle_command`` + ``CommandService.handle_command``：

- unknown command → adapter 收到错误文本
- reasoning_effort 在 command 路径透传到 context
- command result 不触发 usage/error 写入
- command 返回 Result（非 CommandResult）→ render_result 路径
- CommandResult.output_text 为空时不写 adapter

关键函数：
- ``_StubAdapter``：HostAdapter 桩，记录 write_output / render_result 调用。
- ``_StubHostDispatcher``：HostDispatcher 桩，只暴露 session_id（_handle_command 需要）。
- ``_CapturingCommandService``：CommandService 桩，捕获 handle_command 调用并返回预设结果。
- ``test_*``：5 个命令分流边界用例。
"""

from __future__ import annotations

from typing import Any

import pytest

from application.agents.manager import SubmitMode
from commands.models import CommandExecutionContext, CommandResult
from core.message import Message
from core.result import Result
from hosts.cli.interactive_loop import CLIInteractiveLoop, SendDelivery
from hosts.shared.base import HostAdapter
from hosts.shared.host_dispatcher import SubmitReceipt


class _StubAdapter(HostAdapter):
    """HostAdapter 桩，记录输出与关闭次数。

    输入为空；输出为空，副作用是记录 write_output / render_result / close 调用。
    """

    def __init__(self) -> None:
        self.outputs: list[str] = []
        self.closed: int = 0
        self.rendered: list[Result] = []

    async def read_input(self) -> str | None:
        return None

    async def write_output(self, text: str) -> None:
        self.outputs.append(text)

    async def render_result(self, result: Result) -> None:
        self.rendered.append(result)

    async def close(self) -> None:
        self.closed += 1


class _StubHostDispatcher:
    """HostDispatcher 桩，记录 submit 调用。

    输入为 session_id；输出为可被 CLIInteractiveLoop 读取 session_id 的桩对象。
    """

    def __init__(self, session_id: str = "sid-1", *, immediate_merged: bool = False) -> None:
        self.session_id = session_id
        self.immediate_merged = immediate_merged
        self.submit_calls: list[tuple[str, SubmitMode]] = []
        self.interrupted = False
        self.closed = False

    async def submit(self, text: str, *, mode: SubmitMode, **_: Any) -> SubmitReceipt:
        self.submit_calls.append((text, mode))
        return SubmitReceipt(merged=mode is SubmitMode.IMMEDIATE and self.immediate_merged)

    async def interrupt(self) -> None:
        self.interrupted = True

    async def reset_for_reuse(self) -> None:
        return None

    async def aclose(self, *, drain: bool = False) -> None:
        del drain
        self.closed = True

    def has_active_work(self) -> bool:
        return False


class _CapturingCommandService:
    """CommandService 桩，捕获 handle_command 调用并返回预设结果。

    输入为预设的 Result 或 CommandResult；输出为空，副作用是记录每次 handle_command
    的 (raw_input, context) 调用。本桩只服务命令分流测试，不实现真实命令注册表。
    """

    def __init__(self, result: Result | CommandResult) -> None:
        self._result = result
        self.calls: list[tuple[str, CommandExecutionContext]] = []

    async def handle_command(
        self,
        raw_input: str,
        *,
        execution_context: CommandExecutionContext,
        references: list[dict[str, Any]] | None = None,
    ) -> Result | CommandResult:
        self.calls.append((raw_input, execution_context))
        return self._result


def _build_loop(
    *,
    adapter: _StubAdapter,
    command_result: Result | CommandResult,
) -> tuple[CLIInteractiveLoop, _CapturingCommandService]:
    """装配 CLIInteractiveLoop + 捕获 CommandService。

    输入为 adapter 和预设 command result；输出为 (loop, command_service)，便于
    断言调用链路。
    """
    dispatcher = _StubHostDispatcher()
    svc = _CapturingCommandService(command_result)
    loop = CLIInteractiveLoop(
        host_dispatcher=dispatcher,  # type: ignore[arg-type]
        command_service=svc,  # type: ignore[arg-type]
        adapter=adapter,
    )
    return loop, svc


@pytest.mark.asyncio
async def test_command_unknown_writes_error_text() -> None:
    """unknown command 的 failed CommandResult 文本正确写到 adapter。"""
    ad = _StubAdapter()
    loop, svc = _build_loop(
        adapter=ad,
        command_result=CommandResult(
            status="failed",
            command_name="/deploy",
            output_text="Unknown command: /deploy",
        ),
    )
    result = await loop._handle_command("/deploy")
    assert isinstance(result, CommandResult)
    assert result.status == "failed"
    assert ad.outputs == ["Unknown command: /deploy"]
    assert len(svc.calls) == 1


@pytest.mark.asyncio
async def test_command_result_does_not_write_usage() -> None:
    """CommandResult 不会触发 usage token 行写入（render_result 不被调）。"""
    ad = _StubAdapter()
    loop, _ = _build_loop(
        adapter=ad,
        command_result=CommandResult(
            status="completed",
            command_name="/review",
            output_text="/review completed.",
            invocation_id="cmd-1",
            metadata={"artifact_path": "/tmp/foo.json"},
        ),
    )
    await loop._handle_command("/review auth")
    assert ad.outputs == ["/review completed."]
    # CommandResult 不触发 render_result（rendered 保持空，不会写 token 行）。
    assert ad.rendered == []
    assert not any("[tokens" in o for o in ad.outputs)


@pytest.mark.asyncio
async def test_reasoning_effort_reaches_command_context() -> None:
    """reasoning_effort 通过 _handle_command 到达 command context。"""
    ad = _StubAdapter()
    loop, svc = _build_loop(
        adapter=ad,
        command_result=CommandResult(
            status="completed",
            command_name="/review",
            output_text="done",
        ),
    )
    await loop._handle_command("/review", reasoning_effort="high")
    assert len(svc.calls) == 1
    _, ctx = svc.calls[0]
    assert ctx.reasoning_effort == "high"
    assert ctx.session_id == "sid-1"


@pytest.mark.asyncio
async def test_command_with_result_type_triggers_render_result() -> None:
    """command_service 返回 Result（非 CommandResult）时触发 adapter.render_result。

    覆盖 prompt 类命令展开成 run 后回传 Result 的链路：_handle_command 走 else 分支
    调 render_result，不走 write_output。
    """
    ad = _StubAdapter()
    prompt_result = Result(
        run_id="r-1",
        session_id="sid-1",
        status="completed",
        turn_count=1,
        final_message=Message.assistant(content="hi"),
    )
    loop, _ = _build_loop(adapter=ad, command_result=prompt_result)
    result = await loop._handle_command("/hello")
    assert isinstance(result, Result)
    assert ad.rendered == [prompt_result]
    # Result 路径不写 output_text。
    assert ad.outputs == []


@pytest.mark.asyncio
async def test_empty_command_output_writes_nothing() -> None:
    """CommandResult.output_text 为空时不写 adapter。"""
    ad = _StubAdapter()
    loop, _ = _build_loop(
        adapter=ad,
        command_result=CommandResult(
            status="completed",
            command_name="/noop",
            output_text="",
        ),
    )
    await loop._handle_command("/noop")
    assert ad.outputs == []
    assert ad.rendered == []


@pytest.mark.asyncio
async def test_send_text_uses_submit_queue() -> None:
    """普通文本 send 只通过 HostDispatcher.submit(QUEUE) 投递。"""
    ad = _StubAdapter()
    dispatcher = _StubHostDispatcher()
    loop = CLIInteractiveLoop(
        host_dispatcher=dispatcher,  # type: ignore[arg-type]
        command_service=_CapturingCommandService(
            CommandResult(status="completed", command_name="/noop", output_text="")
        ),
        adapter=ad,
    )

    receipt = await loop.send("hello")
    await loop.aclose(drain=True)

    assert receipt.delivery is SendDelivery.QUEUED
    assert dispatcher.submit_calls == [("hello", SubmitMode.QUEUE)]


@pytest.mark.asyncio
async def test_send_now_uses_submit_immediate_and_queue_fallback() -> None:
    """send_now 先走 submit(IMMEDIATE)，未合并时显式回落 submit(QUEUE)。"""
    ad = _StubAdapter()
    dispatcher = _StubHostDispatcher(immediate_merged=False)
    loop = CLIInteractiveLoop(
        host_dispatcher=dispatcher,  # type: ignore[arg-type]
        command_service=_CapturingCommandService(
            CommandResult(status="completed", command_name="/noop", output_text="")
        ),
        adapter=ad,
    )

    receipt = await loop.send_now("late")
    await loop.aclose(drain=True)

    assert receipt.delivery is SendDelivery.QUEUED
    assert dispatcher.submit_calls == [
        ("late", SubmitMode.IMMEDIATE),
        ("late", SubmitMode.QUEUE),
    ]


@pytest.mark.asyncio
async def test_send_now_merged_does_not_queue_fallback() -> None:
    """send_now 合并成功时不发起 QUEUE fallback。"""
    ad = _StubAdapter()
    dispatcher = _StubHostDispatcher(immediate_merged=True)
    loop = CLIInteractiveLoop(
        host_dispatcher=dispatcher,  # type: ignore[arg-type]
        command_service=_CapturingCommandService(
            CommandResult(status="completed", command_name="/noop", output_text="")
        ),
        adapter=ad,
    )

    receipt = await loop.send_now("now")
    await loop.aclose(drain=True)

    assert receipt.delivery is SendDelivery.SEND_NOW
    assert ad.outputs == ["[send-now] merged into current run"]
    assert dispatcher.submit_calls == [("now", SubmitMode.IMMEDIATE)]
