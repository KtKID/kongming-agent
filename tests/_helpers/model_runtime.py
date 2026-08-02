"""为 provider 单测构造 catalog v2 runtime snapshot 与 credential。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.model_provider_catalog import (
    CatalogSource,
    ProviderProtocol,
    ReasoningCapability,
    ResolvedModelConfig,
    ResolvedModelCredential,
)
from infrastructure.config.models import ApiKeyHeader, ReasoningEffortInput


@dataclass(frozen=True)
class CatalogModelFixture:
    """测试 catalog 中的最小模型声明。"""

    preset_id: str
    model: str
    default_reasoning_effort: ReasoningEffortInput | None = None


def make_model_catalog_manager(
    tmp_path: Path,
    *,
    models: tuple[CatalogModelFixture, ...],
    default_preset_id: str | None = None,
) -> ModelCatalogManager:
    """写入临时 catalog 并返回隔离的 Manager。"""
    if not models:
        raise ValueError("test catalog requires at least one model")
    catalog_models: list[dict[str, object]] = []
    for model in models:
        item: dict[str, object] = {
            "preset_id": model.preset_id,
            "display_name": model.model,
            "model": model.model,
        }
        if model.default_reasoning_effort is not None:
            item["reasoning"] = {
                "adapter": "configurable_patch",
                "supported_efforts": ["low", "medium", "high", "max"],
                "default_effort": model.default_reasoning_effort,
                "supports_disabled": True,
                "enabled_patch": {"thinking": {"type": "enabled"}},
                "disabled_patch": {"thinking": {"type": "disabled"}},
            }
        catalog_models.append(item)
    path = tmp_path / "model-providers.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "providers": [
                    {
                        "provider_id": "test-provider",
                        "default_preset_id": default_preset_id or models[0].preset_id,
                        "display_name": "Test Provider",
                        "region_label": "Test",
                        "description": "Test-only provider catalog.",
                        "logo_text": "T",
                        "protocol": "openai",
                        "default_base_url": "http://127.0.0.1:1234/v1",
                        "request_defaults": {
                            "timeout_seconds": 60,
                            "max_tokens": 4096,
                            "temperature": 0.7,
                        },
                        "models": catalog_models,
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return ModelCatalogManager(
        builtin_path=path,
        user_path=tmp_path / "missing-user-catalog.yaml",
        environ={},
    )


def make_model_runtime(
    *,
    name: str = "test-model",
    base_url: str = "http://127.0.0.1:1234/v1",
    api_key: str = "",
    protocol: ProviderProtocol = ProviderProtocol.OPENAI,
    api_key_header: ApiKeyHeader | None = None,
    timeout: float = 60.0,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    reasoning: ReasoningCapability | None = None,
    reasoning_effort: ReasoningEffortInput | None = None,
) -> tuple[ResolvedModelConfig, ResolvedModelCredential]:
    """返回不经过持久化 setting 的完整 provider 测试输入。"""
    runtime = ResolvedModelConfig(
        catalog_version=2,
        catalog_source=CatalogSource.USER,
        provider_id="test-provider",
        preset_id="test-preset",
        protocol=protocol,
        name=name,
        base_url=base_url.rstrip("/"),
        api_key_env="TEST_PROVIDER_API_KEY" if api_key else None,
        fallback_api_key_envs=(),
        api_key_header=api_key_header,
        timeout=timeout,
        max_tokens=max_tokens,
        temperature=temperature,
        context_window_tokens=None,
        reasoning=reasoning,
        default_reasoning_effort=reasoning_effort,
    )
    credential = ResolvedModelCredential(
        value=api_key,
        env_name="TEST_PROVIDER_API_KEY" if api_key else None,
        header=api_key_header,
    )
    return runtime, credential


__all__ = ["CatalogModelFixture", "make_model_catalog_manager", "make_model_runtime"]
