"""REPL 级可复用组件。

这层故意保持很薄——真正的交互壳（读输入、写输出、审批确认）已经全部
落在 :mod:`host.cli_adapter`，本文件只暴露一些跨 CLI 工具可能复用的小
helper，避免每个将来新增的 CLI 子命令各自重新发明一遍。

**不要在这里实现**：

- 第二套 :class:`host.session_bridge.SessionBridge` / 第二套 run loop
- 装配逻辑（那是 :meth:`executors.agent_runtime.native_runtime.NativeRuntime.build`
  唯一职责）
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from prompt_toolkit import PromptSession as _PromptSession


class AsyncPrompt:
    """基于 :class:`prompt_toolkit.PromptSession` 的薄封装。

    v1-mini 里 :class:`host.cli_adapter.CLIAdapter` 已经直接用了
    ``PromptSession``；这个类只是给未来可能出现的其他 CLI 入口（例如
    ``kongming smoke`` 下面的轻量 prompt）一个统一调用点。
    """

    def __init__(self) -> None:
        self._session: _PromptSession[Any] | None = None

    async def prompt(self, text: str = "> ") -> str | None:
        """读一行；EOF 返回 ``None``，Ctrl-C 返回空串。"""
        session = self._ensure_session()
        try:
            result: str = await session.prompt_async(text)
            return result
        except EOFError:
            return None
        except KeyboardInterrupt:
            return ""

    def _ensure_session(self) -> _PromptSession[Any]:
        if self._session is None:
            from prompt_toolkit import PromptSession

            self._session = PromptSession()
        return self._session


def confirm_yn(prompt_text: str, *, default: bool = False) -> bool:
    """纯文本 y/N 确认。

    Args:
        prompt_text: 提示语；调用方自己负责尾随空格。
        default: 用户直接回车时返回的布尔值。

    Returns:
        用户是否确认。EOF / Ctrl-C 等同于拒绝（即返回 ``False``），
        不抛异常——审批语义上"无法确认"就等价于"不批准"。
    """
    hint = "[Y/n]" if default else "[y/N]"
    sys.stdout.write(f"{prompt_text}{hint} ")
    sys.stdout.flush()
    try:
        raw = sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        return False
    if raw == "":  # stdin closed
        return False
    answer = raw.strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def print_streaming_chunk(chunk: str) -> None:  # pragma: no cover - 流式未接入
    """流式输出占位。

    v1-mini 当前为非流式；保留这个入口避免未来流式接入时满 CLI 搜
    ``sys.stdout.write``。真正接入时再把 ``chunk`` 写到 stdout 并 flush。
    """
    # 故意为空：首版非流式。
    del chunk


__all__ = ["AsyncPrompt", "confirm_yn", "print_streaming_chunk"]
