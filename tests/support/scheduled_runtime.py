"""scheduled execution bridge 的 SessionEngine 测试替身。

功能：让 bridge 单元测试继续注入可观测 Runner，同时强制生产调用形态经过
``runtime.run(..., execution_overrides=...)``。本替身只展开显式覆盖合同并把参数
交给测试 Runner，不提供额外调度或 Task 所有权。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from application.scheduled_runs.execution_bridge import ExecutionBridge
from core.agent_spec import AgentSpec
from core.contracts import ApprovalProvider, RunExecutionOverrides, Tool
from core.result import Result
from core.runner import Runner
from scheduler.domain import DueTaskReservation, ScheduledRun


class RunnerBackedScheduledRuntime:
    """把 RunExecutionOverrides 展开给测试 Runner 的最小 runtime。"""

    def __init__(self, runner: Runner, *, approval: ApprovalProvider) -> None:
        """保存测试 Runner；输入为单个 Runner，输出为 runtime 替身。"""
        self._runner = runner
        self._approval = approval

    async def run(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        agent_spec: AgentSpec | None = None,
        max_turns: int | None = None,
        enabled_tools: Sequence[Tool] | None = None,
        event_context: dict[str, Any] | None = None,
        thread_id: str | None = None,
        agent_id: str = "",
        execution_overrides: RunExecutionOverrides | None = None,
    ) -> Result:
        """校验覆盖快照并直接 await 测试 Runner。"""
        del session_id, event_context, thread_id, agent_id
        if agent_spec is None:
            raise ValueError("agent_spec is required")
        if execution_overrides is None:
            raise ValueError("execution_overrides is required")
        if execution_overrides.session is None:
            raise ValueError("execution_overrides.session is required")
        if execution_overrides.llm is None:
            raise ValueError("execution_overrides.llm is required")
        if execution_overrides.tools is None:
            raise ValueError("execution_overrides.tools is required")
        approval = self._approval
        if execution_overrides.approval_transform is not None:
            approval = execution_overrides.approval_transform(approval)
        return await self._runner.run(
            user_input,
            session=execution_overrides.session,
            agent_spec=agent_spec,
            llm=execution_overrides.llm,
            tools=execution_overrides.tools,
            approval=approval,
            max_turns=max_turns,
            run_id=execution_overrides.run_id,
            enabled_tools=enabled_tools,
            event_sinks=execution_overrides.event_sinks,
            tool_context_metadata=execution_overrides.tool_context_metadata,
        )


async def execute_bridge_for_test(
    bridge: ExecutionBridge,
    reservation: DueTaskReservation,
) -> ScheduledRun:
    """用显式业务 ID 调用 bridge；仅服务 bridge 单元测试与 fixture 播种。"""
    suffix = uuid.uuid4().hex[:12]
    return await bridge.execute_admitted(
        reservation,
        run_id=f"run-sched-{reservation.task.task_id}-{suffix}",
        session_id=f"sched-{reservation.task.task_id}-{suffix}",
    )


__all__ = ["RunnerBackedScheduledRuntime", "execute_bridge_for_test"]
