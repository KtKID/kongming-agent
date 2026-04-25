"""流式 CLI 渲染器：把 ``content.delta`` / ``reasoning.delta`` 实时打到 stdout。

无状态机纯渲染：每个 :class:`Event` 直接写 stdout，不维护"当前在 reasoning 还是
在 content"之类的内部状态——靠 emit 顺序保证视觉效果。

支持的事件（其它 kind 静默忽略）：

- ``content.delta`` → 写 ``delta`` 到 stdout 并 flush（正文打字机效果）
- ``reasoning.delta`` → 用 ANSI 灰色前景包裹 ``delta``（可关；渐弱视觉区分思考链）
- ``llm.stream.end`` → 写换行（结束本轮 UI 输出，让下一行 prompt 干净）
- ``tool.call.start`` → 写换行（既有 EventKind，由 ``_execute_tool_calls`` emit；
  在 tool 调用前收尾当前 content 行）

不支持流式 ``tool_call.*`` 渲染（spec §1.4 明确不升 EventKind；CLI 不显示
半成品 tool_call 参数）。
"""

from __future__ import annotations

import sys
from typing import TextIO

from core.contracts import Event

# ANSI 灰色前景（90 = bright black），与 cli_adapter 的 reasoning 渲染色一致；
# 终止序列 \x1b[0m。
_DEFAULT_REASONING_COLOR = "\x1b[90m"
_RESET = "\x1b[0m"


class CLIStreamSink:
    """流式 CLI 渲染器（满足 :class:`core.contracts.EventSink` Protocol）。

    Args:
        out: 文本输出流。默认 ``sys.stdout``；测试可注入 :class:`io.StringIO`。
        show_reasoning: 是否显示 ``reasoning.delta``。``False`` 时完全跳过
            reasoning 渲染（不写 stdout），尊重用户"不显示思考链"的意图。
            默认 ``True``。
        reasoning_color: ``reasoning.delta`` 的 ANSI 前景色 ESC 序列。
            ``None`` 时禁用染色直接打印（适合不支持 ANSI 的终端）。
            注意：``reasoning_color`` 仅控**染色**，不控**显示与否**——
            是否显示由 ``show_reasoning`` 决定。
    """

    def __init__(
        self,
        *,
        out: TextIO | None = None,
        show_reasoning: bool = True,
        reasoning_color: str | None = _DEFAULT_REASONING_COLOR,
    ) -> None:
        self._out: TextIO = out if out is not None else sys.stdout
        self._show_reasoning: bool = show_reasoning
        self._reasoning_color: str | None = reasoning_color

    async def emit(self, event: Event) -> None:
        """按 event kind 分派渲染；未识别 kind 静默忽略，绝不抛异常。"""
        kind = event.kind
        if kind == "content.delta":
            delta = event.payload.get("delta", "")
            if not isinstance(delta, str) or not delta:
                return
            self._out.write(delta)
            self._out.flush()
            return

        if kind == "reasoning.delta":
            if not self._show_reasoning:
                return
            delta = event.payload.get("delta", "")
            if not isinstance(delta, str) or not delta:
                return
            if self._reasoning_color:
                self._out.write(f"{self._reasoning_color}{delta}{_RESET}")
            else:
                self._out.write(delta)
            self._out.flush()
            return

        if kind == "llm.stream.end":
            # 流结束 → 收尾换行
            self._out.write("\n")
            self._out.flush()
            return

        if kind == "tool.call.start":
            # tool 调用开始 → 收尾换行（让 tool 输出从新行开始）
            self._out.write("\n")
            self._out.flush()
            return

        # 其它 kind 不渲染


__all__ = ["CLIStreamSink"]
