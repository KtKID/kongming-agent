"""kongming-agent 会话上下文工程层。

``context/`` 负责当前 run 可见的会话上下文，不承担长期 memory。
它是 :class:`core.session.InMemorySession` 的工程化升级侧：

- :mod:`context.session_store` 提供 :class:`SQLiteSession` 以及按配置装配的工厂
- :mod:`context.history_compactor` 实现最小历史压缩策略
- :mod:`context.instruction_loader` 从 agent_spec / 文件 / 环境变量加载规则
- :mod:`context.input_assembler` 把 history + 指令组装成模型真正看到的输入
- :mod:`context.prompts_loader` 从 ``.kongming/prompts/{AGENT,TOOLS,USER}.md`` 装配三段 system prompt 基础文本（v0.1.3）

严格约束（对齐 ``docs/kongming-agent-v1-minimal/10-contracts.md`` "Session 边界"）：

- :class:`core.contracts.Session` Protocol 是真源，``context/`` 禁止重新定义
- ``context/`` 只消费 ``core`` 暴露的协议和模型，不 import 任何 sibling 模块
  （tools / host / cli / safety / executors / observability）
"""

from __future__ import annotations

from context.file_session import FileSession
from context.history_compactor import CompactorConfig, HistoryCompactor
from context.input_assembler import AssembledInput, InputAssembler
from context.instruction_loader import InstructionLoader, InstructionSource
from context.prompts_loader import TEMPLATE_FILENAMES, materialize_and_load_prompts
from context.session_bootstrap import SessionBootstrap
from context.session_store import SQLiteSession, build_session

__all__ = [
    "AssembledInput",
    "CompactorConfig",
    "FileSession",
    "HistoryCompactor",
    "InputAssembler",
    "InstructionLoader",
    "InstructionSource",
    "SessionBootstrap",
    "SQLiteSession",
    "TEMPLATE_FILENAMES",
    "build_session",
    "materialize_and_load_prompts",
]
