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


class _RaisingMcpManager:
    """启动阶段抛错的 MCP manager fake，用于验证注册门户兜底清理。"""

    def __init__(self, _servers: object) -> None:
        """初始化 fake，输入 server 配置，输出可观察 closed 状态。"""
        self.closed = False
        self._diagnostics: dict[str, object] = {
            "servers": {"minimax": {"status": "starting"}},
            "events": [{"type": "starting", "server_id": "minimax"}],
        }

    async def start_all(self) -> None:
        """模拟启动失败，输入为空，输出 RuntimeError。"""
        raise RuntimeError("stdio transport exploded")

    async def aclose(self) -> None:
        """记录清理调用，输入为空，输出 closed 状态。"""
        self.closed = True
        self._diagnostics["closed"] = True

    def diagnostics(self) -> dict[str, object]:
        """返回 fake 诊断，输入为空，输出诊断 dict。"""
        return dict(self._diagnostics)


class _RaisingCloseMcpManager(_RaisingMcpManager):
    """关闭阶段抛错的 MCP manager fake，用于验证引用提前清理。"""

    async def aclose(self) -> None:
        """模拟关闭失败，输入为空，输出 RuntimeError。"""
        self.closed = True
        raise RuntimeError("close exploded")


class _SensitiveRaisingMcpManager(_RaisingMcpManager):
    """启动异常包含敏感 marker 的 MCP manager fake。"""

    async def start_all(self) -> None:
        """模拟敏感启动错误，输入为空，输出 RuntimeError。"""
        raise RuntimeError("MINIMAX_API_KEY=sk-secret-token transport exploded")


class _ClosableMcpManager:
    """可成功启动关闭的 MCP manager fake，用于验证 aclose 幂等。"""

    def __init__(self, _servers: object) -> None:
        """初始化 fake，输入 server 配置，输出可观察关闭计数。"""
        self.closed = False
        self.close_calls = 0

    async def start_all(self) -> None:
        """模拟启动成功，输入为空，输出 ready 状态。"""
        self.closed = False

    async def list_tools(self, server_id: str) -> list[object]:
        """返回空工具列表，输入 server_id，输出空 descriptors。"""
        del server_id
        return []

    async def aclose(self) -> None:
        """记录关闭次数，输入为空，输出 closed 状态。"""
        self.close_calls += 1
        self.closed = True

    def diagnostics(self) -> dict[str, object]:
        """返回 fake 诊断，输入为空，输出 server 状态。"""
        status = "closed" if self.closed else "ready"
        return {"servers": {"minimax": {"status": status}}}


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


@pytest.mark.asyncio
@pytest.mark.unit
async def test_runtime_registration_cleans_up_when_start_all_raises(tmp_path: Path) -> None:
    """验证 MCP 启动异常，输入抛错 manager，输出诊断 fallback 和资源清理。"""
    instances: list[_RaisingMcpManager] = []

    def _factory(servers: object) -> _RaisingMcpManager:
        manager = _RaisingMcpManager(servers)
        instances.append(manager)
        return manager

    cfg = load_config(
        _write_config(
            tmp_path,
            """
mcp:
  enabled: true
  servers:
    - server_id: minimax
      command: fake-mcp
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
    manager = McpRuntimeRegistrationManager(cfg, mcp_manager_factory=_factory)

    result = await manager.register(registry)

    assert result.diagnostics["reason"] == "mcp_startup_failed"
    assert result.diagnostics["error_class"] == "RuntimeError"
    assert result.diagnostics["cleanup"]["closed"] is True
    assert instances[0].closed is True
    assert manager.mcp_manager is None
    assert result.registered_tools == ("web_search",)
    assert result.diagnostics["web_search"]["reason"] == "registered"
    assert "web_search" in registry


@pytest.mark.asyncio
@pytest.mark.unit
async def test_runtime_registration_redacts_sensitive_startup_error(tmp_path: Path) -> None:
    """验证启动异常脱敏，输入含 API key marker 的错误，输出安全 diagnostics。"""

    def _factory(servers: object) -> _SensitiveRaisingMcpManager:
        return _SensitiveRaisingMcpManager(servers)

    cfg = load_config(
        _write_config(
            tmp_path,
            """
mcp:
  enabled: true
  servers:
    - server_id: minimax
      command: fake-mcp
web_search:
  enabled: true
  search_tool_names:
    - custom_search_tool
""",
        ),
        load_env_file=False,
    )
    registry = ToolRegistry()
    registry.register(_FakeUserSearchTool())
    manager = McpRuntimeRegistrationManager(cfg, mcp_manager_factory=_factory)

    result = await manager.register(registry)

    assert result.diagnostics["error_class"] == "RuntimeError"
    assert result.diagnostics["error_message"] == "<redacted sensitive diagnostic>"
    assert "sk-secret-token" not in str(result.diagnostics)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_runtime_registration_clears_manager_when_cleanup_raises(tmp_path: Path) -> None:
    """验证 cleanup 抛错，输入关闭失败 manager，输出引用已清空和错误诊断。"""
    instances: list[_RaisingCloseMcpManager] = []

    def _factory(servers: object) -> _RaisingCloseMcpManager:
        manager = _RaisingCloseMcpManager(servers)
        instances.append(manager)
        return manager

    cfg = load_config(
        _write_config(
            tmp_path,
            """
mcp:
  enabled: true
  servers:
    - server_id: minimax
      command: fake-mcp
web_search:
  enabled: true
  search_tool_names:
    - custom_search_tool
""",
        ),
        load_env_file=False,
    )
    registry = ToolRegistry()
    registry.register(_FakeUserSearchTool())
    manager = McpRuntimeRegistrationManager(cfg, mcp_manager_factory=_factory)

    result = await manager.register(registry)

    assert result.diagnostics["cleanup"]["closed"] is False
    assert result.diagnostics["cleanup"]["error_class"] == "RuntimeError"
    assert instances[0].closed is True
    assert manager.mcp_manager is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_runtime_registration_aclose_is_idempotent(tmp_path: Path) -> None:
    """验证关闭幂等，输入已启动 manager，输出底层 aclose 只调用一次。"""
    instances: list[_ClosableMcpManager] = []

    def _factory(servers: object) -> _ClosableMcpManager:
        manager = _ClosableMcpManager(servers)
        instances.append(manager)
        return manager

    cfg = load_config(
        _write_config(
            tmp_path,
            """
mcp:
  enabled: true
  servers:
    - server_id: minimax
      command: fake-mcp
web_search:
  enabled: false
""",
        ),
        load_env_file=False,
    )
    registry = ToolRegistry()
    manager = McpRuntimeRegistrationManager(cfg, mcp_manager_factory=_factory)

    result = await manager.register(registry)
    await manager.aclose()
    await manager.aclose()

    assert result.diagnostics["started_servers"] == ("minimax",)
    assert instances[0].close_calls == 1
    assert instances[0].closed is True
