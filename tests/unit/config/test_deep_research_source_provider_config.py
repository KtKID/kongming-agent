"""deep_research Web 来源 provider 配置契约测试。

本测试覆盖两条配置链路：
1. ``load_config`` 能消费 Web deep_research source provider 的 6 个 env override；
2. ``schema.py`` 能向 manage 配置页暴露对应字段元数据。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.config import load_config
from infrastructure.config.schema import get_field_meta, list_field_metas

_SOURCE_PROVIDER_META_EXPECTATIONS = {
    "web.deep_research_source_provider.enabled": ("bool", True, True),
    "web.deep_research_source_provider.provider_name": ("string", True, True),
    "web.deep_research_source_provider.search_tool_name": ("string", True, True),
    "web.deep_research_source_provider.fetch_tool_name": ("string", True, True),
    "web.deep_research_source_provider.search_tool_names": ("list", False, True),
    "web.deep_research_source_provider.fetch_tool_names": ("list", False, True),
}


def _write_minimal_config(tmp_path: Path) -> Path:
    """写入只含必填模型字段的临时配置文件，返回配置文件路径。"""
    config_path = tmp_path / "setting.yaml"
    config_path.write_text(
        "\n".join(
            [
                "model:",
                "  name: stub-model",
                "  base_url: http://127.0.0.1:1234/v1",
                "  api_key: ''",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def test_load_config_consumes_deep_research_source_provider_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """6 个 Web deep_research source provider env override 必须落到 Config 对象。"""
    config_path = _write_minimal_config(tmp_path)
    monkeypatch.setenv("KONGMING_WEB_DEEP_RESEARCH_SOURCE_PROVIDER_ENABLED", "false")
    monkeypatch.setenv(
        "KONGMING_WEB_DEEP_RESEARCH_SOURCE_PROVIDER_PROVIDER_NAME",
        "custom_research_source",
    )
    monkeypatch.setenv(
        "KONGMING_WEB_DEEP_RESEARCH_SOURCE_PROVIDER_SEARCH_TOOL_NAME",
        "explicit_search",
    )
    monkeypatch.setenv(
        "KONGMING_WEB_DEEP_RESEARCH_SOURCE_PROVIDER_FETCH_TOOL_NAME",
        "explicit_fetch",
    )
    monkeypatch.setenv(
        "KONGMING_WEB_DEEP_RESEARCH_SOURCE_PROVIDER_SEARCH_TOOL_NAMES",
        "search_alpha, search_beta,search_gamma",
    )
    monkeypatch.setenv(
        "KONGMING_WEB_DEEP_RESEARCH_SOURCE_PROVIDER_FETCH_TOOL_NAMES",
        "fetch_alpha, fetch_beta,fetch_gamma",
    )

    cfg = load_config(config_path, load_env_file=False)

    source_provider = cfg.web.deep_research_source_provider
    assert source_provider.enabled is False
    assert source_provider.provider_name == "custom_research_source"
    assert source_provider.search_tool_name == "explicit_search"
    assert source_provider.fetch_tool_name == "explicit_fetch"
    assert source_provider.search_tool_names == (
        "search_alpha",
        "search_beta",
        "search_gamma",
    )
    assert source_provider.fetch_tool_names == (
        "fetch_alpha",
        "fetch_beta",
        "fetch_gamma",
    )


def test_schema_exposes_deep_research_source_provider_field_metas() -> None:
    """schema API 必须暴露 6 个字段，并保持类型、编辑性和重启语义。"""
    listed_metas = {meta.path: meta for meta in list_field_metas()}

    for path, (
        expected_type,
        expected_editable,
        expected_restart_required,
    ) in _SOURCE_PROVIDER_META_EXPECTATIONS.items():
        listed_meta = listed_metas.get(path)
        assert listed_meta is not None
        assert get_field_meta(path) == listed_meta
        assert listed_meta.type == expected_type
        assert listed_meta.editable is expected_editable
        assert listed_meta.restart_required is expected_restart_required
