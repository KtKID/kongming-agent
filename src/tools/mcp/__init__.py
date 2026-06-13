"""MCP tool adapter 公共入口。

本包负责把 MCP tools/list descriptor 转成 Kongming Tool，保持 MCP 生命周期
归属下层 manager，tool 层只处理命名、alias、schema 和 tools/call 转接。
"""

from __future__ import annotations

from tools.mcp.adapter import (
    McpToolAdapter,
    McpToolAdapterManager,
    McpToolAdapterPlan,
    McpToolAliasConfig,
    McpToolDescriptor,
    McpToolRegistration,
)

__all__ = [
    "McpToolAdapter",
    "McpToolAdapterManager",
    "McpToolAdapterPlan",
    "McpToolAliasConfig",
    "McpToolDescriptor",
    "McpToolRegistration",
]
