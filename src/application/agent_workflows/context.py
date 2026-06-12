"""工作流策略执行上下文。

本脚本定义策略运行时共享的上下文数据，包括 workflow ID、父会话、产物目录、审计写入器和预算信息。
作用是让不同策略在统一上下文内创建审计文件、关联父会话并读取执行预算。
关键执行流程：AgentWorkflowManager 创建 WorkflowExecutionContext，策略读取其中的目录和审计入口，随后把具体任务交给运行组件执行。
关键函数：本脚本只提供 WorkflowExecutionContext 数据结构，无独立函数。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WorkflowExecutionContext:
    """单次 workflow 的路径、审计写入器和运行预算。"""

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
    # 最大并发预算，None 表示沿用策略默认值。
    max_concurrency: int | None = None
    # 工作流超时预算，None 表示沿用运行期默认值。
    workflow_timeout_seconds: int | None = None


__all__ = ["WorkflowExecutionContext"]
