"""MCP Tool 适配层。

本模块提供 `McpToolAdapterManager` 边界类，把 MCP tool descriptor 规划成
Kongming Tool 注册项，并把执行转发给底层 MCP manager/client 的 `call_tool`。
关键流程：生成 canonical name，处理 alias 冲突，构造实现 `core.contracts.Tool`
协议的 adapter，执行时保留 MCP data 和 diagnostics。
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.contracts import PreparedToolCall, ToolContext, ToolResult

_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z_]+")


@dataclass(frozen=True)
class McpToolDescriptor:
    """MCP tools/list 返回的工具描述快照。"""

    server_id: str
    name: str
    title: str | None = None
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    raw_descriptor: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class McpToolAliasConfig:
    """MCP tool 到 Kongming alias 的显式映射配置。"""

    tool_name: str
    alias: str
    enabled: bool = True
    server_id: str | None = None


@dataclass(frozen=True)
class McpToolRegistration:
    """准备注册进 ToolRegistry 的 Kongming Tool 项。"""

    kongming_tool_name: str
    canonical_name: str
    server_id: str
    mcp_tool_name: str
    is_alias: bool
    description: str
    input_schema: dict[str, Any]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class McpToolAdapterPlan:
    """MCP descriptor 到 Kongming Tool 的注册计划。"""

    registrations: tuple[McpToolRegistration, ...]
    skipped_aliases: tuple[str, ...] = ()
    diagnostics: dict[str, Any] = field(default_factory=dict)


class McpToolAdapterManager:
    """MCP Tool adapter 边界类。"""

    def __init__(
        self,
        client: Any,
        *,
        alias_configs: Sequence[object] = (),
        existing_tool_names: Sequence[str] = (),
    ) -> None:
        """初始化 manager，输入底层 MCP client、alias 配置和已占用工具名。"""
        self._client = client
        self._alias_configs = tuple(alias_configs)
        self._existing_tool_names = tuple(existing_tool_names)

    def build_registration_plan(
        self,
        descriptors: Sequence[object],
        *,
        alias_configs: Sequence[object] | None = None,
        existing_tool_names: Sequence[str] | None = None,
    ) -> McpToolAdapterPlan:
        """生成注册计划，输入 MCP descriptors，输出 canonical 与 alias 注册项。"""
        aliases = tuple(alias_configs) if alias_configs is not None else self._alias_configs
        planned_names = set(
            existing_tool_names if existing_tool_names is not None else self._existing_tool_names
        )
        registrations: list[McpToolRegistration] = []
        skipped_aliases: list[str] = []
        alias_diagnostics: list[dict[str, Any]] = []
        canonical_diagnostics: list[dict[str, Any]] = []

        for raw_descriptor in descriptors:
            descriptor = _coerce_descriptor(raw_descriptor)
            canonical_name = canonical_tool_name(descriptor.server_id, descriptor.name)
            if canonical_name in planned_names:
                canonical_diagnostics.append(
                    {
                        "reason": "canonical_conflict",
                        "kongming_tool_name": canonical_name,
                        "server_id": descriptor.server_id,
                        "mcp_tool_name": descriptor.name,
                    }
                )
                continue

            registration = _registration_from_descriptor(
                descriptor,
                kongming_tool_name=canonical_name,
                canonical_name=canonical_name,
                is_alias=False,
            )
            registrations.append(registration)
            planned_names.add(canonical_name)

            for alias in _aliases_for_descriptor(descriptor, aliases):
                if not alias.enabled:
                    alias_diagnostics.append(
                        {
                            "reason": "alias_disabled",
                            "alias": alias.alias,
                            "server_id": descriptor.server_id,
                            "mcp_tool_name": descriptor.name,
                        }
                    )
                    continue
                if alias.alias in planned_names:
                    skipped_aliases.append(alias.alias)
                    alias_diagnostics.append(
                        {
                            "reason": "alias_conflict",
                            "alias": alias.alias,
                            "server_id": descriptor.server_id,
                            "mcp_tool_name": descriptor.name,
                            "canonical_name": canonical_name,
                        }
                    )
                    continue
                registrations.append(
                    _registration_from_descriptor(
                        descriptor,
                        kongming_tool_name=alias.alias,
                        canonical_name=canonical_name,
                        is_alias=True,
                    )
                )
                planned_names.add(alias.alias)

        return McpToolAdapterPlan(
            registrations=tuple(registrations),
            skipped_aliases=tuple(skipped_aliases),
            diagnostics={
                "registered_tools": tuple(item.kongming_tool_name for item in registrations),
                "skipped_aliases": tuple(alias_diagnostics),
                "skipped_canonical_tools": tuple(canonical_diagnostics),
            },
        )

    def build_tool(self, registration: McpToolRegistration) -> McpToolAdapter:
        """构造 Tool adapter，输入注册项，输出可注册进 ToolRegistry 的 Tool。"""
        return McpToolAdapter(self._client, registration)

    def build_tools(self, plan: McpToolAdapterPlan) -> tuple[McpToolAdapter, ...]:
        """批量构造 Tool adapter，输入注册计划，输出 Tool adapter 列表。"""
        return tuple(self.build_tool(registration) for registration in plan.registrations)


class McpToolAdapter:
    """把单个 MCP tool 暴露成 Kongming Tool。"""

    def __init__(self, client: Any, registration: McpToolRegistration) -> None:
        """初始化 adapter，输入 MCP client 和注册项，输出 Tool 实例。"""
        self._client = client
        self._registration = registration
        self.name = registration.kongming_tool_name
        self.description = registration.description
        self.input_schema = dict(registration.input_schema)
        self.metadata = dict(registration.metadata)

    def prepare(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> PreparedToolCall:
        """审批前冻结 MCP 参数副本。"""
        del context
        if not isinstance(arguments, dict):
            raise TypeError(f"tool args must be dict, got {type(arguments).__name__}")
        return PreparedToolCall(arguments=dict(arguments))

    async def execute(
        self,
        prepared: PreparedToolCall,
        ctx: ToolContext,
    ) -> ToolResult:
        """执行 MCP tool，输入已准备调用/context，输出 ToolResult。"""
        del ctx
        try:
            call_result = self._client.call_tool(
                self._registration.server_id,
                self._registration.mcp_tool_name,
                dict(prepared.arguments),
            )
            value = await call_result if inspect.isawaitable(call_result) else call_result
        except Exception as exc:
            diagnostics = {
                "error_kind": type(exc).__name__,
                "server_id": self._registration.server_id,
                "mcp_tool_name": self._registration.mcp_tool_name,
                "kongming_tool_name": self.name,
            }
            return ToolResult(
                ok=False,
                content=f"MCP tool call failed: {exc}",
                data={"mcp_diagnostics": diagnostics, "mcp_tool": self.metadata},
                error_message=str(exc),
            )

        ok = bool(_result_field(value, "ok", True))
        error_message = _optional_str(_result_field(value, "error_message", None))
        content = _optional_str(_result_field(value, "content_text", None))
        if content is None:
            content = _optional_str(_result_field(value, "content", None))
        if content is None:
            content = error_message or ""

        data = _result_data(value)
        diagnostics = _mapping_or_empty(_result_field(value, "diagnostics", None))
        if diagnostics:
            data["mcp_diagnostics"] = diagnostics
        data["mcp_tool"] = dict(self.metadata)

        return ToolResult(
            ok=ok,
            content=content,
            data=data,
            error_message=error_message,
        )


def canonical_tool_name(server_id: str, tool_name: str) -> str:
    """生成 canonical name，输入 server_id/tool_name，输出 mcp__server__tool。"""
    return f"mcp__{_safe_identifier(server_id)}__{_safe_identifier(tool_name)}"


def _safe_identifier(value: str) -> str:
    """把外部标识转成 ToolRegistry 可读名称，输入原始字符串，输出安全标识。"""
    safe = _SAFE_NAME_RE.sub("_", value.strip()).strip("_").lower()
    return safe or "unnamed"


def _coerce_descriptor(value: object) -> McpToolDescriptor:
    """标准化 descriptor，输入 dataclass/mapping/object，输出 McpToolDescriptor。"""
    raw = _mapping_or_empty(value)
    server_id = _required_str(_field(value, raw, "server_id"), "server_id")
    name = _required_str(_field(value, raw, "name"), "name")
    raw_descriptor = _mapping_or_empty(_field(value, raw, "raw_descriptor"))
    if not raw_descriptor and raw:
        raw_descriptor = dict(raw)
    input_schema = _mapping_or_empty(_field(value, raw, "input_schema", "inputSchema", "schema"))
    return McpToolDescriptor(
        server_id=server_id,
        name=name,
        title=_optional_str(_field(value, raw, "title")),
        description=_optional_str(_field(value, raw, "description")) or name,
        input_schema=_object_schema(input_schema),
        raw_descriptor=raw_descriptor,
    )


def _coerce_alias_config(value: object) -> McpToolAliasConfig:
    """标准化 alias 配置，输入 dataclass/mapping/object，输出 McpToolAliasConfig。"""
    raw = _mapping_or_empty(value)
    return McpToolAliasConfig(
        tool_name=_required_str(_field(value, raw, "tool_name", "mcp_tool_name"), "tool_name"),
        alias=_required_str(_field(value, raw, "alias"), "alias"),
        enabled=bool(
            _field(value, raw, "enabled") if _field(value, raw, "enabled") is not None else True
        ),
        server_id=_optional_str(_field(value, raw, "server_id")),
    )


def _aliases_for_descriptor(
    descriptor: McpToolDescriptor, alias_configs: Sequence[object]
) -> tuple[McpToolAliasConfig, ...]:
    """筛选 descriptor 对应的 alias 配置。"""
    matched: list[McpToolAliasConfig] = []
    for raw_alias in alias_configs:
        alias = _coerce_alias_config(raw_alias)
        if alias.tool_name != descriptor.name:
            continue
        if alias.server_id is not None and alias.server_id != descriptor.server_id:
            continue
        matched.append(alias)
    return tuple(matched)


def _registration_from_descriptor(
    descriptor: McpToolDescriptor,
    *,
    kongming_tool_name: str,
    canonical_name: str,
    is_alias: bool,
) -> McpToolRegistration:
    """从 descriptor 构造注册项。"""
    metadata = {
        "server_id": descriptor.server_id,
        "mcp_tool_name": descriptor.name,
        "canonical_name": canonical_name,
        "kongming_tool_name": kongming_tool_name,
        "is_alias": is_alias,
        "title": descriptor.title,
        "raw_descriptor": dict(descriptor.raw_descriptor),
    }
    return McpToolRegistration(
        kongming_tool_name=kongming_tool_name,
        canonical_name=canonical_name,
        server_id=descriptor.server_id,
        mcp_tool_name=descriptor.name,
        is_alias=is_alias,
        description=descriptor.description or descriptor.title or descriptor.name,
        input_schema=_object_schema(descriptor.input_schema),
        metadata=metadata,
    )


def _object_schema(schema: Mapping[str, Any] | None) -> dict[str, Any]:
    """补齐 object schema 壳，输入 provider schema，输出 Kongming input_schema。"""
    normalized = dict(schema or {})
    if not normalized:
        return {"type": "object", "properties": {}}
    normalized.setdefault("type", "object")
    normalized.setdefault("properties", {})
    return normalized


def _field(value: object, raw: Mapping[str, Any], *names: str) -> Any:
    """按多候选字段读取值，输入对象和字段名，输出首个命中值。"""
    for name in names:
        if name in raw:
            return raw[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _result_field(value: object, name: str, default: Any = None) -> Any:
    """读取 call result 字段，输入对象和字段名，输出字段值或默认值。"""
    if isinstance(value, Mapping) and name in value:
        return value[name]
    return getattr(value, name, default)


def _result_data(value: object) -> dict[str, Any]:
    """提取 call result data，输入 call result，输出可写入 ToolResult 的 dict。"""
    data = _result_field(value, "data", None)
    if isinstance(data, Mapping):
        return dict(data)
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _mapping_or_empty(value: object) -> dict[str, Any]:
    """把 mapping 转成 dict，输入任意对象，输出 dict 或空 dict。"""
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _required_str(value: object, field_name: str) -> str:
    """校验必填字符串，输入原始值，输出非空字符串。"""
    text = _optional_str(value)
    if not text:
        raise ValueError(f"{field_name} must be non-empty")
    return text


def _optional_str(value: object) -> str | None:
    """把可选值转成字符串，输入任意对象，输出字符串或 None。"""
    if value is None:
        return None
    text = str(value)
    return text if text else None


__all__ = [
    "McpToolAdapter",
    "McpToolAdapterManager",
    "McpToolAdapterPlan",
    "McpToolAliasConfig",
    "McpToolDescriptor",
    "McpToolRegistration",
    "canonical_tool_name",
]
