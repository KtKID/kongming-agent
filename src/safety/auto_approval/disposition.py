"""default:ask 的处置模式合同。"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class ApprovalDispositionMode(StrEnum):
    """每个工作目录的 default:ask 处置方式。"""

    USER = "user"
    LLM = "llm"
    FULL_TRUST = "full_trust"


class ApprovalDispositionResolver(Protocol):
    """按 cwd 查询 default:ask 处置模式的最小门户合同。"""

    def mode_for(self, cwd: str) -> ApprovalDispositionMode:
        """返回目标 cwd 的处置模式。"""
        ...


__all__ = ["ApprovalDispositionMode", "ApprovalDispositionResolver"]
