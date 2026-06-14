"""MCP 基础设施数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class McpToolDescriptor:
    """MCP tools/list 返回的工具描述。"""

    server_id: str
    name: str
    title: str | None
    description: str
    input_schema: dict[str, Any]
    raw_descriptor: dict[str, Any]


@dataclass(frozen=True)
class McpCallResult:
    """MCP tools/call 的归一化结果。"""

    ok: bool
    content_text: str
    data: dict[str, Any]
    error_message: str | None
    diagnostics: dict[str, Any]
