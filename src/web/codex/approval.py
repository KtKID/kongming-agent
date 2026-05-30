"""Codex 三档权限映射（v0.1）。

抄 claudecodeui ``server/openai-codex.js:168-187`` 的 ``mapPermissionModeToCodexOptions``。
v0.1 没有 per-call 审批 UI（exec 模式做不到），唯一的"审批"控制点
就是这里——把前端的 ``permissionMode`` 三档映射到 codex CLI 的
``--sandbox`` + ``--config approval_policy=...`` 组合。

设计要点：

- 抄死，不创新（与 ccui 完全对齐方便对比 bug）
- 未知 mode 走 default 兜底（不抛错，protocol 宽松）
- v0.2+ 若官方暴露 jsonl approval 事件，再加 ApprovalBridge 同
  ``src/web/claude_code/approval.py:229`` 那套
"""

from __future__ import annotations

from typing import Final, Literal

# 三档常量（前端传值）
PermissionMode = Literal["default", "acceptEdits", "bypassPermissions"]

# Codex CLI 的 sandbox 档位
SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]

# Codex CLI 的 approval policy
ApprovalPolicy = Literal["untrusted", "on-request", "never"]

_MODE_TABLE: Final[dict[str, tuple[SandboxMode, ApprovalPolicy]]] = {
    "default": ("workspace-write", "untrusted"),
    "acceptEdits": ("workspace-write", "never"),
    "bypassPermissions": ("danger-full-access", "never"),
}


def map_permission_mode(mode: str) -> tuple[SandboxMode, ApprovalPolicy]:
    """permissionMode → (sandbox, approval_policy)；未知 mode 走 default。"""
    return _MODE_TABLE.get(mode, _MODE_TABLE["default"])


__all__ = [
    "ApprovalPolicy",
    "PermissionMode",
    "SandboxMode",
    "map_permission_mode",
]
