"""共享宿主适配 API。

对外暴露：

- :class:`HostAdapter`：所有宿主适配器的基类。
- :class:`SessionBridge`：连接 adapter 和 runtime 的双向翻译层。
- :class:`MemoryRefreshSink`：订阅 ``history.compact`` 事件刷新 memory
  snapshot（方案 γ，不侵入 core / runner）。
- :class:`McpRuntimeRegistrationManager`：宿主共享的 MCP / Web Search 工具注册胶水。

依赖方向：``hosts/shared`` 消费 ``core / memory``，不反向依赖具体宿主。
"""

from __future__ import annotations

from hosts.shared.base import HostAdapter
from hosts.shared.mcp_runtime_registration import (
    McpRuntimeRegistrationManager,
    McpRuntimeRegistrationResult,
)
from hosts.shared.memory_refresh_sink import MemoryRefreshSink
from hosts.shared.session_bridge import SessionBridge

__all__ = [
    "HostAdapter",
    "MemoryRefreshSink",
    "McpRuntimeRegistrationManager",
    "McpRuntimeRegistrationResult",
    "SessionBridge",
]
