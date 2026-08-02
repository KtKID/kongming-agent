"""Agent workflow 公共结果模型。

本脚本承载 workflow strategy 和 AgentWorkflowManager 共享的轻量 DTO。作用是把
strategy 的输出合同从 manager 实现细节中抽离出来，让策略层只依赖公共模型。
关键执行流程：strategy 构造 AgentWorkflowResult，manager 校验并序列化结果。
关键类：SubAgentReportProjection 表达子 agent 报告摘要，AgentWorkflowResult 表达
workflow 运行终态。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from application.agent_workflows.task_models import SubAgentRun

if TYPE_CHECKING:
    # asyncio 仅用于类型注解（task 字段），运行时不加载，保持 models 零运行时并发依赖。
    import asyncio

WorkflowMode = str


@dataclass(frozen=True)
class ActiveWorkflowHandle:
    """运行中 workflow 的句柄，由 AgentWorkflowManager 注册表维护。

    manager 在 workflow 发起时创建并登记此句柄，运行结束（正常完成或 cancel）后清理。
    外部（Web/CLI）通过它查询运行中 workflow、调用 cancel_workflow(id) 单独停止某个 workflow，
    而不必连带取消整个父 run。task 字段持有底层 asyncio.Task，cancel_workflow 通过它发起取消。
    """

    workflow_id: str
    parent_session_id: str
    mode: WorkflowMode
    started_at: str
    task: asyncio.Task[Any]


@dataclass(frozen=True)
class SubAgentReportProjection:
    """返回给父 agent 和 Web 视图使用的子 agent 报告摘要。"""

    display_order: int
    task_id: str
    task_name: str
    status: str
    summary: str
    error_message: str | None
    report_path: str
    working_dir: str | None
    session_id: str
    run_id: str
    reported_at: str
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentWorkflowResult:
    """返回给调用方的 workflow 最终结果。"""

    workflow_id: str
    mode: WorkflowMode
    parent_session_id: str
    workflow_dir: Path
    started_at: str
    finished_at: str
    runs: tuple[SubAgentRun, ...]
    reports: tuple[SubAgentReportProjection, ...]
    report_index_path: Path
    desc: str | None = None
    data: Mapping[str, object] | None = None
    completed_override: bool | None = None

    @property
    def completed(self) -> bool:
        """判断 workflow 是否全部完成，输入为当前 runs，输出为布尔完成状态。"""
        if self.completed_override is not None:
            return self.completed_override
        return all(run.status == "completed" for run in self.runs)


__all__ = [
    "ActiveWorkflowHandle",
    "AgentWorkflowResult",
    "SubAgentReportProjection",
    "WorkflowMode",
]
