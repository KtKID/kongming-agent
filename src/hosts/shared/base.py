"""Host 模块内部 concrete base / helper。

这里**不**承载跨模块协议真源（跨模块协议真源只在 :mod:`core.contracts`）。
本文件定义 :class:`HostAdapter`：所有宿主适配器（CLI、未来的 HTTP /
macOS / 游戏宿主）共用的最小抽象骨架，给 :class:`host.session_bridge.SessionBridge`
一个稳定调用面。

设计约束：

- 不定义第二份 ``EventSink`` / ``ApprovalProvider`` / ``Session`` 抽象。
  adapter 只负责 **翻译** 宿主与 runtime 之间的事件，不持有状态机。
- ``notify_event`` 是可选渲染点，默认实现直接丢弃。真正把事件落盘或写日志
  请走 :class:`core.contracts.EventSink` 实现并通过 ``NativeRuntime.build
  (event_sinks=[...])`` 注入，而不是把渲染逻辑往这里塞。
- ``prompt_approval`` 返回 ``bool``，由 :func:`tools.build_default_approval`
  的 ``interactive`` 模式消费；它负责把布尔值翻成
  :class:`core.contracts.ApprovalDecision`。
"""

from __future__ import annotations

from core.contracts import ApprovalRequest, Event


class HostAdapter:
    """宿主适配器基类。

    所有具体宿主（CLI、HTTP、macOS、游戏等）通过子类化这个基类，把自己
    的输入/输出/审批 UI 翻译成 runtime 可见的稳定调用面。

    约定：

    - ``read_input`` 返回 ``None`` 表示"用户结束会话"（EOF / Ctrl-D）；
      抛异常表示真正的故障。
    - ``write_output`` / ``notify_event`` 不应抛异常；若底层 I/O 出错，
      子类自行降级或静默，避免影响主链路。
    - ``prompt_approval`` 的返回值（``True`` 批准 / ``False`` 拒绝）由上层
      :class:`tools.InteractiveApproval` 翻成
      :class:`core.contracts.ApprovalDecision`。
    - ``close`` 用来释放宿主资源（例如 PromptSession），多次调用应保持幂等。
    """

    async def read_input(self) -> str | None:
        """读取一条用户输入。

        Returns:
            用户输入的完整字符串（不含回车）。``None`` 表示宿主希望结束会话。
        """
        raise NotImplementedError

    async def write_output(self, text: str) -> None:
        """把 assistant 最终文本输出给用户。"""
        raise NotImplementedError

    async def notify_event(self, event: Event) -> None:
        """把一个 runtime 事件翻译给宿主（默认 no-op）。

        子类可以覆盖这里，把 ``turn.start`` / ``tool.call.start`` 这类
        事件翻成用户可见的进度行。
        """
        return None

    async def prompt_approval(self, request: ApprovalRequest) -> bool:
        """命中 ask 时拿人工确认。

        返回 ``True`` 表示批准，``False`` 表示拒绝。
        """
        raise NotImplementedError

    async def close(self) -> None:
        """清理资源。子类按需实现，多次调用需幂等。"""
        return None


__all__ = ["HostAdapter"]
