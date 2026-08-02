"""工作流策略执行上下文与运行时能力协议。

本脚本定义策略运行时共享的上下文数据（WorkflowExecutionContext）以及具体 workflow 向引擎层借用的能力协议（WorkflowRuntime）。
作用：让具体 workflow 策略只依赖 Protocol，不依赖 AgentWorkflowManager 具体类，从而阻断 strategy → manager 的反向依赖。
关键执行流程：AgentWorkflowManager 构造 WorkflowExecutionContext 时把自身（实现 WorkflowRuntime）注入到 context.runtime；
策略在 run() 中通过 context.runtime.run_subagent_task(...) / write_workflow_manifest(...) 等方法编排子 agent 与写账本，
manager 负责 workflow 整体生命周期（started/completed/cancelled + 整体 manifest 状态机）。
关键符号：WorkflowExecutionContext 携带路径与注入能力；WorkflowRuntime 定义引擎层暴露给策略的能力契约。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class WorkflowRuntime(Protocol):
    """引擎层注入到策略的运行时能力协议。

    具体策略通过 WorkflowExecutionContext.runtime 借用引擎层能力，
    例如跑单个子 agent、写整体 manifest、写 result、同步任务进度。
    策略只依赖本 Protocol，禁止 import AgentWorkflowManager。
    """

    @property
    def workspace_root(self) -> Path:
        """返回 workflow 工作区根目录，供策略物化输入文件。"""
        ...

    def prepare_subagent_tasks(
        self,
        *,
        workflow_dir: Path,
        tasks: list[Any],
        parent_agent: Mapping[str, object] | None = None,
    ) -> list[Any]:
        """为策略任务绑定运行目录并解析 runtime，输出带 metadata 的任务列表。"""
        ...

    async def run_subagent_task(
        self,
        *,
        context: WorkflowExecutionContext,
        task: Any,
        display_order: int,
    ) -> Any:
        """运行单个子 agent 并收尾单任务账本（审计/进度/usage/result.json/报告）。"""
        ...

    def record_subagent_failure(
        self,
        *,
        context: WorkflowExecutionContext,
        task: Any,
        display_order: int,
        error: Exception,
        elapsed_ms: int,
    ) -> Any:
        """以失败收口策略层子任务（不真正跑子 agent，仅写失败账本）。"""
        ...

    def write_workflow_manifest(
        self,
        *,
        context: WorkflowExecutionContext,
        tasks: list[Any],
        status: str,
        finished_at: str | None = None,
    ) -> None:
        """写入整体 workflow 清单 workflow.json。"""
        ...

    def record_assigned_task_progress(
        self,
        *,
        context: WorkflowExecutionContext,
        tasks: list[Any],
    ) -> None:
        """登记 assigned 阶段任务进度快照（任务出现在进度列表）。"""
        ...

    def initialize_task_flow_progress(
        self,
        *,
        context: WorkflowExecutionContext,
        tasks: list[Any],
        title: str,
    ) -> None:
        """初始化 task-flow 的 LLM 控制进度快照。"""
        ...

    def write_report_index(
        self,
        *,
        context: WorkflowExecutionContext,
        status: str,
        reports: tuple[Any, ...],
    ) -> Path:
        """写入 reports/index.json，输出索引路径。"""
        ...

    def write_workflow_result(
        self,
        *,
        context: WorkflowExecutionContext,
        finished_at: str,
        completed: bool,
        report_index_path: Path,
        reports: tuple[Any, ...],
        runs: tuple[Any, ...],
        extra: Mapping[str, object] | None = None,
    ) -> None:
        """写入整体 result.json。"""
        ...

    def append_audit(
        self,
        *,
        context: WorkflowExecutionContext,
        action: str,
        payload: Mapping[str, object],
    ) -> None:
        """追加一条 workflow 级审计事件，引擎层自动补齐 resolved_runtime。"""
        ...


@dataclass(frozen=True)
class WorkflowExecutionContext:
    """单次 workflow 的路径、审计写入器、运行预算与注入的运行时能力。"""

    # 工作流唯一 ID。
    workflow_id: str
    # 父 session ID。
    parent_session_id: str
    # 当前策略 mode。
    mode: str
    # 工作流产物目录。
    workflow_dir: Path
    # 工作流开始时间，ISO 8601 字符串。
    started_at: str
    # LLM 填写的一句 workflow 短描述。
    desc: str | None
    # 工作流审计写入器。
    audit_writer: Any
    # 引擎层注入的运行时能力，策略通过它跑子 agent 与写整体账本，禁止直接引用 manager。
    runtime: WorkflowRuntime
    # 父 agent 当前 run 快照，由 Runner 写入 ToolContext metadata。
    parent_agent: Mapping[str, object] | None = None
    # 最大并发预算，None 表示沿用策略默认值。
    max_concurrency: int | None = None
    # 工作流超时预算，None 表示沿用运行期默认值。
    workflow_timeout_seconds: int | None = None


__all__ = ["WorkflowExecutionContext", "WorkflowRuntime"]
