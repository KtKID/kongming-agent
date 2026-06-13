"""Compatibility exports for shared auto-approval matchers."""

from __future__ import annotations

from safety.auto_approval.matchers import matches, normalize_bash_cmd, split_chained

__all__ = ["matches", "normalize_bash_cmd", "split_chained"]
