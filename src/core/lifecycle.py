"""Core 内部生命周期 hook。

这**不是**跨模块共享协议（所以不进 :mod:`core.contracts`）。
它只是 runner 内部为扩展点预留的可选协议：装配层可以塞一些
 before_turn / after_turn / before_tool / after_tool 钩子进来，
便于做调试、日志、dry-run 拦截之类的事。

观测事件（Event）仍然走 :class:`core.contracts.EventSink` fan-out，
不由 LifecycleHook 承担。两者职责不同：

- ``EventSink`` 是事实落地面，跨模块。
- ``LifecycleHook`` 是 core 内部的轻量切入点，可以改变 run 行为（比如早退）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.message import Message, ToolCall
from core.run_state import RunState


@runtime_checkable
class LifecycleHook(Protocol):
    """可选的生命周期 hook。

    runner 会在关键节点顺序调用所有注册的 hook，任何 hook 抛出的异常都应该
    被 runner 捕获并以 :class:`core.errors.AgentError` 的方式走错误路径，
    不允许让 hook 的异常穿透主链路。

    所有方法都是 async；默认什么都不做（通过可选 Protocol 语义，实现方可以只实现感兴趣的那一两个）。
    """

    async def before_turn(self, state: RunState) -> None: ...
    async def after_turn(self, state: RunState, assistant_message: Message) -> None: ...
    async def before_tool(self, state: RunState, call: ToolCall) -> None: ...
    async def after_tool(
        self, state: RunState, call: ToolCall, result_message: Message
    ) -> None: ...


__all__ = ["LifecycleHook"]
