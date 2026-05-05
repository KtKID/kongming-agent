"""unit：commands.service 覆盖。

测试 CommandService 三路分流：text → runtime / command → executor / unknown → error。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from commands.models import (
    CommandDefinition,
    CommandExecutionContext,
    CommandResult,
)
from commands.service import CommandService
from core.message import Message
from core.result import Result


def _review_def() -> CommandDefinition:
    return CommandDefinition(
        id="builtin.review",
        slash="/review",
        title="Run Review",
        description="d",
        kind="action",
        host_visibility="both",
        accepts_args=True,
        executor_key="review_action",
    )


def _context() -> CommandExecutionContext:
    return CommandExecutionContext(
        session_id="sid-1",
        cwd=Path("/tmp"),
        host_kind="cli",
        reasoning_effort=None,
    )


def _ok_result() -> Result:
    return Result(
        run_id="r-1",
        session_id="sid-1",
        status="completed",
        final_message=Message.assistant(content="hello"),
        turn_count=1,
    )


def _ok_command_result() -> CommandResult:
    return CommandResult(
        status="completed",
        command_name="/review",
        output_text="/review completed.",
        invocation_id="cmd-abc",
    )


class TestCommandServiceRouting:
    @pytest.mark.asyncio
    async def test_text_delegates_to_runtime(self) -> None:
        calls: list[str] = []

        async def runtime_delegate(text: str, effort: str | None = None) -> Result:
            calls.append(text)
            return _ok_result()

        async def action_executor(inv, ctx):  # type: ignore[no-untyped-def]
            pytest.fail("action_executor should not be called for text")

        from commands.registry import CommandRegistry

        svc = CommandService(
            registry=CommandRegistry([_review_def()]),
            runtime_delegate=runtime_delegate,
            action_executor=action_executor,
            host_kind="cli",
        )
        result = await svc.handle_input("hello world", context=_context())
        assert isinstance(result, Result)
        assert calls == ["hello world"]

    @pytest.mark.asyncio
    async def test_command_routes_to_executor(self) -> None:
        executor_calls: list[str] = []

        async def runtime_delegate(text: str, effort: str | None = None) -> Result:
            pytest.fail("runtime should not be called for commands")

        async def action_executor(inv, ctx):  # type: ignore[no-untyped-def]
            executor_calls.append(inv.command.slash)
            return _ok_command_result()

        from commands.registry import CommandRegistry

        svc = CommandService(
            registry=CommandRegistry([_review_def()]),
            runtime_delegate=runtime_delegate,
            action_executor=action_executor,
            host_kind="cli",
        )
        result = await svc.handle_input("/review auth module", context=_context())
        assert isinstance(result, CommandResult)
        assert result.status == "completed"
        assert executor_calls == ["/review"]

    @pytest.mark.asyncio
    async def test_unknown_command_returns_failed(self) -> None:
        async def runtime_delegate(text: str, effort: str | None = None) -> Result:
            pytest.fail("runtime should not be called for unknown commands")

        async def action_executor(inv, ctx):  # type: ignore[no-untyped-def]
            pytest.fail("executor should not be called for unknown commands")

        from commands.registry import CommandRegistry

        svc = CommandService(
            registry=CommandRegistry([_review_def()]),
            runtime_delegate=runtime_delegate,
            action_executor=action_executor,
            host_kind="cli",
        )
        result = await svc.handle_input("/deploy", context=_context())
        assert isinstance(result, CommandResult)
        assert result.status == "failed"
        assert "/deploy" in result.output_text

    @pytest.mark.asyncio
    async def test_path_input_treated_as_text(self) -> None:
        calls: list[str] = []

        async def runtime_delegate(text: str, effort: str | None = None) -> Result:
            calls.append(text)
            return _ok_result()

        async def action_executor(inv, ctx):  # type: ignore[no-untyped-def]
            pytest.fail("paths should not be treated as commands")

        from commands.registry import CommandRegistry

        svc = CommandService(
            registry=CommandRegistry([_review_def()]),
            runtime_delegate=runtime_delegate,
            action_executor=action_executor,
            host_kind="cli",
        )
        result = await svc.handle_input("/tmp/project/foo.py", context=_context())
        assert isinstance(result, Result)
        assert calls == ["/tmp/project/foo.py"]

    @pytest.mark.asyncio
    async def test_reasoning_effort_passes_through_to_delegate(self) -> None:
        received_effort: list[str | None] = []

        async def runtime_delegate(text: str, effort: str | None = None) -> Result:
            received_effort.append(effort)
            return _ok_result()

        async def action_executor(inv, ctx):  # type: ignore[no-untyped-def]
            pytest.fail("should not be called")

        from commands.registry import CommandRegistry

        ctx = CommandExecutionContext(
            session_id="sid-1",
            cwd=Path("/tmp"),
            host_kind="cli",
            reasoning_effort="high",
        )
        svc = CommandService(
            registry=CommandRegistry([_review_def()]),
            runtime_delegate=runtime_delegate,
            action_executor=action_executor,
            host_kind="cli",
        )
        await svc.handle_input("hi", context=ctx)
        assert received_effort == ["high"]

    @pytest.mark.asyncio
    async def test_host_visibility_filters_commands(self) -> None:
        cli_only = CommandDefinition(
            id="builtin.cli-only",
            slash="/clionly",
            title="CLI Only",
            description="d",
            kind="action",
            host_visibility="cli",
            accepts_args=False,
            executor_key="noop",
        )

        async def runtime_delegate(text: str, effort: str | None = None) -> Result:
            pytest.fail("should not reach runtime")

        async def action_executor(inv, ctx):  # type: ignore[no-untyped-def]
            pytest.fail("should not reach executor")

        from commands.registry import CommandRegistry

        svc = CommandService(
            registry=CommandRegistry([cli_only]),
            runtime_delegate=runtime_delegate,
            action_executor=action_executor,
            host_kind="web",
        )
        result = await svc.handle_input("/clionly", context=_context())
        assert isinstance(result, CommandResult)
        assert result.status == "failed"
        assert "Unknown command" in result.output_text
