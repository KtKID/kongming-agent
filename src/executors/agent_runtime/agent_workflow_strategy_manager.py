"""智能体工作流策略注册管理器。

本脚本负责管理可运行策略和计划中策略的注册、查询、说明返回和运行分发。
作用是给 AgentWorkflowManager 提供统一的策略控制面，让父 agent 可以先查看策略目录，再按 mode 获取详情并执行。
关键执行流程：register/register_planned 写入策略表，list_strategies 输出目录，describe_strategy 返回中文详情，run_strategy 校验 mode 后创建上下文并调用策略。
关键函数：register 注册可运行策略，register_planned 注册只读说明，list_strategies 返回目录，describe_strategy 返回详情，run_strategy 分发执行。
"""

from __future__ import annotations

from collections.abc import Callable

from executors.agent_runtime.workflow_execution_context import WorkflowExecutionContext
from executors.agent_runtime.workflow_strategy import (
    WorkflowRunRequest,
    WorkflowStrategy,
    WorkflowStrategyNotFound,
    WorkflowStrategyNotRunnable,
)
from executors.agent_runtime.workflow_strategy_description import (
    WorkflowStrategyCatalogEntry,
    WorkflowStrategyDescription,
)

WorkflowContextFactory = Callable[[WorkflowRunRequest], WorkflowExecutionContext]


class AgentWorkflowStrategyManager:
    """管理策略注册、策略说明查询和策略执行分发。"""

    def __init__(self, *, context_factory: WorkflowContextFactory) -> None:
        """初始化策略表，输入为上下文工厂，输出为可注册和分发策略的管理器实例。"""
        self._context_factory = context_factory
        self._strategies: dict[str, WorkflowStrategy] = {}
        self._planned: dict[str, WorkflowStrategyDescription] = {}

    def register(self, strategy: WorkflowStrategy) -> None:
        """注册可运行策略，输入为策略对象，输出为写入后的运行策略表。"""
        mode = _normalize_mode(strategy.mode)
        if mode in self._strategies or mode in self._planned:
            raise ValueError(f"agent workflow strategy mode already registered: {mode}")
        self._strategies[mode] = strategy

    def register_planned(self, description: WorkflowStrategyDescription) -> None:
        """注册计划中策略，输入为只读策略说明，输出为写入后的计划策略表。"""
        mode = _normalize_mode(description.mode)
        if description.runnable:
            raise ValueError("planned strategy description must set runnable=False")
        if mode in self._strategies or mode in self._planned:
            raise ValueError(f"agent workflow strategy mode already registered: {mode}")
        self._planned[mode] = description

    def list_strategies(self) -> tuple[WorkflowStrategyCatalogEntry, ...]:
        """列出策略目录，输入为当前注册表，输出为可运行和计划中策略的紧凑条目。"""
        entries = [
            strategy.catalog_entry()
            for _, strategy in sorted(self._strategies.items(), key=lambda item: item[0])
        ]
        entries.extend(
            description.catalog_entry()
            for _, description in sorted(self._planned.items(), key=lambda item: item[0])
        )
        return tuple(entries)

    def describe_strategy(self, mode: str) -> WorkflowStrategyDescription:
        """查询策略详情，输入为 mode，输出为对应策略的中文详细说明。"""
        normalized = _normalize_mode(mode)
        strategy = self._strategies.get(normalized)
        if strategy is not None:
            return strategy.describe()
        planned = self._planned.get(normalized)
        if planned is not None:
            return planned
        raise self._not_found(normalized, operation="describe")

    async def run_strategy(self, request: WorkflowRunRequest) -> object:
        """执行策略请求，输入为 WorkflowRunRequest，输出为具体策略返回的运行结果。"""
        mode = _normalize_mode(request.mode)
        strategy = self._strategies.get(mode)
        if strategy is not None:
            context = self._context_factory(request)
            return await strategy.run(context, request.payload)
        planned = self._planned.get(mode)
        if planned is not None:
            raise WorkflowStrategyNotRunnable(
                mode=mode,
                status=planned.status,
                depends_on=planned.depends_on,
                runnable_modes=self._runnable_modes(),
            )
        raise self._not_found(mode, operation="run")

    def _not_found(self, mode: str, *, operation: str) -> WorkflowStrategyNotFound:
        """构造未知策略错误，输入为 mode 和操作名，输出为包含可用策略列表的异常。"""
        return WorkflowStrategyNotFound(
            mode=mode,
            available_modes=self._available_modes(),
            runnable_modes=self._runnable_modes(),
            operation=operation,
        )

    def _available_modes(self) -> tuple[str, ...]:
        """读取全部策略 mode，输入为当前注册表，输出为已排序的可查询策略 ID。"""
        return tuple(sorted({*self._strategies.keys(), *self._planned.keys()}))

    def _runnable_modes(self) -> tuple[str, ...]:
        """读取可运行策略 mode，输入为当前注册表，输出为已排序的可执行策略 ID。"""
        return tuple(sorted(self._strategies.keys()))


def _normalize_mode(mode: str) -> str:
    """规范化策略 mode，输入为原始字符串，输出为去空白后的非空策略 ID。"""
    normalized = mode.strip()
    if not normalized:
        raise ValueError("agent workflow strategy mode must be non-empty")
    return normalized


__all__ = ["AgentWorkflowStrategyManager", "WorkflowContextFactory"]
