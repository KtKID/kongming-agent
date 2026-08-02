"""配置文件 schema 迁移。

本模块服务 ``setting.yaml`` 的持久化 schema 演进：

- 缺少 ``config_schema_version`` 的历史配置按 ``v0`` 处理；
- 当前目标版本是 ``v0.6``；
- 迁移只按版本清单补缺失字段，保留用户显式写入的值；
- v0/v0.5 的静态模型与自定义 preset 一次性迁入 selection + user catalog；
- 写回复用 config writer 的 round-trip 基础能力，保持注释、空行、锁和原子替换。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

from infrastructure.config.errors import ConfigLoadError
from infrastructure.config.model_provider_catalog import (
    CatalogSource,
    ModelProviderCatalog,
    ModelProviderDefinition,
    ModelProviderModelDefinition,
    ProviderProtocol,
    default_model_provider_catalog_path,
    load_model_provider_catalog_document,
)
from infrastructure.config.models import (
    CURRENT_CONFIG_SCHEMA_VERSION,
    ModelSelectionConfig,
)
from infrastructure.config.writer import (
    _count_diff_lines,
    _dump_to_string,
    _lock_fd,
    _lock_path_for,
    _make_tmp_path,
    _unlock_fd,
)

_LEGACY_UNVERSIONED_SCHEMA = "v0"
_PREVIOUS_CONFIG_SCHEMA_VERSION = "v0.5"
_SUPPORTED_SOURCE_VERSIONS = {
    _LEGACY_UNVERSIONED_SCHEMA,
    _PREVIOUS_CONFIG_SCHEMA_VERSION,
    CURRENT_CONFIG_SCHEMA_VERSION,
}
_MODEL_TRANSACTION_MARKER = ".model-config-v06-transaction.json"


@dataclass(frozen=True)
class MigrationResult:
    """配置迁移结果。"""

    yaml_path: Path
    source_version: str
    target_version: str
    migrated: bool
    added_fields: tuple[str, ...]
    removed_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class _FieldMigration:
    """单个 YAML 字段迁移声明。"""

    path: tuple[str, ...]
    value: Any
    comment: str
    requires_existing_parent: tuple[str, ...] = ()


@dataclass(frozen=True)
class _FieldRenameMigration:
    """单个 YAML 字段改名迁移声明。"""

    old_path: tuple[str, ...]
    new_path: tuple[str, ...]
    comment: str


@dataclass(frozen=True)
class _ModelMigrationPlan:
    """v0.6 model selection 与可选 user catalog 的完整写入计划。"""

    catalog_path: Path
    catalog_text: str | None
    added_fields: tuple[str, ...]
    removed_fields: tuple[str, ...]


_WEB_PARENT = ("web",)
_LEGACY_ORIGIN_FIELD = "public_origin"
_REMOVED_FIELDS: tuple[tuple[str, ...], ...] = (
    ("web", _LEGACY_ORIGIN_FIELD),
    ("web", "pending_approval_timeout_seconds"),
    ("mcp", "enabled"),
)
_RENAMED_FIELDS: tuple[_FieldRenameMigration, ...] = (
    _FieldRenameMigration(
        ("web", _LEGACY_ORIGIN_FIELD),
        ("web", "server_origin"),
        "扫码登录和外部客户端 handoff 使用的服务器访问地址；公网填 https://域名，局域网填 http://私网IP:端口。",
    ),
)
_MCP_WEB_SEARCH_BACKFILL_FIELDS: tuple[_FieldMigration, ...] = (
    _FieldMigration(
        ("mcp", "servers"),
        [
            {
                "server_id": "minimax",
                "enabled": True,
                "command": "uvx",
                "args": ["minimax-coding-plan-mcp", "-y"],
                "env": {"MINIMAX_API_HOST": "https://api.minimax.io"},
                "secret_env_keys": ["MINIMAX_API_KEY"],
                "initialize_timeout_ms": 30000,
                "call_timeout_ms": 60000,
            }
        ],
        "stdio MCP server 配置列表。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web_search", "enabled"),
        True,
        "是否启用通用 Web Search provider 装配。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web_search", "provider_name"),
        "minimax_web_search",
        "Web Search provider 名称，会写入搜索结果 diagnostics。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web_search", "search_tool_name"),
        "mcp__minimax__web_search",
        "显式指定底层搜索工具名；为空时按候选列表自动探测。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web_search", "search_tool_names"),
        ["mcp__minimax__web_search", "web_search"],
        "底层搜索工具自动探测候选名列表。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web_search", "max_results"),
        5,
        "单次 Web Search 默认返回结果数。",
        _WEB_PARENT,
    ),
)
_CURRENT_VERSION_BACKFILL_FIELDS: tuple[_FieldMigration, ...] = (
    *_MCP_WEB_SEARCH_BACKFILL_FIELDS,
    _FieldMigration(
        ("web", "server_origin"),
        None,
        "扫码登录和外部客户端 handoff 使用的服务器访问地址；公网填 https://域名，局域网填 http://私网IP:端口。",
        _WEB_PARENT,
    ),
)

_SECTION_COMMENTS: dict[tuple[str, ...], str] = {
    ("mcp",): "MCP 工具注册配置；用于启动 stdio MCP server 并暴露为 Kongming Tool。",
    ("web_search",): "通用 Web Search 工具配置；优先复用 MCP 搜索工具并暴露 web_search 门户。",
    (
        "web",
        "full_log",
    ): "前后端通信全量日志配置，默认关闭；用于排查 Web 运行链路。",
    (
        "web",
        "deep_research_source_provider",
    ): "Deep Research 来源工具配置；Web 运行时按工具名自动探测搜索和读取能力。",
}

_V0_TO_V05_FIELDS: tuple[_FieldMigration, ...] = (
    *_MCP_WEB_SEARCH_BACKFILL_FIELDS,
    _FieldMigration(
        ("web", "server_origin"),
        None,
        "扫码登录和外部客户端 handoff 使用的服务器访问地址；公网填 https://域名，局域网填 http://私网IP:端口。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web", "initial_password"),
        None,
        "首次部署时使用的明文初始密码；长期登录凭据会落到 password store。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web", "ws_heartbeat_interval_ms"),
        30000,
        "前端 WebSocket 前台 ping 间隔，单位毫秒；默认 30 秒。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web", "ws_heartbeat_background_interval_ms"),
        60000,
        "浏览器标签页进入后台后的 ping 间隔，单位毫秒；默认 60 秒。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web", "ws_heartbeat_timeout_ms"),
        10000,
        "单次 pong 等待超时，单位毫秒；默认 10 秒。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web", "ws_heartbeat_max_missed"),
        3,
        "连续丢失 pong 的最大次数；达到后判定连接失活。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web", "full_log", "enabled"),
        False,
        "是否启用前后端通信全量日志；默认关闭。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web", "full_log", "path"),
        ".kongming/logs/full_log.jsonl",
        "全量日志写入路径；.kongming/* 会派生到 kongming_home。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web", "full_log", "rotate_daily"),
        True,
        "是否按自然日切分全量日志文件。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web", "full_log", "include_http_body"),
        False,
        "是否记录 HTTP 请求和响应 body；默认关闭以降低敏感信息风险。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web", "full_log", "queue_size"),
        10000,
        "全量日志异步队列容量；队列满时丢弃最旧记录。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web", "deep_research_source_provider", "enabled"),
        True,
        "是否启用 Web Deep Research 来源工具自动装配。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web", "deep_research_source_provider", "provider_name"),
        "web_user_tool_research_source",
        "写入 Deep Research artifact 的来源 provider 名称。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web", "deep_research_source_provider", "search_tool_name"),
        None,
        "显式指定搜索工具名；留空时按候选列表自动探测。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web", "deep_research_source_provider", "fetch_tool_name"),
        None,
        "显式指定读取工具名；留空时按候选列表自动探测。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web", "deep_research_source_provider", "search_tool_names"),
        ["deep_research_search", "web_search", "search_web", "browser_search"],
        "自动探测搜索工具名列表，按顺序匹配。",
        _WEB_PARENT,
    ),
    _FieldMigration(
        ("web", "deep_research_source_provider", "fetch_tool_names"),
        ["deep_research_fetch", "web_fetch", "fetch_url", "browser_fetch"],
        "自动探测读取工具名列表，按顺序匹配。",
        _WEB_PARENT,
    ),
)


def _normalize_endpoint(value: object) -> str:
    """归一化 endpoint，用于确定性 catalog preset 匹配。"""
    return str(value or "").strip().rstrip("/").lower()


def _infer_legacy_protocol(model: dict[str, Any]) -> ProviderProtocol:
    """从旧显式 provider 或 endpoint 规则推导稳定协议。"""
    declared = model.get("provider")
    if declared == "anthropic":
        return ProviderProtocol.ANTHROPIC
    if declared == "openai_compatible":
        return ProviderProtocol.OPENAI
    base_url = str(model.get("base_url") or "")
    parsed = urlparse(base_url)
    segments = [segment.lower() for segment in parsed.path.split("/") if segment]
    if (parsed.hostname or "").lower() == "api.anthropic.com" or "anthropic" in segments:
        return ProviderProtocol.ANTHROPIC
    return ProviderProtocol.OPENAI


def _default_header(protocol: ProviderProtocol) -> str:
    """返回旧配置缺省鉴权 header。"""
    if protocol is ProviderProtocol.ANTHROPIC:
        return "x-api-key"
    return "authorization-bearer"


def _effective_catalog_values(
    provider: ModelProviderDefinition,
    model: ModelProviderModelDefinition,
) -> tuple[ProviderProtocol, str, str, str]:
    """返回用于旧配置匹配的协议、endpoint、remote model 与 header。"""
    return (
        provider.protocol,
        _normalize_endpoint(model.base_url or provider.default_base_url),
        model.model.strip().lower(),
        str(
            model.api_key_header
            or provider.default_api_key_header
            or _default_header(provider.protocol)
        ),
    )


def _match_builtin_preset(model: dict[str, Any]) -> str | None:
    """按协议、endpoint、remote model 和 header 匹配内置 preset。"""
    protocol = _infer_legacy_protocol(model)
    header = str(model.get("api_key_header") or _default_header(protocol))
    target = (
        protocol,
        _normalize_endpoint(model.get("base_url")),
        str(model.get("name") or "").strip().lower(),
        header,
    )
    builtin = load_model_provider_catalog_document(
        default_model_provider_catalog_path(),
        source=CatalogSource.BUILTIN,
    )
    for provider in builtin.providers:
        for catalog_model in provider.models:
            if _effective_catalog_values(provider, catalog_model) == target:
                return catalog_model.preset_id
    return None


def _stable_custom_suffix(model: dict[str, Any]) -> str:
    """根据静态模型定义生成稳定短哈希。"""
    stable = {
        "protocol": _infer_legacy_protocol(model).value,
        "base_url": _normalize_endpoint(model.get("base_url")),
        "model": str(model.get("name") or "").strip(),
        "api_key_header": model.get("api_key_header"),
        "timeout": model.get("timeout", 60),
        "max_tokens": model.get("max_tokens", 4096),
        "temperature": model.get("temperature", 0.7),
    }
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def _matching_legacy_reasoning(
    model: dict[str, Any],
    model_name: str,
) -> dict[str, Any] | None:
    """把旧 prefix/exact profile 固化为当前模型的直接 capability。"""
    profiles = model.get("reasoning_profiles")
    if not isinstance(profiles, dict):
        return None
    exact: dict[str, Any] | None = None
    prefix: dict[str, Any] | None = None
    lowered = model_name.lower()
    for key, raw_profile in profiles.items():
        if not isinstance(raw_profile, dict):
            continue
        match = raw_profile.get("match", "exact")
        if match == "exact" and str(key).lower() == lowered:
            exact = raw_profile
            break
        if prefix is None and match == "prefix" and lowered.startswith(str(key).lower()):
            prefix = raw_profile
    profile = exact or prefix
    if profile is None:
        return None
    capability = {key: value for key, value in profile.items() if key != "match"}
    adapter = str(capability.get("adapter") or "none")
    capability["supports_disabled"] = adapter != "none" and (
        adapter != "configurable_patch" or capability.get("disabled_patch") is not None
    )
    default_effort = model.get("reasoning_effort")
    if default_effort is not None:
        capability["default_effort"] = default_effort
    return capability


def _custom_provider_definition(model: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """把未匹配旧模型转换为完整 user catalog provider。"""
    suffix = _stable_custom_suffix(model)
    provider_id = f"custom-{suffix}"
    preset_id = f"custom-model-{suffix}"
    protocol = _infer_legacy_protocol(model)
    base_url = str(model.get("base_url") or "").strip().rstrip("/")
    model_name = str(model.get("name") or "").strip()
    header = str(model.get("api_key_header") or _default_header(protocol))
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    is_local = host in {"localhost", "0.0.0.0", "::1", "host.docker.internal"} or host.startswith(
        "127."
    )
    configured_env = str(model.get("api_key_env") or "").strip()
    env_name = (
        None if is_local else configured_env or f"KONGMING_PROVIDER_CUSTOM_{suffix.upper()}_API_KEY"
    )
    catalog_model: dict[str, Any] = {
        "preset_id": preset_id,
        "display_name": model_name,
        "model": model_name,
    }
    reasoning = _matching_legacy_reasoning(model, model_name)
    if reasoning is not None:
        catalog_model["reasoning"] = reasoning
    provider = {
        "provider_id": provider_id,
        "default_preset_id": preset_id,
        "display_name": model_name,
        "region_label": "Local" if is_local else "Custom",
        "description": "Migrated custom model provider.",
        "logo_text": "C",
        "protocol": protocol.value,
        "default_base_url": base_url,
        "default_api_key_env": env_name,
        "default_api_key_header": header,
        "request_defaults": {
            "timeout_seconds": model.get("timeout", 60),
            "max_tokens": model.get("max_tokens", 4096),
            "temperature": model.get("temperature", 0.7),
        },
        "models": [catalog_model],
        "match_keywords": [model_name],
        "match_hosts": [host] if host else [],
    }
    return preset_id, provider


def _read_existing_user_catalog(path: Path) -> list[dict[str, Any]]:
    """读取并校验既有 user catalog，文件缺失时返回空列表。"""
    if not path.exists():
        return []
    yaml = YAML(typ="safe")
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"))
        catalog = ModelProviderCatalog.model_validate(raw)
    except Exception as exc:
        raise ConfigLoadError(
            f"invalid user model catalog at {path}: {exc}",
            details={"path": str(path), "code": "catalog_invalid"},
        ) from exc
    return [provider.model_dump(mode="json") for provider in catalog.providers]


def _dump_catalog(providers: list[dict[str, Any]]) -> str:
    """校验 user catalog 及其与内置 catalog 的完整替换合并结果。"""
    catalog = ModelProviderCatalog.model_validate({"version": 2, "providers": providers})
    builtin = load_model_provider_catalog_document(
        default_model_provider_catalog_path(),
        source=CatalogSource.BUILTIN,
    )
    replacements = {provider.provider_id.lower(): provider for provider in catalog.providers}
    merged = [
        replacements.get(provider.provider_id.lower(), provider) for provider in builtin.providers
    ]
    builtin_ids = {provider.provider_id.lower() for provider in builtin.providers}
    merged.extend(
        provider for provider_id, provider in replacements.items() if provider_id not in builtin_ids
    )
    ModelProviderCatalog.model_validate({"version": 2, "providers": merged})
    yaml = YAML(typ="rt")
    yaml.default_flow_style = False
    return _dump_to_string(yaml, catalog.model_dump(mode="json"))


def _prepare_model_v06_migration(
    doc: CommentedMap,
    yaml_path: Path,
) -> _ModelMigrationPlan:
    """把旧 model/web preset 计算为最小 selection 与可选 user catalog。"""
    raw_model = doc.get("model")
    if not isinstance(raw_model, dict):
        raise ConfigLoadError(
            "v0.6 model migration requires a model mapping",
            details={"path": str(yaml_path), "code": "migration_incomplete"},
        )
    legacy_model = dict(raw_model)
    preset_id = _match_builtin_preset(legacy_model)
    user_catalog_path = yaml_path.parent / "model-providers.yaml"
    catalog_text: str | None = None
    providers: list[dict[str, Any]] | None = None

    def upsert_custom_model(model: dict[str, Any]) -> str:
        """把一条旧静态模型加入用户 catalog，返回稳定 preset ID。"""
        nonlocal providers
        matched = _match_builtin_preset(model)
        if matched is not None:
            return matched
        custom_preset_id, provider = _custom_provider_definition(model)
        if providers is None:
            providers = _read_existing_user_catalog(user_catalog_path)
        providers = [
            item for item in providers if item.get("provider_id") != provider["provider_id"]
        ]
        providers.append(provider)
        return custom_preset_id

    if preset_id is None:
        preset_id = upsert_custom_model(legacy_model)

    web = doc.get("web")
    if isinstance(web, dict):
        raw_presets = web.get("llm_presets")
        if isinstance(raw_presets, list):
            for raw_preset in raw_presets:
                if not isinstance(raw_preset, dict):
                    continue
                static_model = {
                    "provider": raw_preset.get("provider"),
                    "base_url": raw_preset.get("base_url"),
                    "name": raw_preset.get("model"),
                    "api_key_env": raw_preset.get("api_key_env"),
                    "api_key_header": raw_preset.get("api_key_header"),
                    "reasoning_effort": raw_preset.get("reasoning_effort"),
                    "max_tokens": raw_preset.get("max_tokens", 4096),
                    "temperature": raw_preset.get("temperature", 0.7),
                    "timeout": raw_preset.get("timeout", 60),
                }
                if static_model["base_url"] and static_model["name"]:
                    upsert_custom_model(static_model)

    for section_path in (("evolution", "learning"), ("sitian", "analyzer")):
        section = _mapping_at_path(doc, section_path)
        if section is None:
            continue
        legacy_name = section.get("model_name")
        legacy_url = section.get("base_url")
        if isinstance(legacy_name, str) and legacy_name.strip() and legacy_url:
            section["preset_id"] = upsert_custom_model(
                {
                    "provider": section.get("provider"),
                    "base_url": legacy_url,
                    "name": legacy_name,
                    "api_key_env": section.get("api_key_env"),
                    "reasoning_effort": section.get("reasoning_effort"),
                    "max_tokens": section.get("max_tokens", 4096),
                    "temperature": section.get("temperature", 0.7),
                    "timeout": section.get("timeout", 60),
                }
            )
        for retired_field in (
            "model_name",
            "base_url",
            "api_key_env",
            "provider",
            "max_tokens",
            "temperature",
            "timeout",
        ):
            section.pop(retired_field, None)

    if providers is not None:
        catalog_text = _dump_catalog(providers)

    effort = legacy_model.get("reasoning_effort")
    try:
        selection = ModelSelectionConfig(
            preset_id=preset_id,
            reasoning_effort=effort,
        )
    except Exception as exc:
        raise ConfigLoadError(
            f"failed to migrate model selection: {exc}",
            details={"path": str(yaml_path), "code": "migration_incomplete"},
        ) from exc
    old_fields = tuple(f"model.{field}" for field in raw_model if field not in {"preset_id"})
    doc["model"] = CommentedMap(selection.model_dump(mode="json"))
    removed = list(old_fields)
    if isinstance(web, dict) and "llm_presets" in web:
        del web["llm_presets"]
        removed.append("web.llm_presets")
    return _ModelMigrationPlan(
        catalog_path=user_catalog_path,
        catalog_text=catalog_text,
        added_fields=("model.preset_id",),
        removed_fields=tuple(removed),
    )


def _fsync_file(path: Path) -> None:
    """把已写文件内容同步到磁盘。"""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    """同步目录项更新。"""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_prepared_file(source: Path, target: Path) -> None:
    """原子替换已 fsync 的临时文件；独立入口便于故障注入测试。"""
    os.replace(source, target)


def _write_marker(path: Path, payload: dict[str, Any]) -> None:
    """原子写入 transaction marker。"""
    marker_tmp = path.with_suffix(path.suffix + ".tmp")
    marker_tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    _fsync_file(marker_tmp)
    os.replace(marker_tmp, path)
    _fsync_directory(path.parent)


def _cleanup_transaction_files(paths: tuple[Path, ...]) -> None:
    """清理已完成或已恢复事务的 marker、备份与临时文件。"""
    for path in paths:
        with contextlib.suppress(OSError):
            path.unlink()


def _recover_model_v06_transaction(yaml_path: Path) -> None:
    """启动时发现残留 marker 后恢复迁移前 setting/catalog。"""
    marker_path = yaml_path.parent / _MODEL_TRANSACTION_MARKER
    if not marker_path.exists():
        return
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
        setting_path = Path(payload["setting_path"])
        catalog_path = Path(payload["catalog_path"])
        setting_backup = Path(payload["setting_backup"])
        catalog_backup = Path(payload["catalog_backup"])
        catalog_existed = bool(payload["catalog_existed"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigLoadError(
            f"invalid model migration transaction marker at {marker_path}: {exc}",
            details={"path": str(marker_path), "code": "migration_incomplete"},
        ) from exc

    try:
        if setting_backup.exists():
            os.replace(setting_backup, setting_path)
        if catalog_existed and catalog_backup.exists():
            os.replace(catalog_backup, catalog_path)
        elif not catalog_existed and catalog_path.exists():
            catalog_path.unlink()
        _fsync_directory(yaml_path.parent)
    except OSError as exc:
        raise ConfigLoadError(
            f"failed to recover model migration transaction: {exc}",
            details={"path": str(marker_path), "code": "migration_incomplete"},
        ) from exc
    finally:
        _cleanup_transaction_files(
            (
                marker_path,
                setting_backup,
                catalog_backup,
                yaml_path.parent / ".setting.yaml.v06.tmp",
                yaml_path.parent / ".model-providers.yaml.v06.tmp",
            )
        )


def _commit_model_v06_transaction(
    *,
    yaml_path: Path,
    setting_text: str,
    catalog_path: Path,
    catalog_text: str,
) -> None:
    """以 marker + 双备份提交 setting 与 user catalog 两文件事务。"""
    marker_path = yaml_path.parent / _MODEL_TRANSACTION_MARKER
    setting_tmp = yaml_path.parent / ".setting.yaml.v06.tmp"
    catalog_tmp = yaml_path.parent / ".model-providers.yaml.v06.tmp"
    setting_backup = yaml_path.parent / ".setting.yaml.v06.bak"
    catalog_backup = yaml_path.parent / ".model-providers.yaml.v06.bak"
    catalog_existed = catalog_path.exists()

    setting_backup.write_bytes(yaml_path.read_bytes())
    _fsync_file(setting_backup)
    if catalog_existed:
        catalog_backup.write_bytes(catalog_path.read_bytes())
        _fsync_file(catalog_backup)
    setting_tmp.write_text(setting_text, encoding="utf-8")
    catalog_tmp.write_text(catalog_text, encoding="utf-8")
    _fsync_file(setting_tmp)
    _fsync_file(catalog_tmp)
    marker = {
        "version": 1,
        "state": "prepared",
        "setting_path": str(yaml_path),
        "catalog_path": str(catalog_path),
        "setting_backup": str(setting_backup),
        "catalog_backup": str(catalog_backup),
        "catalog_existed": catalog_existed,
    }
    _write_marker(marker_path, marker)

    try:
        _replace_prepared_file(catalog_tmp, catalog_path)
        marker["state"] = "catalog_replaced"
        _write_marker(marker_path, marker)
        _replace_prepared_file(setting_tmp, yaml_path)
        _fsync_directory(yaml_path.parent)
    except OSError:
        if setting_backup.exists():
            os.replace(setting_backup, yaml_path)
        if catalog_existed and catalog_backup.exists():
            os.replace(catalog_backup, catalog_path)
        elif not catalog_existed and catalog_path.exists():
            catalog_path.unlink()
        _fsync_directory(yaml_path.parent)
        raise
    finally:
        _cleanup_transaction_files(
            (
                marker_path,
                setting_tmp,
                catalog_tmp,
                setting_backup,
                catalog_backup,
            )
        )


def migrate_config_if_needed(yaml_path: Path) -> MigrationResult:
    """按当前 schema 补齐 ``setting.yaml``，必要时原子写回。

    Args:
        yaml_path: 需要检查和迁移的配置文件路径。

    Returns:
        :class:`MigrationResult`，包含源版本、目标版本和补齐字段路径。

    Raises:
        ConfigLoadError: 文件不存在、YAML 非 mapping、未知版本或写回失败。
    """
    _recover_model_v06_transaction(yaml_path)
    if not yaml_path.exists():
        raise ConfigLoadError(
            f"config file not found: {yaml_path}",
            details={"path": str(yaml_path)},
        )

    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    lock_path = _lock_path_for(yaml_path)
    lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    tmp_path: Path | None = None
    try:
        _lock_fd(lock_fd)
        try:
            with yaml_path.open("r", encoding="utf-8") as f:
                doc = yaml.load(f)
        except YAMLError as exc:
            raise ConfigLoadError(
                f"failed to parse YAML at {yaml_path}: {exc}",
                details={"path": str(yaml_path)},
            ) from exc
        if doc is None:
            doc = CommentedMap()
        if not isinstance(doc, CommentedMap):
            raise ConfigLoadError(
                f"config root must be a mapping, got {type(doc).__name__} at {yaml_path}",
                details={"path": str(yaml_path)},
            )

        source_version = _read_source_version(doc, yaml_path)
        original_text = yaml_path.read_text(encoding="utf-8")
        added_fields: list[str] = []
        removed_fields: list[str] = []
        model_plan: _ModelMigrationPlan | None = None

        if source_version != CURRENT_CONFIG_SCHEMA_VERSION:
            _set_schema_version(doc)
            if source_version == _LEGACY_UNVERSIONED_SCHEMA:
                added_fields.append("config_schema_version")
        rename_added, rename_removed = _apply_field_renames(doc, _RENAMED_FIELDS)
        added_fields.extend(rename_added)
        removed_fields.extend(rename_removed)
        if source_version == _LEGACY_UNVERSIONED_SCHEMA:
            added_fields.extend(_apply_v0_to_v05(doc))
        if source_version in {_LEGACY_UNVERSIONED_SCHEMA, _PREVIOUS_CONFIG_SCHEMA_VERSION}:
            model_plan = _prepare_model_v06_migration(doc, yaml_path)
            added_fields.extend(model_plan.added_fields)
            removed_fields.extend(model_plan.removed_fields)
        added_fields.extend(_apply_current_version_backfills(doc))
        removed_fields.extend(_remove_removed_fields(doc))
        added_fields.extend(_normalize_mcp_server_comment(doc, original_text))

        if not added_fields and not removed_fields:
            return MigrationResult(
                yaml_path=yaml_path,
                source_version=source_version,
                target_version=CURRENT_CONFIG_SCHEMA_VERSION,
                migrated=False,
                added_fields=(),
                removed_fields=(),
            )

        new_text = _strip_retired_mcp_global_switch_comment(
            _schema_version_first(_dump_to_string(yaml, doc))
        )
        if model_plan is not None and model_plan.catalog_text is not None:
            _commit_model_v06_transaction(
                yaml_path=yaml_path,
                setting_text=new_text,
                catalog_path=model_plan.catalog_path,
                catalog_text=model_plan.catalog_text,
            )
        else:
            tmp_path = _make_tmp_path(yaml_path)
            tmp_path.write_text(new_text, encoding="utf-8")
            _fsync_file(tmp_path)
            _replace_prepared_file(tmp_path, yaml_path)
            _fsync_directory(yaml_path.parent)
        _count_diff_lines(original_text, new_text)
        return MigrationResult(
            yaml_path=yaml_path,
            source_version=source_version,
            target_version=CURRENT_CONFIG_SCHEMA_VERSION,
            migrated=True,
            added_fields=tuple(dict.fromkeys(added_fields)),
            removed_fields=tuple(dict.fromkeys(removed_fields)),
        )
    except ConfigLoadError:
        raise
    except OSError as exc:
        raise ConfigLoadError(
            f"failed to migrate config file {yaml_path}: {exc}",
            details={"path": str(yaml_path), "cause": type(exc).__name__},
        ) from exc
    finally:
        try:
            _unlock_fd(lock_fd)
        finally:
            os.close(lock_fd)
        if tmp_path is not None and tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()


def _read_source_version(doc: CommentedMap, yaml_path: Path) -> str:
    """读取源 schema 版本；缺失时按历史 ``v0`` 处理。"""
    raw = doc.get("config_schema_version")
    if raw is None:
        return _LEGACY_UNVERSIONED_SCHEMA
    if not isinstance(raw, str):
        raise ConfigLoadError(
            "config_schema_version must be a string",
            details={"path": str(yaml_path), "value": repr(raw)},
        )
    if raw not in _SUPPORTED_SOURCE_VERSIONS:
        raise ConfigLoadError(
            f"unsupported config_schema_version {raw!r}; supported: {sorted(_SUPPORTED_SOURCE_VERSIONS)}",
            details={"path": str(yaml_path), "value": raw},
        )
    return raw


def _set_schema_version(doc: CommentedMap) -> None:
    """把当前版本字段写到 YAML 顶部。"""
    if "config_schema_version" in doc:
        doc["config_schema_version"] = CURRENT_CONFIG_SCHEMA_VERSION
    else:
        doc.insert(0, "config_schema_version", CURRENT_CONFIG_SCHEMA_VERSION)
    doc.yaml_add_eol_comment(
        "配置结构版本；当前版本为 v0.6。",
        key="config_schema_version",
    )


def _schema_version_first(text: str) -> str:
    """确保版本声明是文件第一行，文件头注释整体下移保留。"""
    lines = text.splitlines(keepends=True)
    version_prefix = "config_schema_version:"
    version_index = next(
        (idx for idx, line in enumerate(lines) if line.startswith(version_prefix)),
        None,
    )
    if version_index is None:
        return text
    if version_index == 0:
        return text
    version_line = lines.pop(version_index)
    return "".join([version_line, *lines])


def _strip_retired_mcp_global_switch_comment(text: str) -> str:
    """删除退役 MCP 总开关注释，输入 YAML 文本，输出清理后的文本。"""
    return text.replace("# 是否启用 MCP client 启动与工具注册。\n", "")


def _apply_v0_to_v05(doc: CommentedMap) -> list[str]:
    """执行 v0 历史字段补齐，再由 v0.6 模型迁移收口。"""
    return _apply_field_migrations(doc, _V0_TO_V05_FIELDS)


def _apply_current_version_backfills(doc: CommentedMap) -> list[str]:
    """补齐当前版本历史用户配置中缺失的关键字段。"""
    return _apply_field_migrations(doc, _CURRENT_VERSION_BACKFILL_FIELDS)


def _apply_field_renames(
    doc: CommentedMap,
    migrations: tuple[_FieldRenameMigration, ...],
) -> tuple[list[str], list[str]]:
    """按改名迁移声明搬运旧值，返回新增路径和删除路径。"""
    added: list[str] = []
    removed: list[str] = []
    for migration in migrations:
        old_parent = _mapping_at_path(doc, migration.old_path[:-1])
        if old_parent is None:
            continue
        old_leaf = migration.old_path[-1]
        if old_leaf not in old_parent:
            continue

        if not _path_exists(doc, migration.new_path):
            new_parent = _ensure_parent_map(doc, migration.new_path[:-1])
            if new_parent is None:
                continue
            new_leaf = migration.new_path[-1]
            new_parent[new_leaf] = old_parent[old_leaf]
            _set_before_comment(new_parent, new_leaf, migration.comment)
            added.append(".".join(migration.new_path))

        del old_parent[old_leaf]
        removed.append(".".join(migration.old_path))
    return added, removed


def _apply_field_migrations(
    doc: CommentedMap,
    migrations: tuple[_FieldMigration, ...],
) -> list[str]:
    """按字段迁移声明补齐缺失字段，返回新增路径。"""
    added: list[str] = []
    for migration in migrations:
        if (
            migration.requires_existing_parent
            and _mapping_at_path(doc, migration.requires_existing_parent) is None
        ):
            continue
        if _path_exists(doc, migration.path):
            continue
        parent = _ensure_parent_map(doc, migration.path[:-1])
        if parent is None:
            continue
        leaf = migration.path[-1]
        parent[leaf] = _to_yaml_value(migration.value)
        _set_before_comment(parent, leaf, migration.comment)
        added.append(".".join(migration.path))
    return added


def _remove_removed_fields(doc: CommentedMap) -> list[str]:
    """删除已退役的 YAML 字段，返回被删除路径。"""
    removed: list[str] = []
    for path in _REMOVED_FIELDS:
        parent = _mapping_at_path(doc, path[:-1])
        if parent is None:
            continue
        leaf = path[-1]
        if leaf in parent:
            del parent[leaf]
            removed.append(".".join(path))
    return removed


def _normalize_mcp_server_comment(doc: CommentedMap, original_text: str) -> list[str]:
    """清理旧 MCP 总开关注释，返回被更新的注释路径。"""
    if "是否启用 MCP client 启动与工具注册。" not in original_text:
        return []
    mcp = _mapping_at_path(doc, ("mcp",))
    if mcp is None or "servers" not in mcp:
        return []
    _set_before_comment(mcp, "servers", "stdio MCP server 配置列表。")
    return ["mcp.servers.comment"]


def _path_exists(root: CommentedMap, path: tuple[str, ...]) -> bool:
    """判断 YAML 路径是否已存在。"""
    current: Any = root
    for key in path:
        if not isinstance(current, CommentedMap) or key not in current:
            return False
        current = current[key]
    return True


def _mapping_at_path(root: CommentedMap, path: tuple[str, ...]) -> CommentedMap | None:
    """读取路径上的 mapping 节点，缺失或类型不匹配时返回 None。"""
    current: Any = root
    for key in path:
        if not isinstance(current, CommentedMap) or key not in current:
            return None
        current = current[key]
    if not isinstance(current, CommentedMap):
        return None
    return current


def _ensure_parent_map(root: CommentedMap, path: tuple[str, ...]) -> CommentedMap | None:
    """确保叶子字段的父 mapping 存在，遇到用户写入的非 mapping 时放弃迁移该字段。"""
    current = root
    for index, key in enumerate(path):
        section_path = path[: index + 1]
        if key not in current:
            current[key] = CommentedMap()
            _set_before_comment(current, key, _SECTION_COMMENTS.get(section_path, ""))
        child = current[key]
        if not isinstance(child, CommentedMap):
            return None
        current = child
    return current


def _set_before_comment(node: CommentedMap, key: str, comment: str) -> None:
    """给新增字段写入中文前置注释。"""
    if comment:
        node.yaml_set_comment_before_after_key(key, before=comment)


def _to_yaml_value(value: Any) -> Any:
    """把迁移默认值转成 ruamel 可写的普通结构。"""
    if isinstance(value, dict):
        mapped = CommentedMap()
        for key, item in value.items():
            mapped[key] = _to_yaml_value(item)
        return mapped
    if isinstance(value, list | tuple):
        return [_to_yaml_value(item) for item in value]
    return value


__all__ = ["MigrationResult", "migrate_config_if_needed"]
