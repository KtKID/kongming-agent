"""Builtin tool 内部复用的 concrete base / helper。

这份模块**不是** ``Tool`` Protocol 真源。``Tool`` 协议定义在 :mod:`core.contracts`，
本文件只提供一个内部便利基类 :class:`BaseBuiltinTool`，让 ``tools/`` 目录下第一批
builtin tool（``file_tools.py`` / ``shell_tool.py`` 等）少写一点样板：

- 统一参数校验（第一版只做字段存在性检查，不引 jsonschema 重依赖）
- 统一异常包装（把 tool 实现里的普通异常转成 ``ToolResult(ok=False, error_message=...)``
  这种结构化失败，避免 runner 侧裸异常中断主链路）
- 强制让子类只需要实现 :meth:`_run`，不必关心 ``ToolResult`` 构造细节

**不做**的事：

- 不做路径白名单 / 命令黑名单（安全策略归 ``safety/``，tools 层不做）
- 不做审批决策（审批由 :class:`core.contracts.ApprovalProvider` 和装配层串联）
- 不依赖 :mod:`safety.capability_policy` / :mod:`safety.permission_policy`
  （硬约束，import-linter 会红）
"""

from __future__ import annotations

from typing import Any

from core.contracts import ToolContext, ToolResult


class BaseBuiltinTool:
    """builtin tool 的便利基类。

    子类必须覆盖 :attr:`name` / :attr:`description` / :attr:`input_schema` 三个类属性，
    并实现 :meth:`_run`。

    :meth:`execute` 的流程固定为：

    1. 调用 :meth:`_validate_args` 校验参数（当前仅 required 字段存在性）。
    2. 调用 :meth:`_run` 执行真正的工具逻辑，拿到自由形态的输出。
    3. 把输出序列化成 ``ToolResult``，统一 ``ok`` / ``content`` / ``data`` 字段。
    4. 任意异常被捕获并转成 ``ToolResult(ok=False, error_message=str(exc))``。

    这保证所有 builtin tool 对外呈现的行为一致：runner 拿到的永远是结构化
    ``ToolResult``，而不是时好时坏的裸异常。
    """

    # 子类必须覆盖
    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {}

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """统一入口。子类通常**不需要**覆盖这个方法。

        Args:
            args: 模型传入的工具调用参数。
            ctx: runner 注入的执行上下文。
        """
        try:
            validated = self._validate_args(args)
        except Exception as exc:
            return ToolResult(
                ok=False,
                content="",
                error_message=f"argument validation failed: {exc}",
            )

        try:
            content, data = await self._run(validated, ctx)
        except Exception as exc:
            return ToolResult(
                ok=False,
                content="",
                error_message=str(exc),
            )

        return ToolResult(ok=True, content=content, data=data)

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """子类实现真正的工具逻辑。

        Returns:
            ``(content, data)`` 二元组：

            - ``content`` 是给模型看的人类可读文本，必填。
            - ``data`` 是给 infrastructure.tracing / 装配层看的结构化字段，可选。

        子类抛出的任何异常都会被 :meth:`execute` 捕获并转成
        ``ToolResult(ok=False, error_message=str(exc))``。
        """
        raise NotImplementedError

    def _validate_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """第一版参数校验：只查 required 字段是否存在。

        后续若需要类型校验，可以在同一个钩子上升级到 jsonschema，不需要改子类。
        """
        if not isinstance(args, dict):
            raise TypeError(f"tool args must be dict, got {type(args).__name__}")

        required = list(self.input_schema.get("required", []))
        missing = [k for k in required if k not in args]
        if missing:
            raise ValueError(f"missing required args: {missing}")
        return args


__all__ = ["BaseBuiltinTool"]
