"""Web 插件工具管理后端测试。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hosts.web.errors import KongmingWebError, kongming_error_handler
from hosts.web.plugin_management import PluginManagementManager, PluginToolStateStore
from hosts.web.routers.manage import router as manage_router


class _FakeMcpTool:
    name = "mcp__minimax__web_search"
    description = "Search with MiniMax MCP"
    input_schema = {"type": "object", "properties": {}}
    metadata = {
        "server_id": "minimax",
        "mcp_tool_name": "web_search",
        "canonical_name": "mcp__minimax__web_search",
        "kongming_tool_name": "mcp__minimax__web_search",
        "is_alias": False,
        "title": "Web Search",
    }


class _RuntimePluginSync:
    """测试用 runtime 同步器。"""

    def __init__(self, manager: PluginManagementManager) -> None:
        """保存 manager 并记录同步次数。"""
        self.manager = manager
        self.calls = 0

    async def sync_plugin_tools_for_management(self) -> None:
        """模拟 runtime factory 把 MCP 工具同步进插件 store。"""
        self.calls += 1
        self.manager.sync_mcp_tools([_FakeMcpTool()])


def _manager(tmp_path: Path) -> PluginManagementManager:
    store = PluginToolStateStore(tmp_path / "plugin-tools.json")
    manager = PluginManagementManager(store)
    manager.sync_mcp_tools([_FakeMcpTool()])
    return manager


def _app(tmp_path: Path) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(KongmingWebError, kongming_error_handler)
    app.state.plugin_management_manager = _manager(tmp_path)
    app.include_router(manage_router)
    return app


def test_store_defaults_new_mcp_tool_to_enabled(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    plugins = manager.list_registered_plugins()

    assert len(plugins) == 1
    assert plugins[0].id == "mcp__minimax__web_search"
    assert plugins[0].enabled is True
    assert plugins[0].server_id == "minimax"
    assert plugins[0].mcp_tool_name == "web_search"


def test_store_persists_enabled_choice_across_sync(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.set_enabled("mcp__minimax__web_search", False)
    manager.sync_mcp_tools([_FakeMcpTool()])

    plugins = manager.list_registered_plugins()
    enabled_names = manager.enabled_tool_names(("read_file", "mcp__minimax__web_search"))

    assert plugins[0].enabled is False
    assert enabled_names == ["read_file"]


def test_store_marks_missing_mcp_tool_unavailable(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.sync_mcp_tools(())

    assert manager.list_registered_plugins() == ()
    assert manager.enabled_tool_names(("mcp__minimax__web_search",)) == []


def test_manage_plugins_routes_list_and_patch(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))

    list_resp = client.get("/api/manage/plugins")
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert body["plugins"][0]["id"] == "mcp__minimax__web_search"
    assert body["plugins"][0]["enabled"] is True

    patch_resp = client.patch(
        "/api/manage/plugins/mcp__minimax__web_search",
        json={"enabled": False},
    )

    assert patch_resp.status_code == 200
    assert patch_resp.json()["enabled"] is False
    assert client.get("/api/manage/plugins").json()["plugins"][0]["enabled"] is False


def test_manage_plugins_list_syncs_runtime_tools_before_reading_store(tmp_path: Path) -> None:
    manager = PluginManagementManager(PluginToolStateStore(tmp_path / "plugin-tools.json"))
    syncer = _RuntimePluginSync(manager)
    app = FastAPI()
    app.add_exception_handler(KongmingWebError, kongming_error_handler)
    app.state.plugin_management_manager = manager
    app.state.runtime_factory = syncer
    app.include_router(manage_router)
    client = TestClient(app)

    resp = client.get("/api/manage/plugins")

    assert resp.status_code == 200
    body = resp.json()
    assert syncer.calls == 1
    assert body["plugins"][0]["id"] == "mcp__minimax__web_search"


def test_manage_plugins_patch_unknown_returns_404(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))

    resp = client.patch("/api/manage/plugins/missing", json={"enabled": False})

    assert resp.status_code == 404
