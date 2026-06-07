"""工作流策略说明数据结构。

本脚本定义提供给父 agent/LLM 查看和选择的策略目录、详细说明和输入字段模型。
作用是把 runtime 内部策略转换成稳定、可序列化、中文可读的说明对象。
关键执行流程：策略实现生成详细说明，调用 catalog_entry 生成目录摘要，父 agent 先看目录再请求某个策略详情。
关键函数：WorkflowStrategyDescription.catalog_entry 负责从详细说明生成紧凑目录项。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

WorkflowStrategyStatus = Literal["available", "planned", "experimental"]


@dataclass(frozen=True)
class WorkflowStrategyCatalogEntry:
    """父 agent 可查看的中文策略目录项。"""

    # 稳定策略 ID，例如 parallel、map_reduce。
    mode: str
    # 中文策略名。
    title: str
    # 中文一句话摘要。
    summary: str
    # 当前状态：available、planned、experimental。
    status: WorkflowStrategyStatus
    # 是否可以直接 run。
    runnable: bool


@dataclass(frozen=True)
class WorkflowStrategyInputField:
    """策略 payload 中单个输入字段的中文说明。"""

    # JSON 字段名。
    name: str
    # 是否必填。
    required: bool
    # 字段类型描述。
    type_label: str
    # 中文说明。
    description: str
    # 示例值。
    example: Any | None = None


@dataclass(frozen=True)
class WorkflowStrategyDescription:
    """父 agent 生成 payload 前查看的中文策略详情。"""

    # 稳定策略 ID。
    mode: str
    # 中文策略名。
    title: str
    # 当前状态：available、planned、experimental。
    status: WorkflowStrategyStatus
    # 是否可以直接 run。
    runnable: bool
    # 中文策略摘要。
    summary: str
    # 适合使用该策略的场景。
    when_to_use: tuple[str, ...]
    # 不适合该策略的场景或风险提示。
    warnings: tuple[str, ...]
    # 输入字段说明，字段名保持英文，说明使用中文。
    inputs: tuple[WorkflowStrategyInputField, ...]
    # 输出说明。
    outputs: tuple[str, ...]
    # 示例 payload。
    examples: tuple[dict[str, object], ...]
    # 依赖任务或依赖能力。
    depends_on: tuple[str, ...] = ()

    def catalog_entry(self) -> WorkflowStrategyCatalogEntry:
        """生成紧凑目录项，输入为当前详细说明对象，输出为可列表展示的策略摘要。"""
        return WorkflowStrategyCatalogEntry(
            mode=self.mode,
            title=self.title,
            summary=self.summary,
            status=self.status,
            runnable=self.runnable,
        )


__all__ = [
    "WorkflowStrategyCatalogEntry",
    "WorkflowStrategyDescription",
    "WorkflowStrategyInputField",
    "WorkflowStrategyStatus",
]
