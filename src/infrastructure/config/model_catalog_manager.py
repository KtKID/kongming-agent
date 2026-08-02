"""模型目录与运行时选择的统一门户。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from infrastructure.config.model_provider_catalog import (
    CatalogSource,
    ModelCatalogErrorCode,
    ModelProviderCatalog,
    ModelProviderCatalogError,
    ModelProviderCatalogSnapshot,
    ModelProviderDefinition,
    ModelProviderModelDefinition,
    ReasoningAdapter,
    ReasoningCapability,
    ResolvedModelConfig,
    ResolvedModelCredential,
    default_model_provider_catalog_path,
    load_model_provider_catalog_document,
)
from infrastructure.config.models import ModelSelectionConfig, ReasoningEffortInput
from infrastructure.config.paths import get_kongming_home

_PRESET_ENV = "KONGMING_MODEL_PRESET_ID"
_EFFORT_ENV = "KONGMING_MODEL_REASONING_EFFORT"


class ModelCatalogManager:
    """加载 catalog、解析 preset 和构造 immutable runtime snapshot。"""

    def __init__(
        self,
        *,
        builtin_path: Path | None = None,
        user_path: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """保存目录路径和环境视图；实际文件在每次解析时重新读取。"""
        self._builtin_path = (
            builtin_path.expanduser().resolve()
            if builtin_path is not None
            else default_model_provider_catalog_path()
        )
        self._user_path = (
            user_path.expanduser().resolve()
            if user_path is not None
            else (get_kongming_home() / "model-providers.yaml").resolve()
        )
        self._environ = os.environ if environ is None else environ

    @property
    def builtin_path(self) -> Path:
        """返回内置 catalog 实际路径。"""
        return self._builtin_path

    @property
    def user_path(self) -> Path:
        """返回用户 catalog 实际路径。"""
        return self._user_path

    def load_catalog(self) -> ModelProviderCatalogSnapshot:
        """合并内置与用户 catalog，用户同 ID provider 做完整替换。"""
        builtin = load_model_provider_catalog_document(
            self._builtin_path,
            source=CatalogSource.BUILTIN,
        )
        providers = list(builtin.providers)
        source = CatalogSource.BUILTIN
        if self._user_path.exists():
            user = load_model_provider_catalog_document(
                self._user_path,
                source=CatalogSource.USER,
            )
            replacements = {provider.provider_id.lower(): provider for provider in user.providers}
            merged: list[ModelProviderDefinition] = []
            consumed: set[str] = set()
            for provider in providers:
                provider_id = provider.provider_id.lower()
                replacement = replacements.get(provider_id)
                if replacement is None:
                    merged.append(provider)
                    continue
                merged.append(replacement)
                consumed.add(provider_id)
            merged.extend(
                provider
                for provider_id, provider in replacements.items()
                if provider_id not in consumed
            )
            providers = merged
            source = CatalogSource.MERGED

        try:
            validated = ModelProviderCatalog(version=2, providers=tuple(providers))
        except ValidationError as exc:
            raise ModelProviderCatalogError(
                f"invalid merged model provider catalog: {exc}",
                details={
                    "builtin_path": str(self._builtin_path),
                    "user_path": str(self._user_path),
                    "errors": exc.errors(),
                },
            ) from exc
        return ModelProviderCatalogSnapshot(
            version=validated.version,
            source=source,
            providers=validated.providers,
        )

    def list_providers(self) -> tuple[ModelProviderDefinition, ...]:
        """返回合并目录内全部 provider。"""
        return self.load_catalog().providers

    def list_models(self) -> tuple[ModelProviderModelDefinition, ...]:
        """返回合并目录内全部模型，顺序与 provider/model 声明一致。"""
        return tuple(model for provider in self.list_providers() for model in provider.models)

    def get_preset(self, preset_id: str) -> ModelProviderModelDefinition:
        """按全局 preset ID 查询模型，未知 ID 返回 typed error。"""
        _, model = self._find_preset(preset_id)
        return model

    def resolve_runtime(
        self,
        selection: ModelSelectionConfig,
        *,
        preset_id: str | None = None,
        reasoning_effort: ReasoningEffortInput | None = None,
    ) -> ResolvedModelConfig:
        """按显式参数、env、setting、catalog default 解析一次运行快照。"""
        effective_preset_id = (
            preset_id
            if preset_id is not None
            else self._non_empty_env(_PRESET_ENV) or selection.preset_id
        )
        provider, model = self._find_preset(effective_preset_id)
        capability = model.reasoning
        effective_effort = self._resolve_effort(
            selection=selection,
            explicit=reasoning_effort,
            model=model,
            use_selection_default=effective_preset_id == selection.preset_id,
        )
        self._validate_effort(
            preset_id=effective_preset_id,
            capability=capability,
            effort=effective_effort,
        )

        provider_defaults = provider.request_defaults
        model_defaults = model.request_defaults
        api_key_env = model.api_key_env or provider.default_api_key_env
        fallback_envs = (
            model.fallback_api_key_envs
            if model.fallback_api_key_envs is not None
            else provider.fallback_api_key_envs
        )
        return ResolvedModelConfig(
            catalog_version=2,
            catalog_source=provider.source,
            provider_id=provider.provider_id,
            preset_id=model.preset_id,
            protocol=provider.protocol,
            name=model.model,
            base_url=(model.base_url or provider.default_base_url).rstrip("/"),
            api_key_env=api_key_env,
            fallback_api_key_envs=fallback_envs,
            api_key_header=model.api_key_header or provider.default_api_key_header,
            timeout=(
                model_defaults.timeout_seconds
                if model_defaults.timeout_seconds is not None
                else provider_defaults.timeout_seconds
            ),
            max_tokens=(
                model_defaults.max_tokens
                if model_defaults.max_tokens is not None
                else provider_defaults.max_tokens
            ),
            temperature=(
                model_defaults.temperature
                if model_defaults.temperature is not None
                else provider_defaults.temperature
            ),
            context_window_tokens=model.context_window_tokens,
            reasoning=capability,
            default_reasoning_effort=effective_effort,
        )

    def resolve_credential(self, runtime: ResolvedModelConfig) -> ResolvedModelCredential:
        """在 provider 构造阶段解析 credential；本地 endpoint 允许空值。"""
        candidate_envs = tuple(
            env_name
            for env_name in (runtime.api_key_env, *runtime.fallback_api_key_envs)
            if env_name is not None and env_name.strip()
        )
        for env_name in candidate_envs:
            value = self._environ.get(env_name, "").strip()
            if value:
                return ResolvedModelCredential(
                    value=value,
                    env_name=env_name,
                    header=runtime.api_key_header,
                )
        if runtime.is_local:
            return ResolvedModelCredential(
                value="",
                env_name=runtime.api_key_env,
                header=runtime.api_key_header,
            )
        raise ModelProviderCatalogError(
            f"credential is missing for preset {runtime.preset_id!r}",
            code=ModelCatalogErrorCode.CREDENTIAL_MISSING,
            details={
                "preset_id": runtime.preset_id,
                "provider_id": runtime.provider_id,
                "credential_envs": candidate_envs,
            },
        )

    def _find_preset(
        self,
        preset_id: str,
    ) -> tuple[ModelProviderDefinition, ModelProviderModelDefinition]:
        """返回 preset 所属 provider/model；保持单次 catalog 快照一致。"""
        snapshot = self.load_catalog()
        for provider in snapshot.providers:
            for model in provider.models:
                if model.preset_id == preset_id:
                    return provider, model
        raise ModelProviderCatalogError(
            f"unknown model preset: {preset_id!r}",
            code=ModelCatalogErrorCode.PRESET_UNKNOWN,
            details={
                "preset_id": preset_id,
                "available_preset_ids": tuple(
                    model.preset_id for provider in snapshot.providers for model in provider.models
                ),
            },
        )

    def _resolve_effort(
        self,
        *,
        selection: ModelSelectionConfig,
        explicit: ReasoningEffortInput | None,
        model: ModelProviderModelDefinition,
        use_selection_default: bool,
    ) -> ReasoningEffortInput | None:
        """按显式、env、setting、catalog default 顺序选择 effort。"""
        if explicit is not None:
            return explicit
        env_effort = self._non_empty_env(_EFFORT_ENV)
        if env_effort is not None:
            allowed = {"none", "low", "medium", "high", "max"}
            if env_effort not in allowed:
                raise ModelProviderCatalogError(
                    f"invalid {_EFFORT_ENV} value: {env_effort!r}",
                    code=ModelCatalogErrorCode.REASONING_UNSUPPORTED,
                    details={"env": _EFFORT_ENV, "value": env_effort},
                )
            return env_effort  # type: ignore[return-value]
        if use_selection_default and selection.reasoning_effort is not None:
            return selection.reasoning_effort
        if model.reasoning is not None:
            return model.reasoning.default_effort
        return None

    @staticmethod
    def _validate_effort(
        *,
        preset_id: str,
        capability: ReasoningCapability | None,
        effort: ReasoningEffortInput | None,
    ) -> None:
        """在 provider I/O 前验证模型是否支持选定 effort。"""
        if effort is None:
            return
        if capability is None or capability.adapter is ReasoningAdapter.NONE:
            raise ModelProviderCatalogError(
                f"preset {preset_id!r} has no configurable reasoning capability",
                code=ModelCatalogErrorCode.REASONING_UNSUPPORTED,
                details={"preset_id": preset_id, "effort": effort},
            )
        if effort == "none":
            if capability.supports_disabled:
                return
        else:
            aliases = capability.effort_aliases or {}
            aliased = aliases.get(effort, effort)
            mapped = (capability.effort_map or {}).get(aliased, aliased)
            if isinstance(mapped, int):
                mapped = aliased
            if not capability.supported_efforts or mapped in capability.supported_efforts:
                return
        raise ModelProviderCatalogError(
            f"reasoning effort {effort!r} is unsupported by preset {preset_id!r}",
            code=ModelCatalogErrorCode.REASONING_UNSUPPORTED,
            details={
                "preset_id": preset_id,
                "effort": effort,
                "supported_efforts": capability.supported_efforts,
                "supports_disabled": capability.supports_disabled,
            },
        )

    def _non_empty_env(self, name: str) -> str | None:
        """读取并清理非空环境变量。"""
        value = self._environ.get(name)
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


__all__ = ["ModelCatalogManager"]
