"""Codex Web transport 的 permission mode 到 CLI flag 映射。"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from hosts.web.protocol import CodexPermissionMode


class SandboxMode(StrEnum):
    """Codex CLI sandbox 模式。"""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


class ApprovalPolicy(StrEnum):
    """Codex CLI approval policy。"""

    UNTRUSTED = "untrusted"
    ON_REQUEST = "on-request"
    NEVER = "never"


_MODE_TABLE: Final[dict[CodexPermissionMode, tuple[SandboxMode, ApprovalPolicy]]] = {
    CodexPermissionMode.DEFAULT: (
        SandboxMode.WORKSPACE_WRITE,
        ApprovalPolicy.UNTRUSTED,
    ),
    CodexPermissionMode.ACCEPT_EDITS: (
        SandboxMode.WORKSPACE_WRITE,
        ApprovalPolicy.NEVER,
    ),
    CodexPermissionMode.BYPASS_PERMISSIONS: (
        SandboxMode.DANGER_FULL_ACCESS,
        ApprovalPolicy.NEVER,
    ),
}


def map_permission_mode(
    mode: str | CodexPermissionMode,
) -> tuple[SandboxMode, ApprovalPolicy]:
    """返回已校验 permission mode 对应的 sandbox 与 approval policy。"""
    try:
        canonical = CodexPermissionMode(mode)
    except ValueError as exc:
        raise ValueError(f"unsupported Codex permission mode: {mode!r}") from exc
    return _MODE_TABLE[canonical]


__all__ = [
    "ApprovalPolicy",
    "SandboxMode",
    "map_permission_mode",
]
