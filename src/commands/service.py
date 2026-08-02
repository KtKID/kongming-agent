"""Shared command service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from commands.models import (
    CommandExecutionContext,
    CommandResult,
)
from commands.parser import parse_input
from commands.registry import CommandRegistry, build_builtin_registry
from core.result import Result

# ``attachments`` / ``references``：web 路径透传用户结构化输入 dict 列表；
# CLI 默认 None 不影响纯文本对话。
RuntimeDelegate = Callable[
    [str, str | None, list[dict[str, Any]] | None, list[dict[str, Any]] | None],
    Awaitable[Result],
]


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

    async def handle_command(
        self,
        raw_input: str,
        *,
        execution_context: CommandExecutionContext,
        references: list[dict[str, Any]] | None = None,
    ) -> Result | CommandResult:
        """处理一条 ``/`` 开头的 slash 命令(纯命令职责,不接 text)。

        职责边界(agent-manager-boot-root 重构):本方法**只处理 command**。
        普通 text 输入由 CLIInteractiveLoop 解析后直接交 HostDispatcher，不进
        command service。command service 名字与职责对齐——text 凭什么让
        command service 处理。

        命令处理三分支:

        - **未知命令** → ``CommandResult(status="failed", "Unknown command: ...")``
        - **prompt 类命令**(/hello) → 展开成 ``command.description``(或 args_text)
          的纯文本,调 ``runtime_delegate`` 触发 run。展开后的文本和用户直接打字
          走同一条 runtime 路径。
        - **其他 kind 命令**(action/interactive,当前仓库未注册) → 返回
          ``CommandResult("Unsupported")``。等未来真有 action 命令时再分支。

        Args:
            raw_input: ``/`` 开头的原始命令输入(含参数)。
            execution_context: 命令执行上下文(供 runtime_delegate 拿 reasoning_effort)。
            references: 透传给 runtime_delegate 的会话引用(prompt assembly 注入用)。

        Returns:
            prompt 命令 → :class:`Result`(run 产物);其他 → :class:`CommandResult`。
        """
        exec_ctx = execution_context
        parsed = parse_input(raw_input)

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
            # slash command 路径不带附件（命令展开成纯文本 prompt），但保留
            # conversation reference，供 prompt assembly 注入本轮显式上下文。
            return await self._runtime_delegate(
                prompt_text,
                exec_ctx.reasoning_effort,
                None,
                references,
            )

        return CommandResult(
            status="failed",
            command_name=command.slash,
            output_text=f"Unsupported command kind for {command.slash}: {command.kind}",
        )


def build_default_command_service(
    *,
    adapter: object,
    runtime_delegate: RuntimeDelegate,
) -> CommandService:
    """构造默认 CommandService。

    输入为宿主 adapter（推断 host_kind 用）和 runtime_delegate（prompt 命令展开后触发
    run 的回调）；输出为 CommandService。``runtime``/``session_id`` 历史参数已被删除——
    命令执行只依赖 delegate 回调，不需要直接持有 runtime 或 session_id。
    """
    registry = build_builtin_registry()
    host_kind = _infer_host_kind(adapter)
    return CommandService(
        registry=registry,
        runtime_delegate=runtime_delegate,
        host_kind=host_kind,
    )


def build_execution_context(
    *,
    session_id: str,
    adapter: object,
    reasoning_effort: str | None,
) -> CommandExecutionContext:
    return CommandExecutionContext(
        session_id=session_id,
        cwd=Path.cwd(),
        host_kind=_infer_host_kind(adapter),
        reasoning_effort=reasoning_effort,
    )


def _infer_host_kind(adapter: object) -> Literal["cli", "web"]:
    class_name = type(adapter).__name__.lower()
    module_name = type(adapter).__module__.lower()
    if "web" in class_name or "web" in module_name:
        return "web"
    return "cli"
