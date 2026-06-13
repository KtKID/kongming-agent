"""粗粒度能力门禁（v0.1.4 已迁移到 SafetyDecisionEngine）。

v0.1.3 角色：

- 回答"这次运行是否允许使用某类能力"，例如 ``shell`` / ``file_read`` / ``file_write``
- 输入：``tool_name + args``；输出：:class:`CapabilityCheck`（``allow`` / ``deny``）

v0.1.4 变化（任务 #37/#39）：

- **不再做运行时判定**：``check()`` 方法保留签名，方法体改为
  ``raise NotImplementedError``。所有 capability 判定全部下沉到
  :class:`safety.approval.decision_engine.SafetyDecisionEngine`。
- **保留 `from_config` 作为迁移入口**：v0.1.3 yaml 中的 ``capability allow=...``
  / ``deny=...`` 字段（实际由 ``config.tool.<name>.enabled`` 等推导）翻译为新
  规则表的派生条目（:data:`MIGRATED_HARD_DENY_COMMANDS`）。
- **v0.1.3 默认行为保留**：``capability deny`` 派生为 ``HardDenyCommand``
  （match_mode="profile"），追加到 :class:`HardBlockGuard` 的 hard_deny_commands。
  ``capability allow`` 不动（默认 silent_allow 范围由 BoundaryResolver / TrustResolver
  推导）。

迁移指引详见 ``docs/modules/安全/README.md`` 末尾。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from infrastructure.config.models import Config
from safety.approval.types import BoundaryScope, HardDenyCommand

# ---------------------------------------------------------------------------
# 数据结构（保留供 v0.1.3 调用方导入）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilitySet:
    """一次运行允许使用的能力集合（v0.1.3 兼容字段）。

    Attributes:
        allow: 白名单。空集合视为"全开"，配合 ``deny`` 使用。
        deny: 显式黑名单。优先于 ``allow``。
    """

    allow: frozenset[str] = frozenset()
    deny: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CapabilityCheck:
    """单次 tool 调用的能力判定结果（v0.1.3 兼容字段，v0.1.4 不再产生）。"""

    outcome: Literal["allow", "deny"]
    capability: str
    reason: str


# ---------------------------------------------------------------------------
# 默认 tool_name → capability 映射（保留供 v0.1.3 测试期间访问）
# ---------------------------------------------------------------------------


_DEFAULT_TOOL_CAPABILITIES: dict[str, str] = {
    "read_file": "file_read",
    "list_dir": "file_read",
    "write_file": "file_write",
    "run_shell": "shell",
    "memory": "memory",
}


# ---------------------------------------------------------------------------
# CapabilityPolicy
# ---------------------------------------------------------------------------


class CapabilityPolicy:
    """v0.1.4 占位类。

    v0.1.3 时承担 capability 门禁判定；v0.1.4 起不再做运行时决策，仅作为
    v0.1.3 yaml 字段到新规则表的迁移入口（保留 :meth:`from_config` 把
    capability deny 翻译成 :class:`HardDenyCommand`）。

    ``check()`` 方法签名保留以避免外部 import 断链；调用 raise
    :class:`NotImplementedError`，引导 caller 改走 SafetyDecisionEngine。
    """

    def __init__(
        self,
        capability_set: CapabilitySet,
        *,
        tool_capabilities: Mapping[str, str] | None = None,
    ) -> None:
        self._caps = capability_set
        self._tool_map = dict(tool_capabilities or _DEFAULT_TOOL_CAPABILITIES)
        # 迁移产物（_migrated_hard_deny_commands）：capability deny 派生的规则
        self._migrated_hard_deny_commands: tuple[HardDenyCommand, ...] = (
            _derive_hard_deny_commands_from_capability_deny(capability_set.deny)
        )

    @property
    def capability_set(self) -> CapabilitySet:
        """暴露原始 :class:`CapabilitySet`（供测试 / 迁移工具访问）。"""
        return self._caps

    @property
    def migrated_hard_deny_commands(self) -> tuple[HardDenyCommand, ...]:
        """v0.1.3 capability deny 翻译后的 :class:`HardDenyCommand` 元组。

        v0.1.4 SafetyDecisionEngine 装配 :class:`HardBlockGuard` 时合并默认 +
        用户 yaml 规则；本字段提供给 native_runtime / 测试观察迁移效果。
        """
        return self._migrated_hard_deny_commands

    async def check(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> CapabilityCheck:
        """v0.1.4 已删除内部判定。

        Raises:
            NotImplementedError: 永远抛出，提示调用方改走 SafetyDecisionEngine。
        """
        raise NotImplementedError(
            "CapabilityPolicy.check is removed in v0.1.4; "
            "decisions moved to SafetyDecisionEngine. "
            "Use safety.approval.chain.build_safety_chain(...) instead."
        )

    @classmethod
    def from_config(cls, config: Config) -> CapabilityPolicy:
        """v0.1.3 yaml → v0.1.4 规则表迁移入口。

        映射规则：

        - ``config.tool.file.enabled = True`` → ``file_read`` / ``file_write``
          进 allow（不影响 v0.1.4 判定，仅用于历史调用方读 ``capability_set``）
        - ``config.tool.shell.enabled = True`` → ``shell`` 进 allow
        - ``config.evolution.memory.enabled = True`` → ``memory`` 进 allow
        - ``capability deny`` 由用户在 yaml 显式追加（v0.1.3 schema 没暴露，
          但保留接口）；deny 集合派生为 ``HardDenyCommand`` 追加到
          HardBlockGuard 默认规则集

        v0.1.3 测试 / native_runtime 仍可正常 ``CapabilityPolicy.from_config(cfg)``
        拿到一个不抛异常的实例；只是 ``check()`` 不再返回结果。
        """
        allow: set[str] = {"file_read"}
        if config.tool.file.enabled:
            allow.update({"file_read", "file_write"})
        if config.tool.shell.enabled:
            allow.add("shell")
        if config.evolution.memory.enabled:
            allow.add("memory")
        return cls(CapabilitySet(allow=frozenset(allow)))


# ---------------------------------------------------------------------------
# 迁移辅助
# ---------------------------------------------------------------------------


def _derive_hard_deny_commands_from_capability_deny(
    deny_capabilities: frozenset[str],
) -> tuple[HardDenyCommand, ...]:
    """把 v0.1.3 capability deny 集合翻译成 ``HardDenyCommand`` 规则元组。

    每条 capability deny 对应一条 ``match_mode="profile"`` 规则，
    matcher 为 ``"capability_<name>"``。这些 profile 名不会被默认
    :func:`safety.guards.consent._profile_matches` 命中（它仅识别
    git_push / git_reset_hard），所以默认情况下"capability deny X"
    不会自动拒绝普通工具调用——这是 v0.1.4 的明确边角：

    - v0.1.3 capability deny 的本意是"运行级别禁用此能力"
    - v0.1.4 改为通过工具开关（``config.tool.<name>.enabled = false``）实现
      同等效果；capability deny 的派生规则仅作为 trace 留痕

    用户如确实需要拒绝某 capability，应改为关闭对应工具或在
    ``safety.hard_deny_commands`` yaml 字段显式追加 segment_regex 规则。
    """
    return tuple(
        HardDenyCommand(
            name=f"legacy-capability-deny-{cap}",
            matcher=f"capability_{cap}",
            match_mode="profile",
            boundary_scope=BoundaryScope.ANY,
            reason=f"v0.1.3 capability deny migration for {cap!r} (informational)",
        )
        for cap in sorted(deny_capabilities)
    )


__all__ = [
    "CapabilityCheck",
    "CapabilityPolicy",
    "CapabilitySet",
]
