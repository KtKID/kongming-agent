"""MCP runtime 注册单元测试。

本脚本验证配置到 ToolRegistry 的完整装配链路。
关键执行流程：用 fake MCP stdio server 启动 McpRuntimeRegistrationManager，
注册 canonical MCP tool 和通用 web_search，再执行 web_search 工具并关闭子进程。
关键函数：
- _write_config：写入最小配置文件。
- test_runtime_registration_registers_fake_mcp_web_search：验证成功注册和调用。
- test_runtime_registration_wraps_user_search_tool_without_mcp_servers：验证用户搜索工具注入。
- test_runtime_registration_keeps_baseline_when_mcp_command_missing：验证失败诊断。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from core.contracts import ToolContext, ToolResult
from hosts.shared.mcp_runtime_registration import McpRuntimeRegistrationManager
from infrastructure.config import load_config
from tools import ToolRegistry


def _write_config(tmp_path: Path, body: str) -> Path:
    """写入测试配置，输入临时目录和 YAML 正文，输出配置路径。"""
    config_path = tmp_path / "setting.yaml"
    config_path.write_text(
        "\n".join(
            [
                "model:",
                "  name: stub-model",
                "  base_url: http://127.0.0.1:1234/v1",
                "  api_key: ''",
                body.rstrip(),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def _fake_mcp_server_path() -> str:
    """返回 fake MCP server 脚本绝对路径。"""
    return str(Path(__file__).parents[1] / "fixtures" / "mcp" / "fake_mcp_server.py")


class _FakeUserSearchTool:
    """用户注入的搜索工具 fake，输入 query，输出 URL 搜索结果。"""

    name = "custom_search_tool"
    description = "Fake user-provided search tool."
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    async def execute(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        """执行 fake 搜索，输入 tool 参数和上下文，输出 ToolResult。"""
        query = str(args["query"])
        return ToolResult(
            ok=True,
            content=f"custom result for {query}",
            data={
                "results": [
                    {
                        "url": "https://example.com/user-provider",
                        "title": "User Provider Result",
                        "snippet": f"Result for {query}",
                    }
                ],
                "diagnostics": {"provider_tool_name": self.name},
            },
        )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_runtime_registration_registers_fake_mcp_web_search(tmp_path: Path) -> None:
    """验证 fake MCP server 能注册 canonical tool 和通用 web_search。"""
    cfg = load_config(
        _write_config(
            tmp_path,
            f"""
mcp:
  enabled: true
  servers:
    - server_id: minimax
      command: {sys.executable}
      args:
        - {_fake_mcp_server_path()}
      aliases:
        - tool_name: web_search
          alias: web_search
web_search:
  enabled: true
  provider_name: fake_mcp
  search_tool_names:
    - web_search
    - mcp__minimax__web_search
""",
        ),
        load_env_file=False,
    )
    registry = ToolRegistry()
    manager = McpRuntimeRegistrationManager(cfg)

    try:
        result = await manager.register(registry)
        assert result.started_servers == ("minimax",)
        assert "mcp__minimax__web_search" in result.registered_tools
        assert "web_search" in result.registered_tools
        assert result.diagnostics["reserved_aliases"] == ("web_search",)
        assert result.diagnostics["web_search"]["reason"] == "registered"
        assert "mcp__minimax__web_search" in registry
        assert "web_search" in registry

        tool_result = await registry["web_search"].execute(
            {"query": "kongming deep research", "max_results": 1},
            ToolContext(run_id="r", session_id="s", turn=1, call_id="c"),
        )

        assert tool_result.ok is True
        assert tool_result.data is not None
        assert tool_result.data["results"][0]["url"] == "https://example.com/result"
        assert tool_result.data["results"][0]["title"] == "Fake Search Result"
        assert tool_result.data["diagnostics"]["provider_name"] == "fake_mcp"
        assert tool_result.data["diagnostics"]["provider_tool_name"] == ("mcp__minimax__web_search")
    finally:
        await manager.aclose()

    assert manager.mcp_manager is not None
    assert manager.mcp_manager.diagnostics()["servers"]["minimax"]["status"] == "closed"


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    ("mcp_body", "expected_reason"),
    [
        ("mcp:\n  enabled: false", "mcp_disabled"),
        ("mcp:\n  enabled: true\n  servers: []", "no_servers_configured"),
    ],
)
async def test_runtime_registration_wraps_user_search_tool_without_mcp_servers(
    tmp_path: Path,
    mcp_body: str,
    expected_reason: str,
) -> None:
    """验证无 MCP server 时仍能封装用户注入搜索工具。"""
    cfg = load_config(
        _write_config(
            tmp_path,
            f"""
{mcp_body}
web_search:
  enabled: true
  provider_name: user_provider
  search_tool_names:
    - custom_search_tool
""",
        ),
        load_env_file=False,
    )
    registry = ToolRegistry()
    registry.register(_FakeUserSearchTool())
    manager = McpRuntimeRegistrationManager(cfg)

    result = await manager.register(registry)

    assert result.started_servers == ()
    assert result.registered_tools == ("web_search",)
    assert result.diagnostics["reason"] == expected_reason
    assert result.diagnostics["web_search"]["reason"] == "registered"
    assert result.diagnostics["web_search"]["search_tool_name"] == "custom_search_tool"
    assert "custom_search_tool" in registry
    assert "web_search" in registry

    tool_result = await registry["web_search"].execute(
        {"query": "user injected search"},
        ToolContext(run_id="r", session_id="s", turn=1, call_id="c"),
    )

    assert tool_result.ok is True
    assert tool_result.data is not None
    assert tool_result.data["results"][0]["url"] == "https://example.com/user-provider"
    assert tool_result.data["diagnostics"]["provider_name"] == "user_provider"
    assert tool_result.data["diagnostics"]["provider_tool_name"] == "custom_search_tool"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_runtime_registration_keeps_baseline_when_mcp_command_missing(
    tmp_path: Path,
) -> None:
    """验证 MCP command 缺失时只写 diagnostics，ToolRegistry 仍可继续使用。"""
    cfg = load_config(
        _write_config(
            tmp_path,
            """
mcp:
  enabled: true
  servers:
    - server_id: minimax
      command: definitely-missing-kongming-mcp-command
web_search:
  enabled: true
  search_tool_names:
    - mcp__minimax__web_search
""",
        ),
        load_env_file=False,
    )
    registry = ToolRegistry()
    manager = McpRuntimeRegistrationManager(cfg)

    try:
        result = await manager.register(registry)

        assert result.started_servers == ()
        assert result.registered_tools == ()
        assert "web_search" not in registry
        server_diag = result.diagnostics["mcp_manager"]["servers"]["minimax"]
        assert server_diag["status"] == "failed"
        assert server_diag["last_event"] == "missing_command"
        assert result.diagnostics["web_search"]["reason"] == "search_tool_missing"
    finally:
        await manager.aclose()
