"""Compatibility exports for shared auto-approval rules."""

from __future__ import annotations

from safety.auto_approval.rules import (
    SUPPORTED_MATCH_KINDS,
    MatchKind,
    RuleDefinition,
    RuleSet,
    load_default_rules,
    materialize_user_rules_yaml,
)

__all__ = [
    "SUPPORTED_MATCH_KINDS",
    "MatchKind",
    "RuleDefinition",
    "RuleSet",
    "load_default_rules",
    "materialize_user_rules_yaml",
]
