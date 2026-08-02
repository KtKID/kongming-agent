"""Thread permissions DSL 与持久快照的不可变合同。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Verdict(StrEnum):
    """thread permissions 规则命中后的二态裁决。"""

    ALLOW = "allow"
    DENY = "deny"


class MatcherKind(StrEnum):
    """permissions DSL 支持的 matcher 类型。"""

    TOOL_EXACT = "tool_exact"
    TOOL_GLOB = "tool_glob"
    SHELL_PREFIX = "shell_prefix"
    PATH_PREFIX = "path_prefix"
    PATH_GLOB = "path_glob"
    MCP_EXACT = "mcp_exact"
    MCP_GLOB = "mcp_glob"


@dataclass(frozen=True)
class RuleMatch:
    """解析后的 canonical matcher。"""

    kind: MatcherKind
    tool_name: str
    pattern: str
    canonical_expression: str


@dataclass(frozen=True)
class PermissionRuleRecord:
    """一条可持久化的 canonical permission 规则及其目录作用域。"""

    expression: str
    scope_cwd: str | None = None


@dataclass(frozen=True)
class PermissionEntry:
    """一条已解析的 thread permission。"""

    rule: PermissionRuleRecord
    verdict: Verdict
    matcher: RuleMatch

    @property
    def expression(self) -> str:
        """返回 canonical DSL，供审计和既有展示代码读取。"""
        return self.rule.expression

    @property
    def scope_cwd(self) -> str | None:
        """返回规则绑定的 exact cwd。"""
        return self.rule.scope_cwd


@dataclass(frozen=True)
class PermissionResolution:
    """thread permissions 对单次请求的命中结果。"""

    verdict: Verdict
    expression: str
    scope_cwd: str | None = None


@dataclass(frozen=True)
class PermissionsMigrationSummary:
    """schema v1 首次读取迁移到 v2 的一次性结果。"""

    from_schema_version: int
    to_schema_version: int
    invalidated_shell_allow_count: int
    backup_path: str


@dataclass(frozen=True)
class ThreadPermissionsSnapshot:
    """单个 thread 的持久审批本子快照。"""

    thread_id: str
    revision: int
    allow: tuple[PermissionRuleRecord, ...]
    deny: tuple[PermissionRuleRecord, ...]
    updated_at: str | None
    schema_version: int = 2
    migration_summary: PermissionsMigrationSummary | None = None


@dataclass(frozen=True)
class RememberRule:
    """审批卡展示并原样写回当前 thread 的规则候选。"""

    expression: str
    display_text: str
    scope_cwd: str | None = None


__all__ = [
    "MatcherKind",
    "PermissionEntry",
    "PermissionResolution",
    "PermissionRuleRecord",
    "PermissionsMigrationSummary",
    "RememberRule",
    "RuleMatch",
    "ThreadPermissionsSnapshot",
    "Verdict",
]
