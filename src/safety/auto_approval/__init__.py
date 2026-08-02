"""Shared auto-approval policy primitives.

This package owns the host-independent approval rule engine used by Web and
CLI channels. Web-specific websocket handlers and audit output stay under
``web.approvals.auto``.
"""

from __future__ import annotations

from safety.auto_approval.config_store import ConfigStore, ProjectConfig
from safety.auto_approval.disposition import ApprovalDispositionMode, ApprovalDispositionResolver
from safety.auto_approval.manager import AutoApprovalManager
from safety.auto_approval.policy import AutoApprovalPolicy
from safety.auto_approval.rules import (
    RuleDefinition,
    RuleSet,
    load_default_rules,
    materialize_user_rules_yaml,
)

__all__ = [
    "AutoApprovalPolicy",
    "AutoApprovalManager",
    "ApprovalDispositionMode",
    "ApprovalDispositionResolver",
    "ConfigStore",
    "ProjectConfig",
    "RuleDefinition",
    "RuleSet",
    "load_default_rules",
    "materialize_user_rules_yaml",
]
