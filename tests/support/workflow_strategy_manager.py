"""Workflow 策略测试用 manager。

本模块只隔离策略算法的 child 执行边界，让 map-reduce、roundtable 和 research
测试使用确定性输出；AgentManager/TaskRegistry 的真实跨层链由 workflow smoke
与 manager 集成测试覆盖。
关键执行流程：生产 AgentWorkflowManager 负责策略、产物和审计，测试 executor
只返回单个 SubAgentRun。
关键类：WorkflowStrategyTestManager 绑定 executor 并覆盖 child 执行边界。
"""

from __future__ import annotations

from typing import Any, Protocol

from application.agent_workflows.manager import AgentWorkflowManager
from application.agent_workflows.task_models import SubAgentRun, SubAgentTask
from application.subagents.permissions import WorkflowAuditWriter


class WorkflowTaskTestExecutor(Protocol):
    """策略测试 child executor 合同。"""

    async def execute_task(
        self,
        *,
        workflow_id: str,
        parent_session_id: str,
        task: SubAgentTask,
        audit_writer: WorkflowAuditWriter,
    ) -> SubAgentRun:
        """执行一个确定性策略任务，返回 SubAgentRun。"""
        ...


class _StrategyAgentManager:
    """满足 workflow 启动前 owner 校验的最小测试 AgentManager。"""

    def get_agent(self, agent_id: str) -> object | None:
        """返回测试 root，输入为 agent id，输出为 root 或 None。"""
        return object() if agent_id == "strategy-test-root" else None


class WorkflowStrategyTestManager(AgentWorkflowManager):
    """保留生产策略/产物链，只替换 child 执行的测试 manager。"""

    def __init__(
        self,
        *,
        task_executor: WorkflowTaskTestExecutor | None = None,
        **kwargs: Any,
    ) -> None:
        """初始化策略测试 manager，输入为 executor 和生产参数，输出可运行 facade。"""
        super().__init__(
            agent_manager=_StrategyAgentManager(),
            **kwargs,
        )
        self._task_executor = task_executor

    async def run_workflow_payload(
        self,
        *,
        parent_agent: dict[str, object] | None = None,
        **kwargs: Any,
    ) -> Any:
        """补入测试 root identity，输入为 workflow payload，输出生产 workflow 结果。"""
        resolved_parent = parent_agent
        if not isinstance(parent_agent, dict) or not str(parent_agent.get("agent_id", "")).strip():
            resolved_parent = {"agent_id": "strategy-test-root"}
        return await super().run_workflow_payload(
            parent_agent=resolved_parent,
            **kwargs,
        )

    async def _spawn_and_wait_workflow_task(
        self,
        *,
        workflow_id: str,
        parent_session_id: str,
        workflow_dir: Any,
        task: SubAgentTask,
        parent_agent: Any,
        audit_writer: WorkflowAuditWriter,
    ) -> SubAgentRun:
        """委托测试 executor，输入为生产任务上下文，输出为确定性 SubAgentRun。"""
        del workflow_dir, parent_agent
        if self._task_executor is None:
            raise AssertionError("strategy test unexpectedly spawned a workflow child")
        return await self._task_executor.execute_task(
            workflow_id=workflow_id,
            parent_session_id=parent_session_id,
            task=task,
            audit_writer=audit_writer,
        )


__all__ = ["WorkflowStrategyTestManager", "WorkflowTaskTestExecutor"]
