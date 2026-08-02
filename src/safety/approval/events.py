"""审批管理器对宿主公开的不可变事件 DTO。

关键流程是 ApprovalManager 创建 pending、冻结 danger 与 thread remember 上下文，
再将本视图 fan-out 给 CLI、Web inbox 和 Avatar。Future 与任务句柄保持模块私有。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from safety.approval.rule_models import RememberRule


@dataclass(frozen=True)
class PendingApprovalView:
    """审批请求的公开只读快照。"""

    request_id: str
    channel: str
    thread_id: str
    agent_id: str = ""
    cwd: str = ""
    tool_name: str = ""
    tool_input: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    severity: str = "standard"
    matched_rule: str | None = None
    danger: bool = False
    remember_allowed: bool = False
    arrived_at_ms: int = 0
    timeout_ms: int = 0
    remember_rule: RememberRule | None = None
    auto_approve_at_ms: int | None = None

    def __post_init__(self) -> None:
        """冻结可变映射字段，避免宿主 sink 改写 pending 状态。"""
        object.__setattr__(self, "tool_input", MappingProxyType(dict(self.tool_input)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


__all__ = ["PendingApprovalView"]
