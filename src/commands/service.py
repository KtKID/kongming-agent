"""Shared command service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from commands.models import (
    CommandExecutionContext,
    CommandResult,
)
from commands.parser import parse_input
from commands.registry import CommandRegistry, build_builtin_registry
from core.result import Result

if TYPE_CHECKING:
    from executors.agent_runtime.native_runtime import NativeRuntime
    from host.base import HostAdapter

RuntimeDelegate = Callable[[str, str | None], Awaitable[Result]]


class CommandService:
    def __init__(
        self,
        *,
        registry: CommandRegistry,
        runtime_delegate: RuntimeDelegate,
        host_kind: str,
    ) -> None:
        self._registry = registry
        self._runtime_delegate = runtime_delegate
        self._host_kind = host_kind

    async def handle_input(
        self,
        raw_input: str,
        *,
        context: CommandExecutionContext,
    ) -> Result | CommandResult:
        parsed = parse_input(raw_input)
        if parsed.kind == "text":
            return await self._runtime_delegate(raw_input, context.reasoning_effort)

        command_name = parsed.command_name or ""
        command = self._registry.lookup(command_name, self._host_kind)
        if command is None:
            return CommandResult(
                status="failed",
                command_name=f"/{command_name}",
                output_text=f"Unknown command: /{command_name}",
            )

        if command.kind == "prompt":
            prompt_text = command.description
            if parsed.args_text:
                prompt_text = parsed.args_text
            return await self._runtime_delegate(prompt_text, context.reasoning_effort)

        return CommandResult(
            status="failed",
            command_name=command.slash,
            output_text=f"Unsupported command kind for {command.slash}: {command.kind}",
        )


def build_default_command_service(
    *,
    runtime: NativeRuntime,
    adapter: HostAdapter,
    session_id: str,
    runtime_delegate: RuntimeDelegate,
) -> CommandService:
    registry = build_builtin_registry()
    host_kind = _infer_host_kind(adapter)
    del runtime, session_id
    return CommandService(
        registry=registry,
        runtime_delegate=runtime_delegate,
        host_kind=host_kind,
    )


def build_execution_context(
    *,
    session_id: str,
    adapter: HostAdapter,
    reasoning_effort: str | None,
) -> CommandExecutionContext:
    return CommandExecutionContext(
        session_id=session_id,
        cwd=Path.cwd(),
        host_kind=_infer_host_kind(adapter),
        reasoning_effort=reasoning_effort,
    )


def _infer_host_kind(adapter: HostAdapter) -> Literal["cli", "web"]:
    class_name = type(adapter).__name__.lower()
    module_name = type(adapter).__module__.lower()
    if "web" in class_name or "web" in module_name:
        return "web"
    return "cli"
