"""Agent workflow 子任务与运行结果值对象。

本模块只定义 workflow 编排所需的不可变任务合同和结果合同。子任务的创建与运行
生命周期统一归 AgentManager/TaskRegistry；策略和报告层共享这里的值对象。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from application.subagents.permissions import SubAgentPermissionSpec
from application.subagents.runtime_resolver import ResolvedSubAgentRuntime


def _normalized_names(values: tuple[str, ...]) -> tuple[str, ...]:
    """规范化名称集合，输入为字符串元组，输出为去空白去重后的稳定元组。"""
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


@dataclass(frozen=True)
class SubAgentTask:
    """单个 workflow child 任务规格。"""

    task_id: str
    task_name: str
    prompt: str
    context: str = ""
    tool_names: tuple[str, ...] = ()
    requested_tool_names: tuple[str, ...] | None = None
    skill_names: tuple[str, ...] = ()
    agent_role_id: str | None = None
    permission: SubAgentPermissionSpec | None = None
    runtime: ResolvedSubAgentRuntime | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """冻结工具声明，保留缺省继承与显式空集合的差异。"""
        tool_names = _normalized_names(self.tool_names)
        requested = self.requested_tool_names
        if requested is None:
            requested = tool_names if tool_names else None
        else:
            requested = _normalized_names(requested)
            tool_names = requested
        object.__setattr__(self, "tool_names", tool_names)
        object.__setattr__(self, "requested_tool_names", requested)
        object.__setattr__(self, "skill_names", _normalized_names(self.skill_names))
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True)
class SubAgentRun:
    """一个 workflow child 的终态结果。"""

    task: SubAgentTask
    session_id: str
    run_id: str
    status: str
    content: str
    error_message: str | None
    turn_count: int
    usage: dict[str, int] = field(default_factory=dict)


__all__ = ["SubAgentRun", "SubAgentTask"]
