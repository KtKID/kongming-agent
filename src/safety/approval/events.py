"""审批事件公开 DTO。

本模块定义 ApprovalManager fan-out 给宿主 sink 的只读视图。关键流程是
manager 内部创建 _PendingApproval 并持有 Future / timeout task，再投影成
PendingApprovalView 交给 CLI、Web inbox、Avatar 等展示层。

关键类型：
- PendingApprovalView：审批请求的公开展示快照，不包含 manager 内部控制流句柄。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class PendingApprovalView:
    """审批请求的公开只读视图。

    关键输入：ApprovalManager 内部 pending 状态投影出的展示字段。
    关键输出：宿主 sink 可安全消费的审批快照，不暴露 Future 等内部生命周期对象。
    """

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
    auto_approve_at_ms: int | None = None
    auto_reject_at_ms: int | None = None
    arrived_at_ms: int = 0
    timeout_ms: int = 0

    def __post_init__(self) -> None:
        """冻结可变映射字段，防止宿主 sink 改写 manager 内部状态。"""
        object.__setattr__(self, "tool_input", MappingProxyType(dict(self.tool_input)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


__all__ = ["PendingApprovalView"]
