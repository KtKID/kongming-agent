"""``ApprovalProvider`` 的默认实现集合。

``ApprovalProvider`` 协议真源在 :mod:`core.contracts`。本模块不重定义协议，
只提供 v1-mini 第一版需要的三种具体实现：

- :class:`InteractiveApproval`：命中 ``ask`` 时走调用方（例如 CLI）注入的
  ``prompt_fn`` 回调拿人工确认。
- :class:`AutoAllowApproval`：无条件放行，给自动化测试 / 批处理用。
- :class:`AutoDenyApproval`：无条件拒绝，给压测 deny 分支用。

和 :func:`build_default_approval` 一起，装配层只要读 ``config.approval.mode``
就可以决定落地哪一套实现，避免配置和实现在不同文件重复分支。

边界提醒：

- 本模块**不**判断"某次工具调用是否应该进 ask 流程"——那是
  :mod:`safety.permission_policy` 的事，:class:`ApprovalProvider` 只负责
  "在被问到时给一个决定"。
- 本模块**不 import** ``safety/`` 下任何内部 policy 组件（硬约束）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from core.contracts import (
    ApprovalDecision,
    ApprovalProvider,
    ApprovalRequest,
)

PromptFn = Callable[[ApprovalRequest], Awaitable[bool]]
"""人工确认回调签名：接收 :class:`ApprovalRequest`，异步返回 ``True/False``。

典型实现：CLI 会在终端打印工具名、参数、命中原因，然后读一行 yes/no；
其他宿主（未来的 IDE / GUI）会走自己的 UI 渠道拿同一个布尔值。
"""


class InteractiveApproval:
    """交互式审批：通过传入的 ``prompt_fn`` 拿人工确认。

    这个实现完全不关心"如何"与用户交互——CLI / GUI / 测试 stub 各自决定，
    只要实现方返回 ``True`` / ``False`` 即可。这样 :class:`ApprovalProvider`
    的形状就能在不同宿主之间保持一致。
    """

    def __init__(self, prompt_fn: PromptFn) -> None:
        if prompt_fn is None:
            raise ValueError("InteractiveApproval requires a non-None prompt_fn")
        self._prompt = prompt_fn

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        approved = await self._prompt(request)
        if approved:
            return ApprovalDecision(
                outcome="approved",
                reason="user confirmed",
                metadata={"source": "interactive"},
            )
        return ApprovalDecision(
            outcome="rejected",
            reason="user rejected",
            metadata={"source": "interactive"},
        )


class AutoAllowApproval:
    """自动放行所有审批请求。

    只用于自动化测试 / 批处理场景。生产使用必须上层明确开启，
    不允许作为隐式 fallback。
    """

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(
            outcome="approved",
            reason="auto_allow mode",
            metadata={"source": "auto_allow", "tool_name": request.tool_name},
        )


class AutoDenyApproval:
    """自动拒绝所有审批请求。

    主要服务于压测 deny 分支和紧急禁用场景。
    """

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(
            outcome="rejected",
            reason="auto_deny mode",
            metadata={"source": "auto_deny", "tool_name": request.tool_name},
        )


def build_default_approval(
    mode: str,
    *,
    prompt_fn: PromptFn | None = None,
) -> ApprovalProvider:
    """按 ``config.approval.mode`` 造一个 :class:`ApprovalProvider`。

    Args:
        mode: ``"interactive"`` / ``"auto_allow"`` / ``"auto_deny"`` 之一。
        prompt_fn: ``interactive`` 模式必填；其他模式忽略。

    Raises:
        ValueError: ``mode`` 不在白名单 / interactive 模式缺 ``prompt_fn``。
    """
    if mode == "interactive":
        if prompt_fn is None:
            raise ValueError("interactive approval requires prompt_fn")
        return InteractiveApproval(prompt_fn)
    if mode == "auto_allow":
        return AutoAllowApproval()
    if mode == "auto_deny":
        return AutoDenyApproval()
    raise ValueError(f"unknown approval mode: {mode!r}")


__all__ = [
    "AutoAllowApproval",
    "AutoDenyApproval",
    "InteractiveApproval",
    "PromptFn",
    "build_default_approval",
]
