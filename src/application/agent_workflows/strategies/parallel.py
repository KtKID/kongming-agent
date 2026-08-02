"""并行工作流策略实现。

本脚本是 self-contained 的并行子 agent 编排策略：校验 payload 中的 task_specs，
把它们转成 SubAgentTask，通过注入到 context 的 WorkflowRuntime 并发 fan-out 子 agent，
并在 strategy 内完成 manifest/审计/result 的收口。
作用是让并行编排逻辑只依赖 WorkflowRuntime Protocol，不引用 AgentWorkflowManager，
从而阻断 strategy → manager 的反向依赖。
关键执行流程：catalog_entry/describe 提供目录与中文说明；run 校验 task_specs，
经 prepare_subagent_tasks 绑定目录后并发 gather 单子任务，最后写 manifest/result/report_index 收口。
关键函数：catalog_entry 提供目录项，describe 提供中文说明，run 执行并行编排并收口。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from application.agent_workflows.context import WorkflowExecutionContext
from application.agent_workflows.strategies.description import (
    WorkflowStrategyCatalogEntry,
    WorkflowStrategyDescription,
    WorkflowStrategyInputField,
)
from application.agent_workflows.task_models import SubAgentTask
from application.subagents.permissions import (
    parse_permission_spec,
    validate_scoped_tool_names,
)


def _optional_string(value: object) -> str | None:
    """把任意值规范化为可选字符串，输入为原始值，输出为非空字符串或 None。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


class ParallelWorkflowStrategy:
    """通过注入的 WorkflowRuntime 执行互不依赖的子 agent fan-out/fan-in 任务。

    本策略 self-contained：不持有也不引用 AgentWorkflowManager，
    所需能力（跑子 agent、写 manifest/审计/result、同步进度）全部经
    WorkflowExecutionContext.runtime（WorkflowRuntime 协议）借用。
    """

    mode = "parallel"

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
                        ],
                    },
                },
            ),
        )

    @staticmethod
    def _parse_task_specs(raw_task_specs: list[object]) -> list[SubAgentTask]:
        """校验并转换 task_specs，输入为原始 payload 列表，输出为 SubAgentTask 列表。

        覆盖空列表、超过 8 个、非 object、缺字段、非法 context 的边界，沿用历史校验语义。
        """
        if not raw_task_specs:
            raise ValueError("parallel workflow requires non-empty task_specs")
        if len(raw_task_specs) > 8:
            raise ValueError("parallel workflow supports at most 8 task specs")
        tasks: list[SubAgentTask] = []
        for index, spec in enumerate(raw_task_specs, 1):
            if not isinstance(spec, dict):
                raise ValueError(f"task_specs[{index}] must be an object")
            task_name = spec.get("task_name")
            if not isinstance(task_name, str) or not task_name.strip():
                raise ValueError(f"task_specs[{index}].task_name must be a non-empty string")
            prompt = spec.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError(f"task_specs[{index}].prompt must be a non-empty string")
            context = spec.get("context", "")
            if context is None:
                context = ""
            if not isinstance(context, str):
                raise ValueError(f"task_specs[{index}].context must be a string")
            has_tool_names = "tool_names" in spec
            raw_tool_names = spec.get("tool_names", [])
            if not isinstance(raw_tool_names, list | tuple):
                raw_tool_names = []
            raw_skill_names = spec.get("skill_names", [])
            if not isinstance(raw_skill_names, list | tuple):
                raw_skill_names = []
            permission = None
            if "permission" in spec:
                permission = parse_permission_spec(spec["permission"])
                validate_scoped_tool_names(tuple(str(name) for name in raw_tool_names))
            # task_id 优先透传调用方提供的值；未提供时回退到稳定序号，保持向后兼容。
            raw_task_id = spec.get("task_id")
            task_id = (
                str(raw_task_id).strip()
                if isinstance(raw_task_id, str) and raw_task_id.strip()
                else f"agent-{index}"
            )
            tasks.append(
                SubAgentTask(
                    task_id=task_id,
                    task_name=task_name.strip(),
                    prompt=prompt.strip(),
                    context=context.strip(),
                    tool_names=tuple(str(name) for name in raw_tool_names),
                    requested_tool_names=(
                        tuple(str(name) for name in raw_tool_names) if has_tool_names else None
                    ),
                    skill_names=tuple(str(name) for name in raw_skill_names),
                    agent_role_id=_optional_string(spec.get("agent_role_id")),
                    permission=permission,
                )
            )
        return tasks

    async def run(self, context: WorkflowExecutionContext, payload: Mapping[str, object]) -> Any:
        """执行并行策略，输入为 workflow 上下文和 payload，输出为 AgentWorkflowResult。

        所有引擎层能力（manifest/审计/result/进度/跑子 agent）经 context.runtime 借用，
        本方法只负责并行编排控制流和收口审计事件，不引用 AgentWorkflowManager。
        """
        raw_task_specs = payload.get("task_specs", payload.get("tasks"))
        if not isinstance(raw_task_specs, list):
            raise ValueError("parallel strategy requires non-empty task_specs")
        tasks = self._parse_task_specs(raw_task_specs)
        # 局部引用，避免 mypy 把 Protocol 当未使用 import
        runtime = context.runtime

        assigned_tasks = runtime.prepare_subagent_tasks(
            workflow_dir=context.workflow_dir,
            tasks=tasks,
            parent_agent=context.parent_agent,
        )

        runtime.write_workflow_manifest(context=context, tasks=assigned_tasks, status="running")
        runtime.append_audit(
            context=context,
            action="workflow_started",
            payload={
                "workflow_id": context.workflow_id,
                "mode": self.mode,
                "parent_session_id": context.parent_session_id,
                "desc": context.desc,
                "task_count": len(assigned_tasks),
            },
        )
        runtime.record_assigned_task_progress(context=context, tasks=assigned_tasks)
        for task in assigned_tasks:
            runtime.append_audit(
                context=context,
                action="agent_assigned",
                payload={"task_id": task.task_id, "task_name": task.task_name},
            )

        outcomes = await asyncio.gather(
            *[
                runtime.run_subagent_task(
                    context=context,
                    task=task,
                    display_order=index,
                )
                for index, task in enumerate(assigned_tasks, 1)
            ]
        )
        runs = tuple(outcome.run for outcome in outcomes)
        reports = tuple(outcome.report for outcome in outcomes)
        completed = all(run.status == "completed" for run in runs)
        finished_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        report_index_path = runtime.write_report_index(
            context=context,
            status="completed" if completed else "failed",
            reports=reports,
        )
        runtime.write_workflow_result(
            context=context,
            finished_at=finished_at,
            completed=completed,
            report_index_path=report_index_path,
            reports=reports,
            runs=runs,
        )
        runtime.write_workflow_manifest(
            context=context,
            tasks=assigned_tasks,
            status="completed" if completed else "failed",
            finished_at=finished_at,
        )
        runtime.append_audit(
            context=context,
            action="workflow_completed",
            payload={
                "workflow_id": context.workflow_id,
                "completed": completed,
                "finished_at": finished_at,
                "run_count": len(runs),
                "report_index_path": str(report_index_path),
            },
        )

        # 延迟导入打破符号循环：models 不依赖本模块
        from application.agent_workflows.models import AgentWorkflowResult

        return AgentWorkflowResult(
            workflow_id=context.workflow_id,
            mode=self.mode,
            parent_session_id=context.parent_session_id,
            workflow_dir=context.workflow_dir,
            started_at=context.started_at,
            finished_at=finished_at,
            runs=runs,
            reports=reports,
            report_index_path=report_index_path,
            desc=context.desc,
            completed_override=completed,
        )


__all__ = ["ParallelWorkflowStrategy"]
