"""粗粒度能力门禁。

职责边界：

- 回答"这次运行是否允许使用某类能力"，例如 ``shell`` / ``file_read`` / ``file_write``
- 输入：``tool_name + args``；输出：:class:`CapabilityCheck`（``allow`` / ``deny``）
- **不**负责细粒度规则匹配（那是 :mod:`safety.permission_policy` 的事）
- **不**负责与人工交互（那是 :class:`core.contracts.ApprovalProvider` 的事）

装配层按 :meth:`CapabilityPolicy.from_config` 从统一 :class:`Config` 读 tool
启用开关并转成 allow 集合；业务代码不需要关心映射逻辑。

**内部组件约束**（来自 ``10-contracts.md``）：

- ``CapabilityPolicy`` 是 ``safety/`` 内部组件，**不进 core/contracts.py**
- ``Tool Runtime`` 不直接 import 它；运行时装配层负责把它串进高层安全链
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from config_loader.models import Config

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilitySet:
    """一次运行允许使用的能力集合。

    Attributes:
        allow: 白名单。空集合视为"全开"，配合 ``deny`` 使用。
        deny: 显式黑名单。优先于 ``allow``——任一能力命中 ``deny`` 即拒绝。
    """

    allow: frozenset[str] = frozenset()
    deny: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CapabilityCheck:
    """单次 tool 调用的能力判定结果。

    Attributes:
        outcome: ``allow`` 或 ``deny``。
        capability: 本次调用对应的能力名（已经过 ``tool_name → capability`` 映射）。
        reason: 判定理由，会拼进 :class:`core.contracts.ApprovalDecision.reason`。
    """

    outcome: Literal["allow", "deny"]
    capability: str
    reason: str


# ---------------------------------------------------------------------------
# 默认 tool_name → capability 映射
# ---------------------------------------------------------------------------


_DEFAULT_TOOL_CAPABILITIES: dict[str, str] = {
    # 文件类 builtin tools
    "read_file": "file_read",
    "list_dir": "file_read",
    "write_file": "file_write",
    # shell 类 builtin tools
    "run_shell": "shell",
    # memory 长期记忆工具：读写活态 memory；作为独立 capability 登记，
    # 避免 fallback 走"capability 名 = 工具名"时意外被非空 allow 集合拒绝。
    "memory": "memory",
}
"""v1-mini builtin tools 的默认能力映射。

对于未登记的工具名，:meth:`CapabilityPolicy.check` 回退到 "capability 名 = 工具名"，
这样第三方 tool 不登记映射时仍可通过精确工具名进入白名单。
"""


# ---------------------------------------------------------------------------
# CapabilityPolicy
# ---------------------------------------------------------------------------


class CapabilityPolicy:
    """粗粒度能力门禁。

    规则：

    1. ``tool_name → capability`` 按 ``tool_capabilities`` 映射；未登记时
       capability = 工具名本身。
    2. capability 命中 ``deny`` 集合：返回 ``outcome=deny``（优先级最高）。
    3. ``allow`` 为空：视为"全开"，返回 ``outcome=allow``。
    4. capability 命中 ``allow`` 集合：返回 ``outcome=allow``。
    5. 其他：返回 ``outcome=deny``。
    """

    def __init__(
        self,
        capability_set: CapabilitySet,
        *,
        tool_capabilities: Mapping[str, str] | None = None,
    ) -> None:
        self._caps = capability_set
        self._tool_map = dict(tool_capabilities or _DEFAULT_TOOL_CAPABILITIES)

    async def check(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> CapabilityCheck:
        """判定单次工具调用的能力是否允许。"""
        capability = self._tool_map.get(tool_name, tool_name)

        if capability in self._caps.deny:
            return CapabilityCheck(
                outcome="deny",
                capability=capability,
                reason=f"capability '{capability}' explicitly denied",
            )

        if not self._caps.allow:
            # 空 allow = 全开；配合黑名单使用。
            return CapabilityCheck(
                outcome="allow",
                capability=capability,
                reason=f"capability '{capability}' allowed (empty allow-list means allow-all)",
            )

        if capability in self._caps.allow:
            return CapabilityCheck(
                outcome="allow",
                capability=capability,
                reason=f"capability '{capability}' in allow list",
            )

        return CapabilityCheck(
            outcome="deny",
            capability=capability,
            reason=f"capability '{capability}' not in allow list",
        )

    @classmethod
    def from_config(cls, config: Config) -> CapabilityPolicy:
        """按统一配置装配默认 :class:`CapabilityPolicy`。

        - ``tool.file.enabled = True`` → 追加 ``file_read`` / ``file_write``
        - ``tool.shell.enabled = True`` → 追加 ``shell``
        - ``evolution.memory.enabled = True`` → 追加 ``memory``
        - ``file_read`` 始终加入默认 allow 集合（反射 / 观察性最低要求）

        未启用的能力不进入 allow，配合"非空 allow = 白名单"语义自动阻断。
        """
        allow: set[str] = {"file_read"}
        if config.tool.file.enabled:
            allow.update({"file_read", "file_write"})
        if config.tool.shell.enabled:
            allow.add("shell")
        if config.evolution.memory.enabled:
            allow.add("memory")
        return cls(CapabilitySet(allow=frozenset(allow)))


__all__ = [
    "CapabilityCheck",
    "CapabilityPolicy",
    "CapabilitySet",
]
