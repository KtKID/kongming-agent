"""model provider catalog v2 文件加载测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.config.model_provider_catalog import (
    CatalogSource,
    ModelProviderCatalogError,
    default_model_provider_catalog_path,
    load_model_provider_catalog,
    load_model_provider_catalog_document,
)

_CATALOG = """\
version: 2
providers:
  - provider_id: glm
    default_preset_id: glm-52
    display_name: GLM
    region_label: CN
    description: test provider
    logo_text: G
    protocol: openai
    default_base_url: https://configured.example/v1
    default_api_key_env: GLM_API_KEY
    fallback_api_key_envs: [BIGMODEL_API_KEY]
    default_api_key_header: authorization-bearer
    request_defaults:
      timeout_seconds: 75
      max_tokens: 4096
      temperature: 0.7
    models:
      - preset_id: glm-52
        display_name: GLM-5.2
        model: glm-5.2
        context_window_tokens: 1000000
        request_defaults:
          max_tokens: 65536
          temperature: 1.0
        reasoning:
          adapter: glm_thinking_toggle
          supported_efforts: [low, medium, high]
          default_effort: high
          supports_disabled: true
"""


def _write(tmp_path: Path, text: str = _CATALOG) -> Path:
    """写入 catalog fixture。"""
    path = tmp_path / "model-providers.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_catalog_v2_reads_provider_and_model_defaults(tmp_path: Path) -> None:
    providers = load_model_provider_catalog(_write(tmp_path))

    assert len(providers) == 1
    provider = providers[0]
    model = provider.models[0]
    assert provider.protocol.value == "openai"
    assert provider.request_defaults.timeout_seconds == 75
    assert model.request_defaults.max_tokens == 65_536
    assert model.context_window_tokens == 1_000_000
    assert model.reasoning is not None
    assert model.reasoning.default_effort == "high"


def test_document_loader_marks_requested_source(tmp_path: Path) -> None:
    catalog = load_model_provider_catalog_document(
        _write(tmp_path),
        source=CatalogSource.USER,
    )
    assert catalog.providers[0].source is CatalogSource.USER


def test_catalog_rejects_v1_shape(tmp_path: Path) -> None:
    with pytest.raises(ModelProviderCatalogError) as exc_info:
        load_model_provider_catalog(_write(tmp_path, _CATALOG.replace("version: 2", "version: 1")))
    assert exc_info.value.details["code"] == "catalog_invalid"


def test_catalog_rejects_duplicate_global_preset_ids(tmp_path: Path) -> None:
    duplicate_provider = """
  - provider_id: other
    default_preset_id: glm-52
    display_name: Other
    region_label: Local
    description: duplicate
    logo_text: O
    protocol: openai
    default_base_url: http://127.0.0.1:9999/v1
    request_defaults: {}
    models:
      - preset_id: glm-52
        model: other
"""
    with pytest.raises(ModelProviderCatalogError):
        load_model_provider_catalog(_write(tmp_path, _CATALOG + duplicate_provider))


def test_default_catalog_path_points_to_v2_catalog() -> None:
    path = default_model_provider_catalog_path()
    assert path.name == "model-providers.yaml"
    assert (
        load_model_provider_catalog_document(
            path,
            source=CatalogSource.BUILTIN,
        ).version
        == 2
    )
