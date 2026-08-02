"""插件工具管理门户。

本模块只管理工具可见性状态，不拥有真实 Tool 生命周期。真实 Tool 由 Web runtime
factory 和 MCP 注册链路维护；这里保存 per-tool enabled bool，并在新 session
创建时生成可暴露给 LLM 的工具名列表。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from hosts.web.plugin_management.store import PluginToolState, PluginToolStateStore


class PluginManagementManager:
    """Web 插件工具状态门户。"""

    def __init__(self, store: PluginToolStateStore) -> None:
        """初始化 manager，输入持久化 store。"""
        self._store = store

    @classmethod
    def from_home(cls, home: Path) -> PluginManagementManager:
        """从 kongming_home 构造 manager。"""
        return cls(PluginToolStateStore.from_home(home))

    @property
    def store(self) -> PluginToolStateStore:
        """返回底层 store，供测试观察。"""
        return self._store

    def list_registered_plugins(self) -> tuple[PluginToolState, ...]:
        """返回当前可展示的 MCP 插件工具。"""
        return self._store.list_available_mcp_states()

    def set_enabled(self, tool_id: str, enabled: bool) -> PluginToolState:
        """更新单个插件工具 enabled 状态。"""
        return self._store.set_enabled(tool_id, enabled)

    def sync_mcp_tools(self, registry: Iterable[object]) -> tuple[PluginToolState, ...]:
        """从 ToolRegistry 同步当前 MCP 工具元数据。"""
        states: list[PluginToolState] = []
        for tool in registry:
            state = _state_from_mcp_tool(tool)
            if state is not None:
                states.append(state)
        return self._store.sync_mcp_tools(tuple(states))

    def enabled_tool_names(self, candidate_names: Sequence[str]) -> list[str]:
        """按插件 enabled 状态过滤新 session 可暴露工具名。"""
        return self._store.filter_enabled_tool_names(tuple(candidate_names))


def _state_from_mcp_tool(tool: object) -> PluginToolState | None:
    """从 MCP Tool adapter 提取插件状态。"""
    metadata_obj = getattr(tool, "metadata", None)
    if not isinstance(metadata_obj, Mapping):
        return None
    metadata: Mapping[object, object] = metadata_obj
    server_id = _string_value(metadata.get("server_id"))
    mcp_tool_name = _string_value(metadata.get("mcp_tool_name"))
    tool_name = _string_value(getattr(tool, "name", None))
    if not server_id or not mcp_tool_name or not tool_name:
        return None

    title = _string_value(metadata.get("title"))
    canonical_name = _string_value(metadata.get("canonical_name")) or tool_name
    is_alias = bool(metadata.get("is_alias", False))
    description = _string_value(getattr(tool, "description", None)) or ""
    return PluginToolState(
        id=tool_name,
        name=tool_name,
        display_name=title or mcp_tool_name,
        source="mcp",
        enabled=True,
        available=True,
        server_id=server_id,
        mcp_tool_name=mcp_tool_name,
        description=description,
        canonical_name=canonical_name,
        is_alias=is_alias,
    )


def _string_value(value: object) -> str | None:
    """返回非空字符串。"""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


__all__ = ["PluginManagementManager"]
