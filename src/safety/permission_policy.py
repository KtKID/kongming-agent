"""细粒度权限规则判断（v0.1.4 已迁移到 SafetyDecisionEngine）。

v0.1.3 角色：

- 回答"这次具体工具调用是 ``allow`` / ``deny`` / ``ask``"
- 输入：``tool_name + args``；输出：:class:`PermissionDecision`
- ``ask`` 语义对应 :class:`core.contracts.ApprovalProvider`

v0.1.4 变化（任务 #38/#39）：

- **不再做运行时判定**：``check()`` 方法保留签名，方法体改为
  ``raise NotImplementedError``。所有规则匹配下沉到 v0.1.4 三张规则表
  + :class:`safety.guards.consent.ConsentResolver` 等。
- **保留 `from_config` 作为迁移入口**：v0.1.3 yaml 中的 ``permission rules``
  字段（``tool_name`` + ``arg_contains`` + ``outcome``）翻译为新规则表派生
  条目（参见 :data:`PermissionPolicy.migrated_*`）。
- **v0.1.3 默认行为映射**：
  - ``outcome=deny + path`` → :class:`SensitivePathRule`(effect="block")
  - ``outcome=ask + path`` → :class:`SensitivePathRule`(effect="elevated")
  - ``outcome=ask + tool_name`` → :class:`ApprovalRequiredCommand`(severity="standard")
  - ``outcome=allow + path`` → 转成 config grant 装入 GrantStore（由装配层处理）

迁移指引详见 ``docs/modules/安全/README.md`` 末尾。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from infrastructure.config.models import Config
from safety.types import (
    ApprovalRequiredCommand,
    BoundaryScope,
    SensitivePathRule,
)

# ---------------------------------------------------------------------------
# 数据结构（保留供 v0.1.3 调用方导入）
# ---------------------------------------------------------------------------


PermissionOutcome = Literal["allow", "deny", "ask"]


@dataclass(frozen=True)
class PermissionRule:
    """单条权限规则（v0.1.3 兼容字段）。"""

    tool_name: str
    outcome: PermissionOutcome
    arg_key: str | None = None
    arg_contains: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class PermissionDecision:
    """单次 tool 调用的权限判定结果（v0.1.3 兼容字段，v0.1.4 不再产生）。"""

    outcome: PermissionOutcome
    matched_rule: PermissionRule | None
    reason: str


# ---------------------------------------------------------------------------
# PermissionPolicy
# ---------------------------------------------------------------------------


class PermissionPolicy:
    """v0.1.4 占位类。

    v0.1.3 时承担细粒度规则匹配；v0.1.4 起不再做运行时决策，仅作为 v0.1.3
    yaml 字段到新规则表 / GrantStore 的迁移入口。

    ``check()`` 方法签名保留以避免外部 import 断链；调用 raise
    :class:`NotImplementedError`，引导 caller 改走 SafetyDecisionEngine。
    """

    def __init__(
        self,
        rules: Sequence[PermissionRule] = (),
        *,
        default_outcome: PermissionOutcome = "ask",
    ) -> None:
        self._rules: list[PermissionRule] = list(rules)
        self._default_outcome: PermissionOutcome = default_outcome
        # 迁移产物
        (
            self._migrated_sensitive_paths,
            self._migrated_approval_required_commands,
            self._migrated_allow_path_grants,
        ) = _translate_legacy_rules(self._rules)

    @property
    def rules(self) -> tuple[PermissionRule, ...]:
        """暴露原始规则元组（供测试 / 迁移工具访问）。"""
        return tuple(self._rules)

    @property
    def default_outcome(self) -> PermissionOutcome:
        return self._default_outcome

    @property
    def migrated_sensitive_paths(self) -> tuple[SensitivePathRule, ...]:
        """v0.1.3 ``outcome=deny/ask + path`` 翻译后的 sensitive_paths 规则。"""
        return self._migrated_sensitive_paths

    @property
    def migrated_approval_required_commands(self) -> tuple[ApprovalRequiredCommand, ...]:
        """v0.1.3 ``outcome=ask + tool_name`` 翻译后的 approval_required_commands 规则。"""
        return self._migrated_approval_required_commands

    @property
    def migrated_allow_path_grants(self) -> tuple[tuple[str, str], ...]:
        """v0.1.3 ``outcome=allow + path`` 翻译后的 grant 候选元组。

        每条形如 ``(capability, path_prefix)``，由装配层（``build_safety_chain``）
        在创建 :class:`safety.grant_store.GrantStore` 时转为 config grants。
        """
        return self._migrated_allow_path_grants

    async def check(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> PermissionDecision:
        """v0.1.4 已删除内部判定。

        Raises:
            NotImplementedError: 永远抛出，提示调用方改走 SafetyDecisionEngine。
        """
        raise NotImplementedError(
            "PermissionPolicy.check is removed in v0.1.4; "
            "decisions moved to SafetyDecisionEngine. "
            "Use safety.chain.build_safety_chain(...) instead."
        )

    async def evaluate(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> PermissionDecision:
        """v0.1.4 已删除内部判定（``evaluate`` 是 v0.1.3 的别名调用点）。

        Raises:
            NotImplementedError: 永远抛出。
        """
        raise NotImplementedError(
            "PermissionPolicy.evaluate is removed in v0.1.4; "
            "decisions moved to SafetyDecisionEngine."
        )

    @classmethod
    def from_config(cls, config: Config) -> PermissionPolicy:
        """v0.1.3 yaml → v0.1.4 迁移入口。

        映射规则：

        - ``approval.mode = "auto_allow"`` → ``default_outcome="allow"``
        - ``approval.mode = "auto_deny"``  → ``default_outcome="deny"``
        - ``approval.mode = "interactive"``→ ``default_outcome="ask"``（默认）

        v0.1.3 第一版没有从 yaml 读 rules 的能力；v0.1.4 也维持这个状态——
        新规则表（``safety.hard_deny_commands`` / ``approval_required_commands``
        / ``sensitive_paths``）是 v0.1.4 的唯一规则源。本方法保留 v0.1.3 调用
        路径不抛异常即可。
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


# ---------------------------------------------------------------------------
# 迁移辅助
# ---------------------------------------------------------------------------


# v0.1.3 PermissionRule 的语义识别：
# - 规则带 arg_key=path 视为路径规则（用 arg_contains 作为 path prefix）
# - 规则没有 arg_key 视为命令/工具级规则（按 tool_name 匹配）
def _translate_legacy_rules(
    rules: Sequence[PermissionRule],
) -> tuple[
    tuple[SensitivePathRule, ...],
    tuple[ApprovalRequiredCommand, ...],
    tuple[tuple[str, str], ...],
]:
    """把 v0.1.3 PermissionRule 列表翻译成 v0.1.4 三张表 + grant 候选。

    Returns:
        ``(sensitive_paths, approval_required_commands, allow_path_grants)``：
        分别按 outcome + 规则形态（path 类 vs tool 类）分流。
    """
    sensitive_paths: list[SensitivePathRule] = []
    approval_required_commands: list[ApprovalRequiredCommand] = []
    allow_path_grants: list[tuple[str, str]] = []

    for idx, rule in enumerate(rules):
        is_path_rule = (
            rule.arg_key in {"path", "file_path", "target"} and rule.arg_contains is not None
        )

        if rule.outcome == "deny" and is_path_rule and rule.arg_contains is not None:
            sensitive_paths.append(
                SensitivePathRule(
                    name=f"legacy-permission-deny-{idx}-{rule.tool_name}",
                    matcher=rule.arg_contains,
                    match_mode="path_prefix",
                    ops=frozenset({"read", "write"}),
                    effect="block",
                    boundary_scope=BoundaryScope.ANY,
                    reason=rule.reason
                    or f"v0.1.3 permission deny migration ({rule.arg_contains!r})",
                )
            )
        elif rule.outcome == "ask" and is_path_rule and rule.arg_contains is not None:
            sensitive_paths.append(
                SensitivePathRule(
                    name=f"legacy-permission-ask-{idx}-{rule.tool_name}",
                    matcher=rule.arg_contains,
                    match_mode="path_prefix",
                    ops=frozenset({"read", "write"}),
                    effect="elevated",
                    boundary_scope=BoundaryScope.ANY,
                    reason=rule.reason
                    or f"v0.1.3 permission ask migration ({rule.arg_contains!r})",
                )
            )
        elif rule.outcome == "ask":
            # 工具级 ask → approval_required_commands(standard)
            approval_required_commands.append(
                ApprovalRequiredCommand(
                    name=f"legacy-permission-ask-tool-{idx}-{rule.tool_name}",
                    matcher=f"legacy_{rule.tool_name}",
                    match_mode="profile",
                    severity="standard",
                    boundary_scope=BoundaryScope.ANY,
                    reason=rule.reason
                    or f"v0.1.3 permission ask migration (tool={rule.tool_name})",
                )
            )
        elif rule.outcome == "allow" and is_path_rule and rule.arg_contains is not None:
            # path 类 allow → 转成 GrantStore 候选（capability=file_write 默认）
            capability = _infer_capability_from_tool(rule.tool_name)
            allow_path_grants.append((capability, rule.arg_contains))
        # outcome=allow + tool_only / outcome=deny + tool_only：v0.1.3 时本就罕见，
        # v0.1.4 不再迁移；这种规则建议改为关闭对应工具或写到 hard_deny。

    return (
        tuple(sensitive_paths),
        tuple(approval_required_commands),
        tuple(allow_path_grants),
    )


def _infer_capability_from_tool(tool_name: str) -> str:
    """v0.1.3 tool_name → v0.1.4 capability 名推导（保守 fallback）。"""
    if tool_name in {"read_file", "list_dir"}:
        return "file_read"
    if tool_name in {"write_file", "edit_file"}:
        return "file_write"
    if tool_name == "run_shell":
        return "shell"
    if tool_name == "*":
        return "file_write"  # 通配 → 默认写入语义
    return tool_name


__all__ = [
    "PermissionDecision",
    "PermissionOutcome",
    "PermissionPolicy",
    "PermissionRule",
]
