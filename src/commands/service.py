"""Shared command service."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from commands.models import (
    CommandExecutionContext,
    CommandInvocation,
    CommandResult,
)
from commands.parser import parse_input
from commands.registry import CommandRegistry, build_builtin_registry
from commands.review_action import run_review_action
from core.result import Result
from executors.agent_runtime.native_runtime import NativeRuntime

if TYPE_CHECKING:
    from host.base import HostAdapter

RuntimeDelegate = Callable[[str, str | None], Awaitable[Result]]
ActionExecutor = Callable[[CommandInvocation, CommandExecutionContext], Awaitable[CommandResult]]


class CommandService:
    def __init__(
        self,
        *,
        registry: CommandRegistry,
        runtime_delegate: RuntimeDelegate,
        action_executor: ActionExecutor,
        host_kind: str,
    ) -> None:
        self._registry = registry
        self._runtime_delegate = runtime_delegate
        self._action_executor = action_executor
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
        if command.kind != "action":
            return CommandResult(
                status="failed",
                command_name=command.slash,
                output_text=f"Unsupported command kind for {command.slash}: {command.kind}",
            )

        invocation = CommandInvocation(
            invocation_id=f"cmd-{uuid.uuid4().hex[:12]}",
            session_id=context.session_id,
            command=command,
            raw_input=raw_input,
            args_text=parsed.args_text,
            cwd=context.cwd,
        )
        return await self._action_executor(invocation, context)


def build_default_command_service(
    *,
    runtime: NativeRuntime,
    adapter: HostAdapter,
    session_id: str,
    runtime_delegate: RuntimeDelegate,
) -> CommandService:
    registry = build_builtin_registry()
    host_kind = _infer_host_kind(adapter)

    async def action_executor(
        invocation: CommandInvocation,
        context: CommandExecutionContext,
    ) -> CommandResult:
        if invocation.command.executor_key == "review_action":
            return await run_review_action(
                runtime=runtime,
                invocation=invocation,
                context=context,
            )
        return CommandResult(
            status="failed",
            command_name=invocation.command.slash,
            output_text=f"No executor registered for {invocation.command.slash}",
            invocation_id=invocation.invocation_id,
        )

    del session_id
    return CommandService(
        registry=registry,
        runtime_delegate=runtime_delegate,
        action_executor=action_executor,
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
