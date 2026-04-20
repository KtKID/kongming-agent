"""细粒度权限规则判断。

职责边界：

- 回答"这次具体工具调用是 ``allow`` / ``deny`` / ``ask``"
- 输入：``tool_name + args``；输出：:class:`PermissionDecision`
- ``ask`` 语义对应 :class:`core.contracts.ApprovalProvider` —— 装配层会把 ``ask``
  转给底层 approval provider 处理（典型为 :class:`tools.approval.InteractiveApproval`）
- **不**负责实际审批交互，也**不**直接抛异常；只返回结构化判定

规则模型（v1-mini 第一版简化）：

- 每条 :class:`PermissionRule` 由 ``tool_name``（``*`` 匹配所有）+ 可选
  ``arg_key / arg_contains``（子串匹配，不引入 glob / regex）组成
- 规则按顺序匹配，**第一条命中的规则生效**
- 未命中任何规则 → 返回 ``default_outcome``

**内部组件约束**（来自 ``10-contracts.md``）：

- ``PermissionPolicy`` 是 ``safety/`` 内部组件，**不进 core/contracts.py**
- ``Tool Runtime`` 不直接 import 它；运行时装配层负责把它串进高层安全链
- v1-mini 不提前 Protocol 化，v0.2+ 再考虑稳定化
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from config_loader.models import Config

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


PermissionOutcome = Literal["allow", "deny", "ask"]


@dataclass(frozen=True)
class PermissionRule:
    """单条权限规则。

    Attributes:
        tool_name: 工具名；``*`` 表示匹配所有工具。
        outcome: 命中时的判定（``allow`` / ``deny`` / ``ask``）。
        arg_key: 可选 arg 匹配键。为 ``None`` 表示不按参数过滤，只按 ``tool_name`` 匹配。
        arg_contains: 可选子串匹配值。仅当 ``arg_key is not None`` 时生效，
            要求 ``args[arg_key]`` 是字符串且包含此子串才命中。
        reason: 命中时附带的人类可读理由。
    """

    tool_name: str
    outcome: PermissionOutcome
    arg_key: str | None = None
    arg_contains: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class PermissionDecision:
    """单次 tool 调用的权限判定结果。

    Attributes:
        outcome: ``allow`` / ``deny`` / ``ask``。
        matched_rule: 命中的 :class:`PermissionRule`；``None`` 表示走了 ``default_outcome``。
        reason: 判定理由，会拼进 :class:`core.contracts.ApprovalDecision.reason`。
    """

    outcome: PermissionOutcome
    matched_rule: PermissionRule | None
    reason: str


# ---------------------------------------------------------------------------
# PermissionPolicy
# ---------------------------------------------------------------------------


class PermissionPolicy:
    """细粒度规则判断。按规则顺序匹配，第一条命中的规则生效。"""

    def __init__(
        self,
        rules: Sequence[PermissionRule] = (),
        *,
        default_outcome: PermissionOutcome = "ask",
    ) -> None:
        self._rules: list[PermissionRule] = list(rules)
        self._default_outcome: PermissionOutcome = default_outcome

    async def check(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> PermissionDecision:
        """按规则顺序判定一次工具调用的权限归属。"""
        for rule in self._rules:
            if not self._matches(rule, tool_name, args):
                continue
            return PermissionDecision(
                outcome=rule.outcome,
                matched_rule=rule,
                reason=rule.reason or f"matched rule for '{rule.tool_name}'",
            )
        return PermissionDecision(
            outcome=self._default_outcome,
            matched_rule=None,
            reason=f"no rule matched; default={self._default_outcome}",
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _matches(
        rule: PermissionRule,
        tool_name: str,
        args: dict[str, Any],
    ) -> bool:
        if rule.tool_name != "*" and rule.tool_name != tool_name:
            return False
        # 未声明 arg 过滤：整条按 tool_name 命中。
        if rule.arg_key is None or rule.arg_contains is None:
            return True
        value = args.get(rule.arg_key)
        if not isinstance(value, str):
            return False
        return rule.arg_contains in value

    # ------------------------------------------------------------------
    # 装配糖
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: Config) -> PermissionPolicy:
        """按统一配置装配默认 :class:`PermissionPolicy`。

        映射规则：

        - ``approval.mode = "auto_allow"`` → ``default_outcome="allow"``
        - ``approval.mode = "auto_deny"``  → ``default_outcome="deny"``
        - ``approval.mode = "interactive"``→ ``default_outcome="ask"``（默认）

        v1-mini 第一版不从配置文件读自定义 rules；留给 v0.2+ 扩展。
        """
        mode = config.approval.mode
        default: PermissionOutcome
        if mode == "auto_allow":
            default = "allow"
        elif mode == "auto_deny":
            default = "deny"
        else:
            default = "ask"
        return cls(rules=(), default_outcome=default)


__all__ = [
    "PermissionDecision",
    "PermissionOutcome",
    "PermissionPolicy",
    "PermissionRule",
]
