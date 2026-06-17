"""宿主无关配置管理门户。

本模块把配置读取、配置写回和 `.env` 写回收口到
:class:`ConfigManager`。Web dashboard、模型服务商后端和后续配置写回场景
都通过这里触达配置文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from infrastructure.config import schema, writer
from infrastructure.config.env_writer import EnvWriteResult, write_env_values
from infrastructure.config.loader import _ENV_FIELD_PATHS as _LOADER_ENV_FIELD_PATHS
from infrastructure.config.loader import load_config
from infrastructure.config.migrations import migrate_config_if_needed
from infrastructure.config.models import LLMPresetConfig
from infrastructure.config.schema import FieldMeta, FieldSource
from infrastructure.config.writer import (
    ConflictError,
    PatchItem,
    ValidationFailedError,
    WriteResult,
)

# scheduler 字段：loader._apply_scheduler_env_overrides 硬编码了一组额外的
# KONGMING_SCHEDULER_* env，不进 _ENV_FIELD_PATHS。这里显式补齐，让
# read_effective 的 sources 判定能覆盖到。
_SCHEDULER_EXTRA_ENV_FIELDS: tuple[tuple[str, ...], ...] = (
    ("scheduler", "enabled"),
    ("scheduler", "interval"),
    ("scheduler", "max_inflight"),
    ("scheduler", "approval", "mode"),
    ("scheduler", "default_max_turns"),
)

_ENV_FIELD_PATHS: tuple[tuple[str, ...], ...] = tuple(
    list(_LOADER_ENV_FIELD_PATHS) + list(_SCHEDULER_EXTRA_ENV_FIELDS)
)

_ENV_PREFIX = "KONGMING_"

_DICT_LEAF_PATHS: frozenset[str] = frozenset(
    {
        "model.provider_routing",
        "model.reasoning_profiles",
    }
)


@dataclass(frozen=True)
class SchemaResponse:
    """``read_schema`` 响应：字段元数据 + group 显示顺序。"""

    fields: list[FieldMeta]
    groups: list[dict[str, str]]


@dataclass(frozen=True)
class EffectiveResponse:
    """``read_effective`` 响应：每字段当前生效值 + 来源 + 命中的 env 列表。"""

    values: dict[str, Any]
    sources: dict[str, FieldSource]
    env_overrides: list[str]


@dataclass(frozen=True)
class RawResponse:
    """``read_raw`` 响应：yaml 文件原文 + path + mtime。"""

    path: str
    mtime: float
    content: str


@dataclass(frozen=True)
class SavePatchResponse:
    """``save_patch`` 响应：成功时返回新 mtime + 需重启字段清单。"""

    ok: bool
    new_mtime: float
    restart_required_fields: list[str]


@dataclass(frozen=True)
class PresetWriteResponse:
    """`web.llm_presets` 写回响应。"""

    ok: bool
    preset_id: str
    new_mtime: float
    preset_count: int


@dataclass(frozen=True)
class EnvWriteResponse:
    """`.env` 写回响应。"""

    ok: bool
    path: str
    updated_keys: list[str]
    new_mtime: float


class ConfigManager:
    """宿主无关配置管理入口。"""

    def __init__(self, yaml_path: Path, env_path: Path | None = None) -> None:
        """构造配置管理器。

        Args:
            yaml_path: ``setting.yaml`` 绝对路径。
            env_path: ``.env`` 绝对路径。默认按 ``setting.yaml`` 布局推导。
        """
        self._yaml_path = yaml_path
        self._env_path = env_path or _default_env_path(yaml_path)

    def read_schema(self) -> SchemaResponse:
        """返回字段元数据 + group 显示顺序。"""
        return SchemaResponse(
            fields=schema.list_field_metas(),
            groups=schema.list_groups(),
        )

    def read_effective(self) -> EffectiveResponse:
        """加载当前 yaml + env 覆盖后的 Config，扁平化成 dot-path → value。"""
        cfg = load_config(self._yaml_path)
        dumped = cfg.model_dump(mode="json")
        values: dict[str, Any] = {}
        for path, value in _flatten_dict(dumped):
            values[path] = value

        yaml_paths = _yaml_explicit_paths(self._yaml_path)

        import os

        env_overrides: list[str] = []
        env_path_set: set[str] = set()
        for parts in _ENV_FIELD_PATHS:
            env_name = _ENV_PREFIX + "_".join(p.upper() for p in parts)
            if env_name in os.environ:
                dot_path = ".".join(parts)
                env_overrides.append(dot_path)
                env_path_set.add(dot_path)

        sources: dict[str, FieldSource] = {}
        for path in values:
            if path in env_path_set:
                sources[path] = "env"
            elif path in yaml_paths:
                sources[path] = "yaml"
            else:
                sources[path] = "default"

        return EffectiveResponse(
            values=values,
            sources=sources,
            env_overrides=env_overrides,
        )

    def read_raw(self) -> RawResponse:
        """读 yaml 文件原文 + path + mtime。"""
        migrate_config_if_needed(self._yaml_path)
        content = self._yaml_path.read_text(encoding="utf-8")
        mtime = self._yaml_path.stat().st_mtime
        return RawResponse(
            path=str(self._yaml_path),
            mtime=mtime,
            content=content,
        )

    def save_patch(self, patch: list[PatchItem], expected_mtime: float) -> SavePatchResponse:
        """差量写回 + 校验 + 计算 restart_required_fields。"""
        result: WriteResult = writer.round_trip_update(self._yaml_path, patch, expected_mtime)
        restart_fields: list[str] = []
        for item in patch:
            meta = schema.get_field_meta(item.path)
            if meta is not None and meta.restart_required:
                restart_fields.append(item.path)
        return SavePatchResponse(
            ok=True,
            new_mtime=result.new_mtime,
            restart_required_fields=restart_fields,
        )

    def upsert_web_llm_preset(
        self,
        preset: LLMPresetConfig,
        expected_mtime: float | None = None,
    ) -> PresetWriteResponse:
        """向 `web.llm_presets` upsert 一条 preset。"""
        presets = _read_web_llm_presets(self._yaml_path)
        preset_data = preset.model_dump(mode="json", exclude_none=True)
        replaced = False
        for idx, item in enumerate(presets):
            if item.get("id") == preset.id:
                presets[idx] = preset_data
                replaced = True
                break
        if not replaced:
            presets.append(preset_data)
        mtime = self._yaml_path.stat().st_mtime if expected_mtime is None else expected_mtime
        result = writer.round_trip_update(
            self._yaml_path,
            [PatchItem(path="web.llm_presets", value=presets)],
            mtime,
            create_missing_paths={"web.llm_presets"},
            load_env_file=True,
        )
        return PresetWriteResponse(
            ok=True,
            preset_id=preset.id,
            new_mtime=result.new_mtime,
            preset_count=len(presets),
        )

    def remove_web_llm_preset(
        self,
        preset_id: str,
        expected_mtime: float | None = None,
    ) -> PresetWriteResponse:
        """从 `web.llm_presets` 删除一条 preset。"""
        presets = [
            item for item in _read_web_llm_presets(self._yaml_path) if item.get("id") != preset_id
        ]
        mtime = self._yaml_path.stat().st_mtime if expected_mtime is None else expected_mtime
        result = writer.round_trip_update(
            self._yaml_path,
            [PatchItem(path="web.llm_presets", value=presets)],
            mtime,
            create_missing_paths={"web.llm_presets"},
            load_env_file=True,
        )
        return PresetWriteResponse(
            ok=True,
            preset_id=preset_id,
            new_mtime=result.new_mtime,
            preset_count=len(presets),
        )

    def write_env_values(self, values: dict[str, str]) -> EnvWriteResponse:
        """写入 `.env` 文件并同步当前进程。"""
        result: EnvWriteResult = write_env_values(self._env_path, values)
        return EnvWriteResponse(
            ok=True,
            path=result.path,
            updated_keys=result.updated_keys,
            new_mtime=result.new_mtime,
        )


def _flatten_dict(data: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    """嵌套 dict 转 dot-path。"""
    out: list[tuple[str, Any]] = []
    for k, v in data.items():
        path = f"{prefix}.{k}" if prefix else k
        if path in _DICT_LEAF_PATHS:
            out.append((path, v))
            continue
        if isinstance(v, dict) and v:
            out.extend(_flatten_dict(v, path))
        else:
            out.append((path, v))
    return out


def _default_env_path(yaml_path: Path) -> Path:
    """按配置文件布局推导默认 `.env` 路径。"""
    if yaml_path.parent.name == "config":
        return yaml_path.parent.parent / ".env"
    return yaml_path.parent / ".env"


def _yaml_explicit_paths(yaml_path: Path) -> set[str]:
    """读 yaml 原文，返回显式写了的 leaf dot-path 集合。"""
    yaml = YAML(typ="rt")
    with yaml_path.open("r", encoding="utf-8") as f:
        doc = yaml.load(f)
    if doc is None:
        return set()
    if not isinstance(doc, dict):
        return set()
    flat: list[tuple[str, Any]] = _flatten_dict(dict(doc))
    return {path for path, _ in flat}


def _read_web_llm_presets(yaml_path: Path) -> list[dict[str, Any]]:
    """从 yaml 原文读取 `web.llm_presets`，缺省时返回空列表。"""
    yaml = YAML(typ="rt")
    with yaml_path.open("r", encoding="utf-8") as f:
        doc = yaml.load(f)
    if doc is None or not isinstance(doc, dict):
        return []
    web = doc.get("web")
    if not isinstance(web, dict):
        return []
    raw_presets = web.get("llm_presets")
    if raw_presets is None:
        return []
    if not isinstance(raw_presets, list):
        raise ValidationFailedError(
            [{"path": "web.llm_presets", "message": "web.llm_presets must be a list"}]
        )
    return [dict(item) for item in raw_presets if isinstance(item, dict)]


__all__ = [
    "ConfigManager",
    "ConflictError",
    "EffectiveResponse",
    "EnvWriteResponse",
    "PatchItem",
    "PresetWriteResponse",
    "RawResponse",
    "SavePatchResponse",
    "SchemaResponse",
    "ValidationFailedError",
]
