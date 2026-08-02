"""工作流策略运行协议。

本脚本定义策略注册与执行所需的请求模型、异常模型和策略协议。
作用是让 AgentWorkflowStrategyManager 可以用统一接口描述策略、查找策略并执行策略。
关键执行流程：调用方构造 WorkflowRunRequest，注册管理器按 mode 查找 WorkflowStrategy，策略用 payload 和执行上下文产出结果。
关键函数：WorkflowStrategy.catalog_entry 提供目录摘要，WorkflowStrategy.describe 提供中文详情，WorkflowStrategy.run 执行策略。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from application.agent_workflows.context import WorkflowExecutionContext
from application.agent_workflows.strategies.description import (
    WorkflowStrategyCatalogEntry,
    WorkflowStrategyDescription,
)


@dataclass(frozen=True)
class WorkflowRunRequest:
    """单次策略执行请求，携带目标策略、父会话和策略 payload。"""

    # 目标策略。
    mode: str
    # 父 session ID。
    parent_session_id: str
    # 策略 payload，由对应 strategy 校验。
    payload: Mapping[str, object]
    # 调用来源，例如 tool、cli-smoke、internal-test。
    source: str
    # LLM 填写的一句 workflow 短描述，用于运行产物和 Viewer 展示。
    desc: str | None = None
    # 父 agent 当前 run 快照，由 tool context 注入，供子 agent runtime fallback 使用。
    parent_agent: Mapping[str, object] | None = None


@dataclass(frozen=True)
class WorkflowStrategyNotFound(ValueError):
    """策略 mode 未注册时抛出的错误。"""

    # 请求的策略 ID。
    mode: str
    # 已注册策略，包括 planned 条目。
    available_modes: tuple[str, ...]
    # 可以直接运行的策略。
    runnable_modes: tuple[str, ...]
    # 当前操作：list、describe、run。
    operation: str

    def __str__(self) -> str:
        """格式化未知策略错误，输入为异常字段，输出为包含可用策略的错误文本。"""
        return (
            f"unknown agent workflow strategy mode {self.mode!r}; "
            f"available_modes={list(self.available_modes)!r}; "
            f"runnable_modes={list(self.runnable_modes)!r}; operation={self.operation}"
        )


@dataclass(frozen=True)
class WorkflowStrategyNotRunnable(ValueError):
    """策略已登记但当前只能查看说明时抛出的错误。"""

    # 请求的策略 ID。
    mode: str
    # 当前状态，例如 planned。
    status: str
    # 依赖任务或依赖能力。
    depends_on: tuple[str, ...]
    # 可以直接运行的策略。
    runnable_modes: tuple[str, ...]

    def __str__(self) -> str:
        """格式化不可运行策略错误，输入为异常字段，输出为包含依赖信息的错误文本。"""
        return (
            f"agent workflow strategy {self.mode!r} is not runnable; "
            f"status={self.status!r}; depends_on={list(self.depends_on)!r}; "
            f"runnable_modes={list(self.runnable_modes)!r}"
        )


class WorkflowStrategy(Protocol):
    """运行期策略协议，约束策略说明、目录摘要和执行入口。"""

    mode: str

    def catalog_entry(self) -> WorkflowStrategyCatalogEntry:
        """返回目录摘要，输入为策略自身状态，输出为父 agent 可查看的紧凑条目。"""
        ...

    def describe(self) -> WorkflowStrategyDescription:
        """返回中文策略详情，输入为策略自身状态，输出为 payload 生成说明。"""
        ...

    async def run(
        self,
        context: WorkflowExecutionContext,
        payload: Mapping[str, object],
    ) -> Any:
        """执行策略，输入为执行上下文和 payload，输出为策略运行结果。"""
        ...


__all__ = [
    "WorkflowRunRequest",
    "WorkflowStrategy",
    "WorkflowStrategyNotFound",
    "WorkflowStrategyNotRunnable",
]
