"""MCP runtime 注册管理器。

本模块负责宿主共享的 MCP / Web Search 装配胶水。
作用是把配置里的 stdio MCP server 启动为 `McpManager`，将 tools/list
结果注册成 Kongming Tool，再把选中的底层搜索工具封装成通用 `web_search`。
关键执行流程：启动 MCP server、收集 descriptor、注册 canonical/alias 工具、
注册 `web_search` 门户、在宿主 shutdown 时关闭 MCP 子进程。
关键函数：
- McpRuntimeRegistrationManager.register：执行一次 ToolRegistry 装配。
- McpRuntimeRegistrationManager.aclose：关闭已启动的 MCP manager。
- _build_alias_configs：把每个 server 的 alias 配置补齐 server_id。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from application.web_search import (
    WebSearchManager,
    build_missing_web_search_tool,
    build_web_search_tool,
)
from core.contracts import Event, EventSink, Tool
from infrastructure.config import Config
from infrastructure.mcp import McpManager
from tools.mcp import McpToolAdapterManager
from tools.mcp.adapter import McpToolAliasConfig
from tools.runtime.registry import ToolRegistry

_REGISTRATION_RUN_ID = "mcp-runtime-registration"
_WEB_SEARCH_TOOL_NAME = "web_search"
_MAX_DIAGNOSTIC_ERROR_MESSAGE_CHARS = 300
_SENSITIVE_ERROR_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "password",
    "secret",
    "token",
)


@dataclass(frozen=True)
class McpRuntimeRegistrationResult:
    """MCP runtime 注册结果。"""

    started_servers: tuple[str, ...] = ()
    registered_tools: tuple[str, ...] = ()
    enabled_tool_names: tuple[str, ...] = ()
    diagnostics: dict[str, Any] = field(default_factory=dict)


class McpRuntimeRegistrationManager:
    """宿主共享的 MCP / Web Search 注册边界。"""

    def __init__(
        self,
        config: Config,
        *,
        event_sinks: Sequence[EventSink] = (),
        mcp_manager_factory: Any = McpManager,
    ) -> None:
        """初始化注册管理器，输入 Config 和事件 sink，输出可注册实例。"""
        self._config = config
        self._event_sinks = tuple(event_sinks)
        self._mcp_manager_factory = mcp_manager_factory
        self._mcp_manager: Any | None = None
        self._closed = False
        self._last_result = McpRuntimeRegistrationResult()

    @property
    def mcp_manager(self) -> Any | None:
        """返回底层 MCP manager，输入为当前状态，输出 manager 或 None。"""
        return self._mcp_manager

    @property
    def last_result(self) -> McpRuntimeRegistrationResult:
        """返回最近一次注册结果。"""
        return self._last_result

    async def register(
        self,
        registry: ToolRegistry,
        *,
        excluded_tool_names: Sequence[str] = (),
    ) -> McpRuntimeRegistrationResult:
        """注册 MCP 与 Web Search 工具，输入 registry，输出注册结果。"""
        mcp_cfg = self._config.mcp
        diagnostics: dict[str, Any] = {
            "mcp_server_count": len(mcp_cfg.servers),
            "web_search_enabled": self._config.web_search.enabled,
        }
        if not mcp_cfg.servers:
            web_search_diagnostics = _register_web_search_tool(
                registry,
                web_search_cfg=self._config.web_search,
            )
            result = self._finish_result(
                registry,
                registered_tools=_registered_web_search_tools(web_search_diagnostics),
                diagnostics={
                    **diagnostics,
                    "reason": "no_servers_configured",
                    "web_search": web_search_diagnostics,
                },
                excluded_tool_names=excluded_tool_names,
            )
            await self._emit("mcp.registration.skipped", result.diagnostics)
            return result

        mcp_manager: Any | None = None
        try:
            mcp_manager = self._mcp_manager_factory(mcp_cfg.servers)
            self._mcp_manager = mcp_manager
            self._closed = False
            await mcp_manager.start_all()
        except Exception as exc:
            secret_env_keys = _secret_env_keys(mcp_cfg.servers)
            cleanup_diagnostics = await self._cleanup_startup_failure(
                mcp_manager,
                secret_env_keys=secret_env_keys,
            )
            web_search_diagnostics = _register_web_search_tool(
                registry,
                web_search_cfg=self._config.web_search,
            )
            diagnostics.update(
                {
                    "reason": "mcp_startup_failed",
                    "error_class": type(exc).__name__,
                    "error_message": _diagnostic_error_message(
                        exc,
                        secret_env_keys=secret_env_keys,
                    ),
                    "mcp_manager": _manager_diagnostics(
                        mcp_manager,
                        secret_env_keys=secret_env_keys,
                    ),
                    "cleanup": cleanup_diagnostics,
                    "web_search": web_search_diagnostics,
                }
            )
            result = self._finish_result(
                registry,
                registered_tools=_registered_web_search_tools(web_search_diagnostics),
                diagnostics=diagnostics,
                excluded_tool_names=excluded_tool_names,
            )
            await self._emit("mcp.registration.failed", result.diagnostics)
            return result
        assert mcp_manager is not None

        descriptors = []
        manager_diagnostics = _mapping(getattr(mcp_manager, "diagnostics", lambda: {})())
        for server in mcp_cfg.servers:
            if not server.enabled:
                continue
            if not _server_is_ready(manager_diagnostics, server.server_id):
                continue
            descriptors.extend(await mcp_manager.list_tools(server.server_id))

        alias_configs, reserved_aliases = _build_alias_configs(
            mcp_cfg.servers,
            reserve_web_search_alias=self._config.web_search.enabled,
        )
        adapter_manager = McpToolAdapterManager(
            mcp_manager,
            alias_configs=alias_configs,
            existing_tool_names=registry.names(),
        )
        plan = adapter_manager.build_registration_plan(descriptors)
        registered_tools: list[str] = []
        for tool in adapter_manager.build_tools(plan):
            registry.register(cast(Tool, tool))
            registered_tools.append(tool.name)

        web_search_diagnostics = _register_web_search_tool(
            registry,
            web_search_cfg=self._config.web_search,
        )
        if web_search_diagnostics.get("registered_tool_name"):
            registered_tools.append(str(web_search_diagnostics["registered_tool_name"]))

        manager_diagnostics = _mapping(getattr(mcp_manager, "diagnostics", lambda: {})())
        started_servers = _ready_server_ids(manager_diagnostics)
        diagnostics.update(
            {
                "started_servers": started_servers,
                "descriptor_count": len(descriptors),
                "reserved_aliases": tuple(reserved_aliases),
                "adapter_plan": plan.diagnostics,
                "web_search": web_search_diagnostics,
                "mcp_manager": manager_diagnostics,
            }
        )
        result = self._finish_result(
            registry,
            started_servers=started_servers,
            registered_tools=tuple(registered_tools),
            diagnostics=diagnostics,
            excluded_tool_names=excluded_tool_names,
        )
        await self._emit("mcp.registration.completed", result.diagnostics)
        return result

    async def aclose(self) -> None:
        """关闭 MCP manager，输入为空，输出为子进程已清理。"""
        if self._closed:
            return
        manager = self._mcp_manager
        if manager is None:
            self._closed = True
            return
        self._closed = True
        aclose = getattr(manager, "aclose", None)
        if aclose is not None:
            await aclose()
        await self._emit(
            "mcp.registration.closed",
            {"last_result": self._last_result.diagnostics},
        )

    def _finish_result(
        self,
        registry: ToolRegistry,
        *,
        started_servers: Sequence[str] = (),
        registered_tools: Sequence[str],
        diagnostics: dict[str, Any],
        excluded_tool_names: Sequence[str],
    ) -> McpRuntimeRegistrationResult:
        """生成并缓存注册结果，输入 registry 状态，输出结果对象。"""
        excluded = set(excluded_tool_names)
        enabled_tool_names = tuple(name for name in registry.names() if name not in excluded)
        result = McpRuntimeRegistrationResult(
            started_servers=tuple(started_servers),
            registered_tools=tuple(registered_tools),
            enabled_tool_names=enabled_tool_names,
            diagnostics=diagnostics,
        )
        self._last_result = result
        return result

    async def _cleanup_startup_failure(
        self,
        mcp_manager: Any | None,
        *,
        secret_env_keys: Sequence[str] = (),
    ) -> dict[str, Any]:
        """清理启动失败后的 MCP manager，输入 manager，输出清理诊断。"""
        if mcp_manager is None:
            self._mcp_manager = None
            return {"attempted": False, "closed": False, "reason": "manager_unavailable"}
        self._mcp_manager = None
        aclose = getattr(mcp_manager, "aclose", None)
        if aclose is None:
            return {"attempted": False, "closed": False, "reason": "aclose_unavailable"}
        try:
            await aclose()
        except Exception as exc:
            return {
                "attempted": True,
                "closed": False,
                "error_class": type(exc).__name__,
                "error_message": _diagnostic_error_message(
                    exc,
                    secret_env_keys=secret_env_keys,
                ),
            }
        return {"attempted": True, "closed": True}

    async def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        """发出注册事件，输入事件 kind 和 payload，输出为 sink fan-out。"""
        event = Event(kind=kind, run_id=_REGISTRATION_RUN_ID, payload=dict(payload))
        for sink in self._event_sinks:
            try:
                await sink.emit(event)
            except Exception:
                continue


def _build_alias_configs(
    servers: Sequence[Any],
    *,
    reserve_web_search_alias: bool,
) -> tuple[tuple[McpToolAliasConfig, ...], tuple[str, ...]]:
    """构造 adapter alias 配置，输入 server 配置，输出 alias 与保留名。"""
    aliases: list[McpToolAliasConfig] = []
    reserved: list[str] = []
    for server in servers:
        server_id = str(getattr(server, "server_id", ""))
        for alias in getattr(server, "aliases", ()) or ():
            alias_name = str(getattr(alias, "alias", ""))
            if reserve_web_search_alias and alias_name == _WEB_SEARCH_TOOL_NAME:
                reserved.append(alias_name)
                continue
            aliases.append(
                McpToolAliasConfig(
                    tool_name=str(getattr(alias, "tool_name", "")),
                    alias=alias_name,
                    enabled=bool(getattr(alias, "enabled", True)),
                    server_id=server_id,
                )
            )
    return tuple(aliases), tuple(reserved)


def _register_web_search_tool(
    registry: ToolRegistry,
    *,
    web_search_cfg: Any,
) -> dict[str, Any]:
    """按配置注册通用 web_search，输入 registry/config，输出诊断。"""
    if not getattr(web_search_cfg, "enabled", False):
        return {"enabled": False, "reason": "disabled_by_config"}
    if _registry_has_tool(registry, _WEB_SEARCH_TOOL_NAME):
        return {
            "enabled": True,
            "reason": "already_registered",
            "search_tool_name": _WEB_SEARCH_TOOL_NAME,
        }

    search_tool_name, search_tool = _resolve_search_tool(registry, web_search_cfg)
    if search_tool is None or search_tool_name is None:
        candidate_tool_names = _candidate_search_tool_names(web_search_cfg)
        registry.register(
            cast(
                Tool,
                build_missing_web_search_tool(
                    provider_name=str(getattr(web_search_cfg, "provider_name", "web_search")),
                    candidate_tool_names=candidate_tool_names,
                ),
            )
        )
        return {
            "enabled": True,
            "reason": "search_tool_missing",
            "candidate_tool_names": candidate_tool_names,
            "registered_tool_name": _WEB_SEARCH_TOOL_NAME,
        }

    manager = WebSearchManager(
        search_tool,
        provider_name=str(getattr(web_search_cfg, "provider_name", "web_search")),
        provider_tool_name=search_tool_name,
    )
    tool = build_web_search_tool(manager)
    registry.register(cast(Tool, tool))
    return {
        "enabled": True,
        "reason": "registered",
        "search_tool_name": search_tool_name,
        "registered_tool_name": _WEB_SEARCH_TOOL_NAME,
        "provider_name": getattr(web_search_cfg, "provider_name", "web_search"),
    }


def _registered_web_search_tools(web_search_diagnostics: dict[str, Any]) -> tuple[str, ...]:
    """从 Web Search 诊断中提取新增工具名，输入 diagnostics，输出工具名元组。"""
    registered_tool_name = web_search_diagnostics.get("registered_tool_name")
    return (str(registered_tool_name),) if registered_tool_name else ()


def _resolve_search_tool(registry: ToolRegistry, web_search_cfg: Any) -> tuple[str | None, Any]:
    """解析底层搜索工具，输入 registry/config，输出工具名与工具。"""
    for name in _candidate_search_tool_names(web_search_cfg):
        if name == _WEB_SEARCH_TOOL_NAME:
            continue
        tool = _registry_get_tool(registry, name)
        if tool is not None:
            return name, tool
    return None, None


def _candidate_search_tool_names(web_search_cfg: Any) -> tuple[str, ...]:
    """读取候选搜索工具名，输入配置，输出去重后的名称列表。"""
    names: list[str] = []
    primary = getattr(web_search_cfg, "search_tool_name", None)
    if primary:
        names.append(str(primary))
    for name in getattr(web_search_cfg, "search_tool_names", ()) or ():
        names.append(str(name))
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        unique.append(name)
    return tuple(unique)


def _ready_server_ids(diagnostics: dict[str, Any]) -> tuple[str, ...]:
    """提取 ready server id，输入 MCP diagnostics，输出 server_id 元组。"""
    servers = diagnostics.get("servers")
    if not isinstance(servers, dict):
        return ()
    ready = []
    for server_id, server_diag in servers.items():
        if isinstance(server_diag, dict) and server_diag.get("status") == "ready":
            ready.append(str(server_id))
    return tuple(ready)


def _server_is_ready(diagnostics: dict[str, Any], server_id: str) -> bool:
    """判断 server 是否 ready，输入 diagnostics/server_id，输出布尔值。"""
    servers = diagnostics.get("servers")
    if not isinstance(servers, dict):
        return False
    server_diag = servers.get(server_id)
    return isinstance(server_diag, dict) and server_diag.get("status") == "ready"


def _mapping(value: object) -> dict[str, Any]:
    """把 mapping 转成 dict，输入任意值，输出 dict。"""
    return dict(value) if isinstance(value, dict) else {}


def _manager_diagnostics(
    manager: Any | None,
    *,
    secret_env_keys: Sequence[str] = (),
) -> dict[str, Any]:
    """读取 MCP manager 诊断，输入 manager，输出可序列化 dict。"""
    if manager is None:
        return {}
    diagnostics_fn = getattr(manager, "diagnostics", None)
    if not callable(diagnostics_fn):
        return {}
    try:
        diagnostics = diagnostics_fn()
    except Exception as exc:
        return {
            "diagnostics_failed": type(exc).__name__,
            "error_message": _diagnostic_error_message(
                exc,
                secret_env_keys=secret_env_keys,
            ),
        }
    return _mapping(diagnostics)


def _diagnostic_error_message(
    exc: BaseException,
    *,
    secret_env_keys: Sequence[str] = (),
) -> str:
    """生成安全错误摘要，输入异常，输出脱敏且截断后的 diagnostics 文本。"""
    message = str(exc).replace("\n", " ").strip()
    if not message:
        return type(exc).__name__
    redacted = _redact_error_message(message, secret_env_keys=secret_env_keys)
    if len(redacted) <= _MAX_DIAGNOSTIC_ERROR_MESSAGE_CHARS:
        return redacted
    return f"{redacted[:_MAX_DIAGNOSTIC_ERROR_MESSAGE_CHARS].rstrip()}..."


def _redact_error_message(message: str, *, secret_env_keys: Sequence[str]) -> str:
    """脱敏错误文本，输入原始异常消息和 secret key 列表，输出脱敏文本。"""
    redacted = message
    for key in secret_env_keys:
        key_text = str(key).strip()
        if not key_text:
            continue
        pattern = re.compile(
            rf"\b({re.escape(key_text)})\b\s*[:=]\s*([\"']?)[^\s,\"']+\2",
            flags=re.IGNORECASE,
        )
        redacted = pattern.sub(lambda match: f"{match.group(1)}=<redacted>", redacted)
    sensitive_names = "|".join(re.escape(marker) for marker in _SENSITIVE_ERROR_MARKERS)
    redacted = re.sub(
        rf"(?i)\b([A-Za-z0-9_.-]*(?:{sensitive_names}|key))\b\s*[:=]\s*([\"']?)[^\s,\"']+\2",
        lambda match: f"{match.group(1)}=<redacted>",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer <redacted>",
        redacted,
    )
    return redacted


def _secret_env_keys(servers: Sequence[Any]) -> tuple[str, ...]:
    """收集 MCP server secret env key，输入 server 配置，输出去重 key 元组。"""
    keys: list[str] = []
    for server in servers:
        for key in getattr(server, "secret_env_keys", ()) or ():
            key_text = str(key).strip()
            if key_text and key_text not in keys:
                keys.append(key_text)
    return tuple(keys)


def _registry_has_tool(registry: object, name: str) -> bool:
    """兼容不同 registry 形态检查工具名，输入 registry/name，输出是否存在。"""
    try:
        return name in registry  # type: ignore[operator]
    except TypeError:
        pass
    names = getattr(registry, "names", None)
    if callable(names):
        return name in names()
    return _registry_get_tool(registry, name) is not None


def _registry_get_tool(registry: object, name: str) -> Any | None:
    """兼容不同 registry 形态读取工具，输入 registry/name，输出 tool 或 None。"""
    get = getattr(registry, "get", None)
    if callable(get):
        return get(name)
    try:
        return registry[name]  # type: ignore[index]
    except (KeyError, TypeError, AttributeError):
        return None


__all__ = ["McpRuntimeRegistrationManager", "McpRuntimeRegistrationResult"]
