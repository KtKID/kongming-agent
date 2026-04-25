"""Host 模块公共 API。

对外暴露：

- :class:`HostAdapter`：所有宿主适配器的基类。
- :class:`SessionBridge`：连接 adapter 和 runtime 的双向翻译层。
- :class:`CLIAdapter` / :class:`CLIEventSink`：第一版 CLI 宿主实现。
- :class:`MemoryRefreshSink`：订阅 ``history.compact`` 事件刷新 memory
  snapshot（方案 γ，不侵入 core / runner）。

依赖方向：``host/`` 消费 ``core / executors / tools / memory``，
不反向依赖 ``cli/``。
"""

from __future__ import annotations

from host.base import HostAdapter
from host.cli_adapter import CLIAdapter, CLIEventSink
from host.memory_refresh_sink import MemoryRefreshSink
from host.session_bridge import SessionBridge

__all__ = [
    "CLIAdapter",
    "CLIEventSink",
    "HostAdapter",
    "MemoryRefreshSink",
    "SessionBridge",
]
