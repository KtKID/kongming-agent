"""配置文件 schema 迁移。

本模块服务 ``setting.yaml`` 的持久化 schema 演进：

- 缺少 ``config_schema_version`` 的历史配置按 ``v0`` 处理；
- 当前目标版本是 ``v0.5``；
- 迁移只按版本清单补缺失字段，保留用户显式写入的值；
- 写回复用 config writer 的 round-trip 基础能力，保持注释、空行、锁和原子替换。
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

from infrastructure.config.errors import ConfigLoadError
from infrastructure.config.models import CURRENT_CONFIG_SCHEMA_VERSION
from infrastructure.config.writer import (
    _count_diff_lines,
    _dump_to_string,
    _lock_fd,
    _lock_path_for,
    _make_tmp_path,
    _unlock_fd,
)

_LEGACY_UNVERSIONED_SCHEMA = "v0"
_SUPPORTED_SOURCE_VERSIONS = {_LEGACY_UNVERSIONED_SCHEMA, CURRENT_CONFIG_SCHEMA_VERSION}


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


_WEB_PARENT = ("web",)
# 这是 web.public_origin 在 v0.5.x 退役字段，迁移时转存到 server_origin 后清理。
_LEGACY_ORIGIN_FIELD = "public_origin"
_REMOVED_FIELDS: tuple[tuple[str, ...], ...] = (("web", _LEGACY_ORIGIN_FIELD),)
_CURRENT_VERSION_BACKFILL_FIELDS: tuple[_FieldMigration, ...] = (
    _FieldMigration(
        ("web", "server_origin"),
        None,
        "扫码登录和外部客户端 handoff 使用的服务器访问地址；公网填 https://域名，局域网填 http://私网IP:端口。",
        _WEB_PARENT,
    ),
)

_SECTION_COMMENTS: dict[tuple[str, ...], str] = {
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


def migrate_config_if_needed(yaml_path: Path) -> MigrationResult:
    """按当前 schema 补齐 ``setting.yaml``，必要时原子写回。

    Args:
        yaml_path: 需要检查和迁移的配置文件路径。

    Returns:
        :class:`MigrationResult`，包含源版本、目标版本和补齐字段路径。

    Raises:
        ConfigLoadError: 文件不存在、YAML 非 mapping、未知版本或写回失败。
    """
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

        if source_version == _LEGACY_UNVERSIONED_SCHEMA:
            _set_schema_version(doc)
            added_fields.append("config_schema_version")
        added_fields.extend(_rename_legacy_origin_field(doc))
        if source_version == _LEGACY_UNVERSIONED_SCHEMA:
            added_fields.extend(_apply_v0_to_v05(doc))
        added_fields.extend(_apply_current_version_backfills(doc))
        removed_fields.extend(_remove_removed_fields(doc))

        if not added_fields and not removed_fields:
            return MigrationResult(
                yaml_path=yaml_path,
                source_version=source_version,
                target_version=CURRENT_CONFIG_SCHEMA_VERSION,
                migrated=False,
                added_fields=(),
                removed_fields=(),
            )

        tmp_path = _make_tmp_path(yaml_path)
        new_text = _schema_version_first(_dump_to_string(yaml, doc))
        tmp_path.write_text(new_text, encoding="utf-8")
        tmp_path.replace(yaml_path)
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
        "配置结构版本；当前版本为 v0.5。",
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


def _apply_v0_to_v05(doc: CommentedMap) -> list[str]:
    """执行 v0 到 v0.5 的显式字段迁移。"""
    return _apply_field_migrations(doc, _V0_TO_V05_FIELDS)


def _apply_current_version_backfills(doc: CommentedMap) -> list[str]:
    """补齐当前版本历史用户配置中缺失的关键字段。"""
    return _apply_field_migrations(doc, _CURRENT_VERSION_BACKFILL_FIELDS)


def _rename_legacy_origin_field(doc: CommentedMap) -> list[str]:
    """把旧 public_origin 值迁移到 server_origin，返回新增路径。"""
    web = _mapping_at_path(doc, _WEB_PARENT)
    if web is None:
        return []
    if "server_origin" in web or _LEGACY_ORIGIN_FIELD not in web:
        return []
    web["server_origin"] = _to_yaml_value(web[_LEGACY_ORIGIN_FIELD])
    _set_before_comment(
        web,
        "server_origin",
        "扫码登录和外部客户端 handoff 使用的服务器访问地址；公网填 https://域名，局域网填 http://私网IP:端口。",
    )
    return ["web.server_origin"]


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
