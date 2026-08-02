"""插件工具 enabled 状态持久化。

关键流程：
1. MCP 注册完成后同步当前工具元数据，保留用户已有 enabled 选择。
2. PATCH 开关只更新单个工具的 enabled 布尔值。
3. 新 session 创建时读取本 store 过滤可暴露给 LLM 的工具名。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal, cast

_STORE_VERSION = 1


@dataclass(frozen=True)
class PluginToolState:
    """单个插件工具的持久化状态。"""

    id: str
    name: str
    display_name: str
    source: Literal["mcp"]
    enabled: bool
    available: bool
    server_id: str
    mcp_tool_name: str
    description: str = ""
    canonical_name: str = ""
    is_alias: bool = False


class PluginToolStateStore:
    """插件工具状态文件门户。"""

    def __init__(self, path: Path) -> None:
        """初始化 store，输入状态文件路径，输出可读写门户。"""
        self._path = path

    @classmethod
    def from_home(cls, home: Path) -> PluginToolStateStore:
        """从 kongming_home 构造 store。"""
        return cls(home / "web" / "plugin-tools.json")

    @property
    def path(self) -> Path:
        """返回状态文件路径。"""
        return self._path

    def list_states(self) -> tuple[PluginToolState, ...]:
        """读取全部工具状态。"""
        states = self._read_states()
        return tuple(sorted(states.values(), key=lambda item: item.id))

    def list_available_mcp_states(self) -> tuple[PluginToolState, ...]:
        """读取当前已注册且可显示的 MCP 工具状态。"""
        return tuple(state for state in self.list_states() if state.available)

    def get(self, tool_id: str) -> PluginToolState | None:
        """按工具 id 读取状态。"""
        return self._read_states().get(tool_id)

    def set_enabled(self, tool_id: str, enabled: bool) -> PluginToolState:
        """更新单个工具 enabled 状态。"""
        states = self._read_states()
        existing = states.get(tool_id)
        if existing is None:
            raise KeyError(tool_id)
        updated = replace(existing, enabled=enabled)
        states[tool_id] = updated
        self._write_states(states)
        return updated

    def sync_mcp_tools(self, tools: tuple[PluginToolState, ...]) -> tuple[PluginToolState, ...]:
        """同步当前 MCP 注册工具，保留已有 enabled 选择。"""
        states = self._read_states()
        for tool_id, state in tuple(states.items()):
            if state.source == "mcp":
                states[tool_id] = replace(state, available=False)

        for tool in tools:
            existing = states.get(tool.id)
            enabled = existing.enabled if existing is not None else True
            states[tool.id] = replace(tool, enabled=enabled, available=True)

        self._write_states(states)
        return self.list_available_mcp_states()

    def filter_enabled_tool_names(self, names: tuple[str, ...]) -> list[str]:
        """按 enabled 状态过滤候选工具名，未知工具按启用处理。"""
        states = self._read_states()
        enabled_names: list[str] = []
        for name in names:
            state = states.get(name)
            if (
                state is not None
                and state.source == "mcp"
                and (not state.enabled or not state.available)
            ):
                continue
            enabled_names.append(name)
        return enabled_names

    def _read_states(self) -> dict[str, PluginToolState]:
        """读取状态文件，输入为空，输出 id 到状态的映射。"""
        if not self._path.exists():
            return {}
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("plugin tools state must be a JSON object")
        raw_tools = raw.get("tools", {})
        if not isinstance(raw_tools, dict):
            raise ValueError("plugin tools state 'tools' must be an object")
        states: dict[str, PluginToolState] = {}
        for key, value in raw_tools.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            state = _state_from_mapping(key, cast(dict[str, object], value))
            states[state.id] = state
        return states

    def _write_states(self, states: dict[str, PluginToolState]) -> None:
        """原子写入状态文件。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _STORE_VERSION,
            "tools": {key: asdict(value) for key, value in sorted(states.items())},
        }
        tmp_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, self._path)


def _state_from_mapping(tool_id: str, raw: dict[str, object]) -> PluginToolState:
    """把 JSON mapping 转成状态对象。"""
    return PluginToolState(
        id=_string_value(raw.get("id")) or tool_id,
        name=_string_value(raw.get("name")) or tool_id,
        display_name=_string_value(raw.get("display_name")) or tool_id,
        source="mcp",
        enabled=bool(raw.get("enabled", True)),
        available=bool(raw.get("available", False)),
        server_id=_string_value(raw.get("server_id")) or "",
        mcp_tool_name=_string_value(raw.get("mcp_tool_name")) or "",
        description=_string_value(raw.get("description")) or "",
        canonical_name=_string_value(raw.get("canonical_name")) or "",
        is_alias=bool(raw.get("is_alias", False)),
    )


def _string_value(value: object) -> str | None:
    """把非空字符串值规范化。"""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


__all__ = ["PluginToolState", "PluginToolStateStore"]
