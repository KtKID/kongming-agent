"""智能审批 v1（smart-approval-v1）模块。

独立 package，对外只暴露 policy + audit + config_store 三个集成点。
内部 matchers / rules 不对外暴露，避免被 ApprovalBridge 直接耦合。

模块依赖方向（单向，严禁反向）::

    matchers.py  ────┐
                     ↓
    rules.py ───► policy.py ◄─── config_store.py
                     ↑
                     │
    ApprovalBridge ──┘    audit.py（被 ApprovalBridge 直接调用，与 policy 平级）

- ``matchers.py``：零依赖纯函数模块
- ``rules.py``：dataclass + yaml 解析
- ``policy.py``：依赖 rules + matchers + config_store
- ``config_store.py``：独立读写
- ``audit.py``：独立追加

详见 dev-pipeline/tasks/smart-approval-v1/README.md。
"""

from __future__ import annotations

from safety.auto_approval.config_store import ConfigStore, ProjectConfig
from safety.auto_approval.policy import AutoApprovalPolicy, Decision
from safety.auto_approval.rules import (
    RuleDefinition,
    load_default_rules,
    materialize_user_rules_yaml,
)
from web.auto_approval.audit import AuditLogger

__all__ = [
    "AuditLogger",
    "AutoApprovalPolicy",
    "ConfigStore",
    "Decision",
    "ProjectConfig",
    "RuleDefinition",
    "load_default_rules",
    "materialize_user_rules_yaml",
]
