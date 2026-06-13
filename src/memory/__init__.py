"""kongming-agent 长期记忆层。

``memory/`` 负责跨会话的长期记忆读写与 prompt 注入：

- :mod:`memory.store` 提供 :class:`MemoryStore`（多文件读取 + 冻结快照 + 活态条目）
  以及核心数据结构：:class:`MemorySnapshot` / :class:`MemoryEntry` /
  :class:`MemoryWriteAction` / :class:`MemoryWriteResult`
- :mod:`memory.safety_write` 提供安全写入内核（内容扫描 + 原子写入 + 去重）

严格约束：

- ``memory/`` 只消费 ``core`` 协议，不 import 任何 sibling 模块
  （tools / host / cli / safety / executors / infrastructure.tracing / sessions / prompting / infrastructure.config）
- 调用方（NativeRuntime / CLI）通过 import ``memory`` 注入 prompt source
"""

from __future__ import annotations

from memory.safety_write import execute_write, scan_content
from memory.store import (
    ENTRY_DELIMITER,
    MEMORY_MAX_CHARS,
    USER_MAX_CHARS,
    MemoryEntry,
    MemorySnapshot,
    MemoryStore,
    MemoryTarget,
    MemoryWriteAction,
    MemoryWriteResult,
)

__all__ = [
    "ENTRY_DELIMITER",
    "MEMORY_MAX_CHARS",
    "MemoryEntry",
    "MemorySnapshot",
    "MemoryStore",
    "MemoryTarget",
    "MemoryWriteAction",
    "MemoryWriteResult",
    "USER_MAX_CHARS",
    "execute_write",
    "scan_content",
]
