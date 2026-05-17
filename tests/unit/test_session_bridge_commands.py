"""unit：SessionBridge command 分流覆盖。

补充 test_session_bridge.py 中 command 路径的专项测试：
- unknown command → adapter 收到错误文本
- reasoning_effort 在 command 路径透传到 context
- command result 不触发 usage/error 写入
"""

from __future__ import annotations

from typing import Any

import pytest

from commands.models import CommandExecutionContext, CommandResult
from core.message import Message
from core.result import Result
from host.base import HostAdapter
from host.session_bridge import SessionBridge


class _StubAdapter(HostAdapter):
    def __init__(self) -> None:
        self.outputs: list[str] = []
        self.closed: int = 0

    async def read_input(self) -> str | None:
        return None

    async def write_output(self, text: str) -> None:
        self.outputs.append(text)

    async def close(self) -> None:
        self.closed += 1


class _StubRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def run(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        reasoning_effort: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> Result:
        self.calls.append((user_input, reasoning_effort))
        return Result(
            run_id="r-1",
            session_id=session_id or "",
            status="completed",
            final_message=Message.assistant(content="hi"),
            turn_count=1,
        )


class _CapturingCommandService:
    """捕获 handle_input 调用参数并返回可配置结果。"""

    def __init__(self, result: Result | CommandResult) -> None:
        self._result = result
        self.calls: list[tuple[str, CommandExecutionContext]] = []

    async def handle_input(
        self,
        raw_input: str,
        *,
        context: Any,
        attachments: list[dict[str, Any]] | None = None,
    ) -> Result | CommandResult:
        self.calls.append((raw_input, context))
        return self._result


@pytest.mark.asyncio
async def test_command_unknown_writes_error_text() -> None:
    """unknown command 的 failed CommandResult 文本正确写到 adapter。"""
    rt = _StubRuntime()
    ad = _StubAdapter()
    svc = _CapturingCommandService(
        CommandResult(
            status="failed",
            command_name="/deploy",
            output_text="Unknown command: /deploy",
        )
    )
    b = SessionBridge(
        runtime=rt,  # type: ignore[arg-type]
        adapter=ad,
        session_id="sid-1",
        command_service=svc,  # type: ignore[arg-type]
    )
    result = await b.run_once("/deploy")
    assert isinstance(result, CommandResult)
    assert result.status == "failed"
    assert ad.outputs == ["Unknown command: /deploy"]
    assert rt.calls == []


@pytest.mark.asyncio
async def test_command_result_does_not_write_usage() -> None:
    """CommandResult 不会触发 usage token 行写入。"""
    rt = _StubRuntime()
    ad = _StubAdapter()
    svc = _CapturingCommandService(
        CommandResult(
            status="completed",
            command_name="/review",
            output_text="/review completed.",
            invocation_id="cmd-1",
            metadata={"artifact_path": "/tmp/foo.json"},
        )
    )
    b = SessionBridge(
        runtime=rt,  # type: ignore[arg-type]
        adapter=ad,
        session_id="sid-1",
        command_service=svc,  # type: ignore[arg-type]
    )
    await b.run_once("/review auth")
    assert ad.outputs == ["/review completed."]
    assert not any("[tokens" in o for o in ad.outputs)


@pytest.mark.asyncio
async def test_reasoning_effort_reaches_command_context() -> None:
    """reasoning_effort 通过 handle_input 到达 command context。"""
    rt = _StubRuntime()
    ad = _StubAdapter()
    svc = _CapturingCommandService(
        CommandResult(
            status="completed",
            command_name="/review",
            output_text="done",
        )
    )
    b = SessionBridge(
        runtime=rt,  # type: ignore[arg-type]
        adapter=ad,
        session_id="sid-1",
        command_service=svc,  # type: ignore[arg-type]
    )
    await b.run_once("/review", reasoning_effort="high")
    assert len(svc.calls) == 1
    _, ctx = svc.calls[0]
    assert ctx.reasoning_effort == "high"
    assert ctx.session_id == "sid-1"


@pytest.mark.asyncio
async def test_text_still_routes_to_runtime_via_service() -> None:
    """普通文本通过 CommandService 路由到 runtime delegate。"""
    rt = _StubRuntime()
    ad = _StubAdapter()
    result_from_runtime = Result(
        run_id="r-1",
        session_id="sid-1",
        status="completed",
        final_message=Message.assistant(content="response"),
        turn_count=1,
    )
    svc = _CapturingCommandService(result_from_runtime)
    b = SessionBridge(
        runtime=rt,  # type: ignore[arg-type]
        adapter=ad,
        session_id="sid-1",
        command_service=svc,  # type: ignore[arg-type]
    )
    result = await b.run_once("hello world")
    assert isinstance(result, Result)
    assert "response" in ad.outputs


@pytest.mark.asyncio
async def test_empty_command_output_writes_nothing() -> None:
    """CommandResult.output_text 为空时不写 adapter。"""
    rt = _StubRuntime()
    ad = _StubAdapter()
    svc = _CapturingCommandService(
        CommandResult(
            status="completed",
            command_name="/noop",
            output_text="",
        )
    )
    b = SessionBridge(
        runtime=rt,  # type: ignore[arg-type]
        adapter=ad,
        session_id="sid-1",
        command_service=svc,  # type: ignore[arg-type]
    )
    await b.run_once("/noop")
    assert ad.outputs == []
