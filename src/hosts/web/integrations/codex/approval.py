"""Codex permission mode mapping for the web bridge.

The frontend sends one of three ``permissionMode`` values. We map that to the
current Codex CLI ``exec`` contract:

- ``--sandbox <mode>``
- ``--config approval_policy="<policy>"``
"""

from __future__ import annotations

from typing import Final, Literal

PermissionMode = Literal["default", "acceptEdits", "bypassPermissions"]
SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]
ApprovalPolicy = Literal["untrusted", "on-request", "never"]

_MODE_TABLE: Final[dict[str, tuple[SandboxMode, ApprovalPolicy]]] = {
    "default": ("workspace-write", "untrusted"),
    "acceptEdits": ("workspace-write", "never"),
    "bypassPermissions": ("danger-full-access", "never"),
}


def map_permission_mode(mode: str) -> tuple[SandboxMode, ApprovalPolicy]:
    """Return the sandbox and approval policy for a frontend permission mode."""

    return _MODE_TABLE.get(mode, _MODE_TABLE["default"])


__all__ = [
    "ApprovalPolicy",
    "PermissionMode",
    "SandboxMode",
    "map_permission_mode",
]
