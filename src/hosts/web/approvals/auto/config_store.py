"""Compatibility exports for the shared auto-approval config store."""

from __future__ import annotations

from safety.auto_approval.config_store import ConfigStore, ProjectConfig, cwd_hash

__all__ = ["ConfigStore", "ProjectConfig", "cwd_hash"]
