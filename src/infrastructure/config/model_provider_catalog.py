"""模型 provider catalog v2 的 schema 与文件加载实现。

本模块属于 ``ModelCatalogManager`` 的内部实现。catalog 保存 provider/model
静态定义，YAML 中只保存 credential 环境变量名，运行快照不会携带 secret 值。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from yaml import YAMLError

from infrastructure.config.models import (
    ApiKeyHeader,
    ReasoningEffortInput,
    ReasoningLevel,
)

_REPO_ROOT: Path = Path(__file__).resolve().parents[3]
_CATALOG_ENV = "KONGMING_MODEL_PROVIDER_CATALOG"
_LOCAL_HOSTS: frozenset[str] = frozenset(
    {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}
)


class ProviderProtocol(StrEnum):
    """catalog 支持的 provider wire 协议。"""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class CatalogSource(StrEnum):
    """静态模型定义的来源。"""

    BUILTIN = "builtin"
    USER = "user"
    MERGED = "builtin+user"


class ReasoningAdapter(StrEnum):
    """统一 effort 到厂商 payload 的稳定适配器标识。"""

    NONE = "none"
    DEEPSEEK_OPENAI_THINKING = "deepseek_openai_thinking"
    DEEPSEEK_ANTHROPIC_THINKING = "deepseek_anthropic_thinking"
    GLM_THINKING_TOGGLE = "glm_thinking_toggle"
    ANTHROPIC_THINKING_TOGGLE = "anthropic_thinking_toggle"
    GLM_THINKING_BUDGET = "glm_thinking_budget"
    ANTHROPIC_COMPATIBLE_REASONING = "anthropic_compatible_reasoning"
    CONFIGURABLE_PATCH = "configurable_patch"


class ModelCatalogErrorCode(StrEnum):
    """模型目录与运行选择的稳定错误类别。"""

    CATALOG_INVALID = "catalog_invalid"
    PRESET_UNKNOWN = "preset_unknown"
    CREDENTIAL_MISSING = "credential_missing"
    REASONING_UNSUPPORTED = "reasoning_unsupported"
    MIGRATION_INCOMPLETE = "migration_incomplete"


class ModelProviderCatalogError(ValueError):
    """模型目录读取、校验或运行解析失败。"""

    def __init__(
        self,
        message: str,
        *,
        code: ModelCatalogErrorCode = ModelCatalogErrorCode.CATALOG_INVALID,
        details: dict[str, object] | None = None,
    ) -> None:
        """保存稳定错误类别和结构化诊断。"""
        super().__init__(message)
        merged_details = dict(details or {})
        merged_details.setdefault("code", code.value)
        self.code = code
        self.details = merged_details


class ProviderRequestDefaults(BaseModel):
    """provider 级请求默认值。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_seconds: float = Field(default=60.0, gt=0)
    max_tokens: int = Field(default=4096, gt=0)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)


class ModelRequestDefaults(BaseModel):
    """模型级请求覆盖；空字段继承 provider 默认值。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_seconds: float | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)


class ReasoningCapability(BaseModel):
    """单个模型直接声明的 reasoning 控制合同。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter: ReasoningAdapter
    supported_efforts: tuple[ReasoningLevel, ...] = ()
    default_effort: ReasoningEffortInput | None = None
    supports_disabled: bool = False
    effort_aliases: dict[str, ReasoningLevel] | None = None
    effort_map: dict[ReasoningLevel, str | int] | None = None
    enabled_patch: dict[str, Any] | None = None
    disabled_patch: dict[str, Any] | None = None
    effort_path: str | None = None

    @model_validator(mode="after")
    def _validate_contract(self) -> ReasoningCapability:
        """校验默认档位、关闭能力和 configurable patch 的一致性。"""
        if self.default_effort == "none" and not self.supports_disabled:
            raise ValueError("default_effort=none requires supports_disabled=true")
        if (
            self.default_effort is not None
            and self.default_effort != "none"
            and self.supported_efforts
            and self.default_effort not in self.supported_efforts
        ):
            raise ValueError("reasoning default_effort must be listed in supported_efforts")
        if self.effort_path is not None and not self.effort_path.strip():
            raise ValueError("reasoning effort_path must be non-empty")
        if (
            self.adapter is ReasoningAdapter.CONFIGURABLE_PATCH
            and self.enabled_patch is None
            and self.disabled_patch is None
        ):
            raise ValueError("configurable_patch requires enabled_patch or disabled_patch")
        return self


class ModelProviderModelDefinition(BaseModel):
    """provider 下的单个模型与 preset 定义。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preset_id: str
    display_name: str | None = None
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    fallback_api_key_envs: tuple[str, ...] | None = None
    api_key_header: ApiKeyHeader | None = None
    context_window_tokens: int | None = Field(default=None, gt=0)
    request_defaults: ModelRequestDefaults = Field(default_factory=ModelRequestDefaults)
    reasoning: ReasoningCapability | None = None

    @field_validator("preset_id", "model")
    @classmethod
    def _non_empty_required(cls, value: str) -> str:
        """校验并归一化必填字符串。"""
        stripped = value.strip()
        if not stripped:
            raise ValueError("field must be non-empty")
        return stripped

    @field_validator("display_name", "base_url", "api_key_env")
    @classmethod
    def _non_empty_optional(cls, value: str | None) -> str | None:
        """可选字符串存在时必须包含有效内容。"""
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("field must be non-empty")
        return stripped


class ModelProviderDefinition(BaseModel):
    """完整 provider 定义；用户 catalog 覆盖时以本对象为替换单位。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str
    default_preset_id: str
    display_name: str
    region_label: str
    description: str
    logo_text: str
    protocol: ProviderProtocol
    default_base_url: str
    default_api_key_env: str | None = None
    fallback_api_key_envs: tuple[str, ...] = ()
    default_api_key_header: ApiKeyHeader | None = None
    request_defaults: ProviderRequestDefaults = Field(default_factory=ProviderRequestDefaults)
    models: tuple[ModelProviderModelDefinition, ...]
    match_keywords: tuple[str, ...] = ()
    match_hosts: tuple[str, ...] = ()
    source: CatalogSource = Field(default=CatalogSource.BUILTIN, exclude=True)

    @field_validator(
        "provider_id",
        "default_preset_id",
        "display_name",
        "region_label",
        "logo_text",
        "default_base_url",
    )
    @classmethod
    def _non_empty(cls, value: str) -> str:
        """校验并归一化 provider 核心字符串。"""
        stripped = value.strip()
        if not stripped:
            raise ValueError("field must be non-empty")
        return stripped

    @model_validator(mode="after")
    def _validate_models(self) -> ModelProviderDefinition:
        """校验 provider 内 preset 唯一且默认 preset 可解析。"""
        seen: set[str] = set()
        for model in self.models:
            if model.preset_id in seen:
                raise ValueError(f"duplicate model preset_id: {model.preset_id}")
            seen.add(model.preset_id)
        if not self.models:
            raise ValueError("provider models must not be empty")
        if self.default_preset_id not in seen:
            raise ValueError(f"default_preset_id {self.default_preset_id!r} must exist in models")
        return self


class ModelProviderCatalog(BaseModel):
    """单份 catalog v2 文件结构。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[2]
    providers: tuple[ModelProviderDefinition, ...]

    @model_validator(mode="after")
    def _unique_ids(self) -> ModelProviderCatalog:
        """校验 provider ID 与全局 preset ID 唯一。"""
        provider_ids: set[str] = set()
        preset_owners: dict[str, str] = {}
        for provider in self.providers:
            normalized_provider_id = provider.provider_id.lower()
            if normalized_provider_id in provider_ids:
                raise ValueError(f"duplicate provider_id: {provider.provider_id}")
            provider_ids.add(normalized_provider_id)
            for model in provider.models:
                owner = preset_owners.get(model.preset_id)
                if owner is not None:
                    raise ValueError(
                        f"duplicate preset_id: {model.preset_id} in {owner} and {provider.provider_id}"
                    )
                preset_owners[model.preset_id] = provider.provider_id
        return self


@dataclass(frozen=True)
class ModelProviderCatalogSnapshot:
    """合并后的 immutable catalog 快照。"""

    version: int
    source: CatalogSource
    providers: tuple[ModelProviderDefinition, ...]


@dataclass(frozen=True)
class ResolvedModelConfig:
    """单次 runtime 使用的不可变模型配置快照，不含 credential value。"""

    catalog_version: int
    catalog_source: CatalogSource
    provider_id: str
    preset_id: str
    protocol: ProviderProtocol
    name: str
    base_url: str
    api_key_env: str | None
    fallback_api_key_envs: tuple[str, ...]
    api_key_header: ApiKeyHeader | None
    timeout: float
    max_tokens: int
    temperature: float
    context_window_tokens: int | None
    reasoning: ReasoningCapability | None
    default_reasoning_effort: ReasoningEffortInput | None

    @property
    def effective_provider(self) -> Literal["openai_compatible", "anthropic"]:
        """把 catalog 协议映射为 provider factory 的实现标识。"""
        if self.protocol is ProviderProtocol.ANTHROPIC:
            return "anthropic"
        return "openai_compatible"

    @property
    def is_local(self) -> bool:
        """判断 endpoint 是否为无需 credential 的本地地址。"""
        host = (urlparse(self.base_url).hostname or "").lower()
        return host in _LOCAL_HOSTS or host.startswith("127.")

    @property
    def effective_api_key_header(self) -> ApiKeyHeader:
        """返回显式 header，空值时按协议选择默认写法。"""
        if self.api_key_header is not None:
            return self.api_key_header
        if self.protocol is ProviderProtocol.ANTHROPIC:
            return "x-api-key"
        return "authorization-bearer"


@dataclass(frozen=True, repr=False)
class ResolvedModelCredential:
    """provider 构造阶段使用的 secret，repr 始终脱敏。"""

    value: str
    env_name: str | None
    header: ApiKeyHeader | None

    def __repr__(self) -> str:
        """返回不包含 secret value 的诊断文本。"""
        return (
            "ResolvedModelCredential(value=<redacted>, "
            f"env_name={self.env_name!r}, header={self.header!r})"
        )


def default_model_provider_catalog_path() -> Path:
    """返回内置 catalog 路径，覆盖源码、wheel 与 PyInstaller 布局。"""
    env_path = os.environ.get(_CATALOG_ENV)
    if env_path and env_path.strip():
        return Path(env_path).expanduser().resolve()

    candidates: list[Path] = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root) / "config" / "model-providers.yaml")
    module_path = Path(__file__).resolve()
    candidates.extend(
        [
            _REPO_ROOT / "config" / "model-providers.yaml",
            module_path.parents[2] / "config" / "model-providers.yaml",
            Path.cwd() / "config" / "model-providers.yaml",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return (_REPO_ROOT / "config" / "model-providers.yaml").resolve()


def load_model_provider_catalog_document(
    path: Path,
    *,
    source: CatalogSource,
) -> ModelProviderCatalog:
    """读取单份 catalog v2，并为 provider 标记来源。"""
    catalog_path = path.expanduser().resolve()
    if not catalog_path.exists():
        raise ModelProviderCatalogError(
            f"model provider catalog file not found: {catalog_path}",
            details={"path": str(catalog_path)},
        )
    try:
        raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as exc:
        raise ModelProviderCatalogError(
            f"failed to parse model provider catalog at {catalog_path}: {exc}",
            details={"path": str(catalog_path)},
        ) from exc
    try:
        catalog = ModelProviderCatalog.model_validate(raw)
    except ValidationError as exc:
        raise ModelProviderCatalogError(
            f"invalid model provider catalog at {catalog_path}: {exc}",
            details={"path": str(catalog_path), "errors": exc.errors()},
        ) from exc
    providers = tuple(
        provider.model_copy(update={"source": source}) for provider in catalog.providers
    )
    return catalog.model_copy(update={"providers": providers})


def load_model_provider_catalog(path: Path | None = None) -> tuple[ModelProviderDefinition, ...]:
    """读取内置 catalog provider 列表；跨模块消费应使用 ModelCatalogManager。"""
    catalog_path = path or default_model_provider_catalog_path()
    return load_model_provider_catalog_document(
        catalog_path,
        source=CatalogSource.BUILTIN,
    ).providers


__all__ = [
    "CatalogSource",
    "ModelCatalogErrorCode",
    "ModelProviderCatalog",
    "ModelProviderCatalogError",
    "ModelProviderCatalogSnapshot",
    "ModelProviderDefinition",
    "ModelProviderModelDefinition",
    "ModelRequestDefaults",
    "ProviderProtocol",
    "ProviderRequestDefaults",
    "ReasoningAdapter",
    "ReasoningCapability",
    "ResolvedModelConfig",
    "ResolvedModelCredential",
    "default_model_provider_catalog_path",
    "load_model_provider_catalog",
    "load_model_provider_catalog_document",
]
