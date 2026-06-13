"""MiniMax MCP Web Search live smoke 测试。

本脚本验证真实 MiniMax MCP web_search 能通过运行时注册链路输出标准 URL。
作用是覆盖 Config -> McpRuntimeRegistrationManager -> MCP Tool adapter ->
WebSearchManager -> web_search Tool 的最小真实链路。
关键函数：
- test_minimax_mcp_web_search_returns_urls：启动真实 MCP server，调用 web_search，
  断言至少返回一条带 URL 的结果。
- _redacted_diagnostics：生成失败输出用的脱敏诊断。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from core.contracts import ToolContext, ToolResult
from hosts.shared.mcp_runtime_registration import McpRuntimeRegistrationManager
from infrastructure.config import load_config
from tools.runtime.registry import ToolRegistry

pytestmark = [pytest.mark.e2e, pytest.mark.live]

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_minimax_mcp_web_search_returns_urls() -> None:
    """启动真实 MiniMax MCP 并调用 web_search，输入查询文本，输出至少一条 URL。"""
    if os.environ.get("KONGMING_RUN_LIVE_MCP", "").strip() != "1":
        pytest.skip("set KONGMING_RUN_LIVE_MCP=1 to run live MiniMax MCP web_search smoke")

    cfg = load_config(_REPO_ROOT / "config" / "setting.yaml")
    if not os.environ.get("MINIMAX_API_KEY", "").strip():
        pytest.skip("MINIMAX_API_KEY is not configured")

    registry = ToolRegistry()
    manager = McpRuntimeRegistrationManager(cfg)
    try:
        registration = await manager.register(registry)
        web_search = registry.get("web_search")
        assert web_search is not None, _json_dump(
            {
                "registration": _redacted_diagnostics(registration.diagnostics),
                "tools": registry.names(),
            }
        )
        provider_tool = registry.get("mcp__minimax__web_search")
        assert provider_tool is not None, _json_dump(
            {
                "registration": _redacted_diagnostics(registration.diagnostics),
                "tools": registry.names(),
            }
        )

        query = "MiniMax Web Search MCP web_search"
        provider_result = await provider_tool.execute(
            {"query": query, "max_results": 3},
            ToolContext(
                run_id="test-minimax-mcp-provider-search",
                session_id="test-minimax-mcp-web-search",
                turn=1,
                call_id="provider-web-search-live-smoke",
                metadata={"origin": "tests.e2e"},
            ),
        )

        result = await web_search.execute(
            {
                "query": query,
                "max_results": 3,
            },
            ToolContext(
                run_id="test-minimax-mcp-web-search",
                session_id="test-minimax-mcp-web-search",
                turn=1,
                call_id="web-search-live-smoke",
                metadata={"origin": "tests.e2e"},
            ),
        )

        assert isinstance(result, ToolResult)
        data = result.data or {}
        diagnostics = _redacted_diagnostics(data.get("diagnostics"))
        assert result.ok, _json_dump(
            {
                "error_message": result.error_message,
                "content": result.content[:1000],
                "diagnostics": diagnostics,
                "provider": _tool_result_preview(provider_result),
            }
        )
        results = data.get("results")
        assert isinstance(results, list), _json_dump(
            {
                "content": result.content[:1000],
                "diagnostics": diagnostics,
                "data": data,
                "provider": _tool_result_preview(provider_result),
            }
        )
        assert any(isinstance(item, dict) and item.get("url") for item in results), _json_dump(
            {
                "content": result.content[:1000],
                "diagnostics": diagnostics,
                "results": results,
                "provider": _tool_result_preview(provider_result),
            }
        )
    finally:
        await manager.aclose()


def _redacted_diagnostics(value: object) -> object:
    """脱敏诊断结构，输入任意诊断，输出隐藏 secret/key/token/password 字段后的副本。"""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(token in lowered for token in ("secret", "key", "token", "password")):
                redacted[str(key)] = "<redacted>"
            else:
                redacted[str(key)] = _redacted_diagnostics(item)
        return redacted
    if isinstance(value, list):
        return [_redacted_diagnostics(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redacted_diagnostics(item) for item in value)
    return value


def _tool_result_preview(value: object) -> dict[str, object]:
    """生成 ToolResult 预览，输入工具返回值，输出脱敏后的 content/data/错误。"""
    if isinstance(value, ToolResult):
        return {
            "ok": value.ok,
            "content": value.content[:2000],
            "data": _redacted_diagnostics(value.data),
            "error_message": value.error_message,
        }
    return {"value": _redacted_diagnostics(value)}


def _json_dump(value: object) -> str:
    """格式化失败详情，输入任意 JSON-like 值，输出 UTF-8 文本。"""
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
