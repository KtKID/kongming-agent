"""安全层智能审批策略入口。

本包承载跨 host 复用的自动审批规则、匹配器、配置仓库和策略对象。Web 与
CLI 只能通过这里取得安全策略真源，避免 host 层之间互相 import。
"""

from __future__ import annotations

from safety.auto_approval.config_store import ConfigStore, ProjectConfig
from safety.auto_approval.policy import AutoApprovalPolicy, Decision
from safety.auto_approval.rules import (
    RuleDefinition,
    load_default_rules,
    materialize_user_rules_yaml,
)

__all__ = [
    "AutoApprovalPolicy",
    "ConfigStore",
    "Decision",
    "ProjectConfig",
    "RuleDefinition",
    "load_default_rules",
    "materialize_user_rules_yaml",
]
