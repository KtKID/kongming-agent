"""``ApprovalProvider`` 的默认实现集合。

``ApprovalProvider`` 协议真源在 :mod:`core.contracts`。本模块不重定义协议，
只提供 v1-mini 第一版需要的三种具体实现：

- :class:`InteractiveApproval`：命中 ``ask`` 时走调用方（例如 CLI）注入的
  ``prompt_fn`` 回调拿人工确认。**v0.1.4 升级**：``prompt_fn`` 既支持旧
  ``bool`` 返回（自动映射 ``ACCEPT_ONCE`` / ``REJECT``），也支持新
  :class:`core.contracts.ApprovalAction` 返回，用于 CLI 三按钮 UX
  （``[y]es once / [s]ession / [p]ersist / [n]o``）。
- :class:`AutoAllowApproval`：无条件放行，给自动化测试 / 批处理用。
- :class:`AutoDenyApproval`：无条件拒绝，给压测 deny 分支用。

和 :func:`build_default_approval` 一起，装配层只要读 ``config.approval.mode``
就可以决定落地哪一套实现，避免配置和实现在不同文件重复分支。

边界提醒：

- 本模块**不**判断"某次工具调用是否应该进 ask 流程"——那是
  :mod:`safety.policies.permission` 的事，:class:`ApprovalProvider` 只负责
  "在被问到时给一个决定"。
- 本模块**不 import** ``safety/`` 下任何内部 policy 组件（硬约束）。

v0.1.4 三按钮 UX 协议升级：

- ``PromptFn`` （旧）→ ``Awaitable[bool]``：兼容路径，True → ACCEPT_ONCE，
  False → REJECT。
- ``PromptActionFn``（新）→ ``Awaitable[ApprovalAction]``：让 prompt_fn
  能直接表达"本会话允许"/"持久化到配置"。CLI 端通过 :func:`mark_action_aware`
  装饰器或 ``__action_aware__ = True`` 属性声明该签名。

``InteractiveApproval`` 通过 :func:`_is_action_aware` 探测 prompt_fn 类型，
分发到对应分支：

- bool 返回 → 落 ``ApprovalAction.ACCEPT_ONCE`` / ``REJECT``
- ApprovalAction 返回 → 按 ACCEPT_* / REJECT 映射到 ApprovalDecision
  的 ``outcome`` + ``metadata.grant_scope``。

请求 metadata 中携带 ``policy_hint="elevated"`` + ``confirm_token`` 时，
``InteractiveApproval`` 透传给 prompt_fn；具体 elevated UI（隐藏 [s]/[p]、
typed confirm）由 prompt_fn 实现端处理。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from core.contracts import (
    ApprovalAction,
    ApprovalDecision,
    ApprovalProvider,
    ApprovalRequest,
)

PromptFn = Callable[[ApprovalRequest], Awaitable[bool]]
"""人工确认回调签名（v0.1.3 旧）：接收 :class:`ApprovalRequest`，异步返回
``True/False``。

向后兼容路径：``True`` → :attr:`ApprovalAction.ACCEPT_ONCE`，``False`` →
:attr:`ApprovalAction.REJECT`。
"""


PromptActionFn = Callable[[ApprovalRequest], Awaitable[ApprovalAction]]
"""人工确认回调签名（v0.1.4 三按钮 UX）：接收 :class:`ApprovalRequest`，
异步返回 :class:`ApprovalAction`。

CLI 实现端必须显式标记 ``__action_aware__ = True`` 属性（或用
:func:`mark_action_aware` 装饰器），:class:`InteractiveApproval` 据此
分发；否则会按旧 ``bool`` 协议 await，造成类型错误。
"""


# 二者共同 type alias，便于在 InteractiveApproval.__init__ 上类型清晰
PromptFnUnion = PromptFn | PromptActionFn


def mark_action_aware(fn: PromptActionFn) -> PromptActionFn:
    """装饰器：把 ``prompt_fn`` 显式标记为返回 :class:`ApprovalAction`。

    ``InteractiveApproval`` 通过 :func:`_is_action_aware` 探测此标记。
    例：

    .. code-block:: python

        @mark_action_aware
        async def cli_prompt(request: ApprovalRequest) -> ApprovalAction:
            ...
    """
    fn.__action_aware__ = True  # type: ignore[attr-defined]
    return fn


def _is_action_aware(prompt_fn: object) -> bool:
    """判断 ``prompt_fn`` 是否返回 :class:`ApprovalAction`（而非旧 bool）。

    探测规则（短路顺序）：

    1. 显式属性 ``__action_aware__ == True``（推荐 + 装饰器）
    2. callable 自身或其 ``__call__`` 的返回类型注解（typing.get_type_hints）
       含 ``ApprovalAction``

    探测失败默认按旧 bool 协议处理（保兼容）。
    """
    if getattr(prompt_fn, "__action_aware__", False):
        return True
    # 走 type hints：避免对 lambda / functools.partial 这类没有可靠注解的类型出错
    if not callable(prompt_fn):
        return False
    try:
        target = prompt_fn.__call__ if inspect.ismethod(prompt_fn) else prompt_fn
        hints = inspect.signature(target).return_annotation
    except (TypeError, ValueError):
        return False
    if hints is inspect.Signature.empty:
        return False
    # 注解形如 ``Awaitable[ApprovalAction]`` / ``Coroutine[..., ..., ApprovalAction]``；
    # 简单字符串比对兜底（用户可能写成 "ApprovalAction" 或 "core.contracts.ApprovalAction"）
    hint_str = str(hints)
    return "ApprovalAction" in hint_str


class InteractiveApproval:
    """交互式审批：通过传入的 ``prompt_fn`` 拿人工确认。

    这个实现完全不关心"如何"与用户交互——CLI / GUI / 测试 stub 各自决定，
    只要 prompt_fn 返回值符合签名即可。

    v0.1.4 升级：``prompt_fn`` 可返回 ``bool`` 或 :class:`ApprovalAction`。
    具体调用约定见模块 docstring。
    """

    def __init__(self, prompt_fn: PromptFnUnion) -> None:
        if prompt_fn is None:
            raise ValueError("InteractiveApproval requires a non-None prompt_fn")
        self._prompt = prompt_fn
        self._action_aware = _is_action_aware(prompt_fn)

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """根据 ``prompt_fn`` 返回值构造 :class:`ApprovalDecision`。

        - action-aware prompt_fn → 按 :class:`ApprovalAction` 映射
          ``outcome`` + ``metadata.grant_scope``。
        - bool prompt_fn → True → ACCEPT_ONCE，False → REJECT。

        请求 ``metadata`` 中的 ``policy_hint`` / ``confirm_token`` 不在
        本类做校验：透传给 prompt_fn 由 UI 端处理（elevated 流程）。
        本类只负责"拿到 action 后翻译成 decision"。
        """
        if self._action_aware:
            # action-aware 路径：prompt_fn 返回 ApprovalAction
            result = await self._prompt(request)
            assert isinstance(result, ApprovalAction)  # mypy narrowing
            return _action_to_decision(result, request)
        # 兼容路径：旧 bool prompt_fn
        approved = await self._prompt(request)
        if approved:
            return _action_to_decision(ApprovalAction.ACCEPT_ONCE, request)
        return _action_to_decision(ApprovalAction.REJECT, request)


def _action_to_decision(action: ApprovalAction, request: ApprovalRequest) -> ApprovalDecision:
    """把 :class:`ApprovalAction` 映射到 :class:`ApprovalDecision`。

    映射规则（与 ``core.contracts.ApprovalAction`` docstring 一致）：

    - ACCEPT_ONCE → outcome=approved，metadata 不含 grant_scope
    - ACCEPT_FOR_SESSION → outcome=approved，metadata.grant_scope=session
    - ACCEPT_PERSIST → outcome=approved，metadata.grant_scope=config
    - REJECT → outcome=rejected

    所有路径都附带 ``metadata.source = "interactive"`` 便于 trace 区分；
    elevated 路径下 ``confirm_token`` / ``policy_hint`` 也透传到 metadata
    便于审计。
    """
    base_meta: dict[str, Any] = {"source": "interactive"}
    # 把请求 metadata 中的 elevated 透传到决策 metadata（便于 trace）
    req_meta = request.metadata or {}
    if "policy_hint" in req_meta:
        base_meta["policy_hint"] = req_meta["policy_hint"]
    if "confirm_token" in req_meta:
        base_meta["confirm_token"] = req_meta["confirm_token"]

    if action == ApprovalAction.ACCEPT_ONCE:
        return ApprovalDecision(
            outcome="approved",
            reason="user confirmed",
            metadata=base_meta,
        )
    if action == ApprovalAction.ACCEPT_FOR_SESSION:
        return ApprovalDecision(
            outcome="approved",
            reason="user confirmed (session grant)",
            metadata={**base_meta, "grant_scope": "session"},
        )
    if action == ApprovalAction.ACCEPT_PERSIST:
        return ApprovalDecision(
            outcome="approved",
            reason="user confirmed (persist grant)",
            metadata={**base_meta, "grant_scope": "config"},
        )
    if action == ApprovalAction.REJECT:
        return ApprovalDecision(
            outcome="rejected",
            reason="user rejected",
            metadata=base_meta,
        )
    raise ValueError(f"unknown ApprovalAction: {action!r}")


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
    prompt_fn: PromptFnUnion | None = None,
) -> ApprovalProvider:
    """按 ``config.approval.mode`` 造一个 :class:`ApprovalProvider`。

    Args:
        mode: ``"interactive"`` / ``"auto_allow"`` / ``"auto_deny"`` 之一。
        prompt_fn: ``interactive`` 模式必填；其他模式忽略。
            v0.1.4 起接受 :class:`PromptFn`（旧 bool）或
            :class:`PromptActionFn`（新 :class:`ApprovalAction`）。

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
    "ApprovalAction",
    "AutoAllowApproval",
    "AutoDenyApproval",
    "InteractiveApproval",
    "PromptActionFn",
    "PromptFn",
    "PromptFnUnion",
    "build_default_approval",
    "mark_action_aware",
]
