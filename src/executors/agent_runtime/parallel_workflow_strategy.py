"""并行工作流策略实现。

本脚本把已存在的并行子 agent 编排能力包装成可注册、可描述、可执行的 workflow strategy。
作用是向父 agent 暴露“并行子任务”策略的中文说明，并在执行时把 task_specs 转交给 AgentWorkflowManager.run_parallel_specs。
关键执行流程：catalog_entry 返回目录项，describe 返回中文策略详情，run 校验 payload 中的 task_specs/tasks 后触发并行执行。
关键函数：catalog_entry 提供策略目录，describe 提供中文说明，run 执行并行策略。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from executors.agent_runtime.workflow_execution_context import WorkflowExecutionContext
from executors.agent_runtime.workflow_strategy_description import (
    WorkflowStrategyCatalogEntry,
    WorkflowStrategyDescription,
    WorkflowStrategyInputField,
)


class ParallelWorkflowStrategy:
    """通过 SubAgentManager 执行互不依赖的子 agent fan-out/fan-in 任务。"""

    mode = "parallel"

    def __init__(self, manager: Any) -> None:
        """初始化策略，输入为 AgentWorkflowManager，输出为可调用并行编排的策略实例。"""
        self._manager = manager

    def catalog_entry(self) -> WorkflowStrategyCatalogEntry:
        """生成策略目录项，输入为当前策略说明，输出为父 agent 可查看的紧凑条目。"""
        return self.describe().catalog_entry()

    def describe(self) -> WorkflowStrategyDescription:
        """生成中文策略说明，输入为当前策略配置，输出为 LLM 选择和生成 payload 所需的详情。"""
        return WorkflowStrategyDescription(
            mode=self.mode,
            title="并行子任务",
            status="available",
            runnable=True,
            summary="把多个互不依赖的子任务同时派发给子 agent，等待全部返回后汇总报告。",
            when_to_use=(
                "任务可以拆成多个互不依赖的子任务",
                "每个子任务只需要自己的 prompt、上下文和授权工具",
                "结果可以按子任务报告直接汇总",
            ),
            warnings=(
                "子任务之间需要实时协商时应使用 supervisor-worker",
                "存在强依赖顺序时应使用 pipeline/DAG",
                "大规模同构文件分析更适合 map_reduce",
            ),
            inputs=(
                WorkflowStrategyInputField(
                    name="task_specs",
                    required=True,
                    type_label="array<object>",
                    description="并行子任务列表，兼容 run_parallel_subagents 的 tasks 结构。",
                    example=[
                        {
                            "task_name": "审查 API",
                            "prompt": "检查 API 兼容性",
                            "permission": {"mode": "scoped_workdir"},
                        }
                    ],
                ),
            ),
            outputs=(
                "AgentWorkflowResult",
                "reports/index.json",
                "每个子任务的报告 JSON",
            ),
            examples=(
                {
                    "mode": "parallel",
                    "payload": {
                        "task_specs": [
                            {
                                "task_name": "review-a",
                                "prompt": "检查 A 模块",
                                "permission": {"mode": "scoped_workdir"},
                            },
                            {
                                "task_name": "review-b",
                                "prompt": "检查 B 模块",
                                "permission": {"mode": "scoped_workdir"},
                            },
                        ]
                    },
                },
            ),
        )

    async def run(self, context: WorkflowExecutionContext, payload: Mapping[str, object]) -> Any:
        """执行并行策略，输入为 workflow 上下文和 payload，输出为 AgentWorkflowResult。"""
        raw_task_specs = payload.get("task_specs", payload.get("tasks"))
        if not isinstance(raw_task_specs, list) or not raw_task_specs:
            raise ValueError("parallel strategy requires non-empty task_specs")
        return await self._manager.run_parallel_specs(
            parent_session_id=context.parent_session_id,
            task_specs=raw_task_specs,
        )


__all__ = ["ParallelWorkflowStrategy"]
