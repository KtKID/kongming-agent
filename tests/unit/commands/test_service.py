"""unit：commands.service 覆盖。

测试 CommandService 的 slash command 路径：prompt command → runtime / unknown → error。
普通文本分流归 CLIInteractiveLoop / HostDispatcher 覆盖，CommandService 只处理命令输入。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
        title="Review",
        description="Review current workspace changes.",
        kind="prompt",
        host_visibility="both",
        accepts_args=True,
        executor_key="prompt",
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


class TestCommandServiceRouting:
    @pytest.mark.asyncio
    async def test_prompt_command_delegates_args_to_runtime(self) -> None:
        """prompt 命令带参数时，args_text 发给主 agent。"""
        calls: list[str] = []

        async def runtime_delegate(
            text: str,
            effort: str | None = None,
            attachments: list[dict[str, Any]] | None = None,
            references: list[dict[str, Any]] | None = None,
        ) -> Result:
            calls.append(text)
            return _ok_result()

        from commands.registry import CommandRegistry

        svc = CommandService(
            registry=CommandRegistry([_review_def()]),
            runtime_delegate=runtime_delegate,
            host_kind="cli",
        )
        result = await svc.handle_command("/review auth module", execution_context=_context())
        assert isinstance(result, Result)
        assert calls == ["auth module"]

    @pytest.mark.asyncio
    async def test_prompt_command_no_args_uses_description(self) -> None:
        """prompt 命令无参数时，发 description 给主 agent。"""
        calls: list[str] = []

        async def runtime_delegate(
            text: str,
            effort: str | None = None,
            attachments: list[dict[str, Any]] | None = None,
            references: list[dict[str, Any]] | None = None,
        ) -> Result:
            calls.append(text)
            return _ok_result()

        from commands.registry import CommandRegistry

        svc = CommandService(
            registry=CommandRegistry([_review_def()]),
            runtime_delegate=runtime_delegate,
            host_kind="cli",
        )
        result = await svc.handle_command("/review", execution_context=_context())
        assert isinstance(result, Result)
        assert calls == ["Review current workspace changes."]

    @pytest.mark.asyncio
    async def test_unknown_command_returns_failed(self) -> None:
        async def runtime_delegate(
            text: str,
            effort: str | None = None,
            attachments: list[dict[str, Any]] | None = None,
            references: list[dict[str, Any]] | None = None,
        ) -> Result:
            pytest.fail("runtime should not be called for unknown commands")

        from commands.registry import CommandRegistry

        svc = CommandService(
            registry=CommandRegistry([_review_def()]),
            runtime_delegate=runtime_delegate,
            host_kind="cli",
        )
        result = await svc.handle_command("/deploy", execution_context=_context())
        assert isinstance(result, CommandResult)
        assert result.status == "failed"
        assert "/deploy" in result.output_text

    @pytest.mark.asyncio
    async def test_reasoning_effort_passes_through(self) -> None:
        received_effort: list[str | None] = []

        async def runtime_delegate(
            text: str,
            effort: str | None = None,
            attachments: list[dict[str, Any]] | None = None,
            references: list[dict[str, Any]] | None = None,
        ) -> Result:
            received_effort.append(effort)
            return _ok_result()

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
            host_kind="cli",
        )
        await svc.handle_command("/review check auth", execution_context=ctx)
        assert received_effort == ["high"]

    @pytest.mark.asyncio
    async def test_host_visibility_filters_commands(self) -> None:
        cli_only = CommandDefinition(
            id="builtin.cli-only",
            slash="/clionly",
            title="CLI Only",
            description="d",
            kind="prompt",
            host_visibility="cli",
            accepts_args=False,
            executor_key="prompt",
        )

        async def runtime_delegate(
            text: str,
            effort: str | None = None,
            attachments: list[dict[str, Any]] | None = None,
            references: list[dict[str, Any]] | None = None,
        ) -> Result:
            pytest.fail("should not reach runtime")

        from commands.registry import CommandRegistry

        svc = CommandService(
            registry=CommandRegistry([cli_only]),
            runtime_delegate=runtime_delegate,
            host_kind="web",
        )
        result = await svc.handle_command("/clionly", execution_context=_context())
        assert isinstance(result, CommandResult)
        assert result.status == "failed"
        assert "Unknown command" in result.output_text
