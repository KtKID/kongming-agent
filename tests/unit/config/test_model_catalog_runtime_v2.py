"""模型 catalog v2 与运行时选择合同测试。"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest

from infrastructure.config import models as config_models
from infrastructure.config.errors import ConfigValidationError
from infrastructure.config.loader import load_config
from infrastructure.config.model_provider_catalog import ModelProviderCatalogError

_BUILTIN_CATALOG = """\
version: 2
providers:
  - provider_id: glm
    default_preset_id: glm-52
    display_name: GLM
    region_label: CN
    description: GLM test provider
    logo_text: G
    protocol: openai
    default_base_url: https://open.bigmodel.cn/api/coding/paas/v4
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


def _write(path: Path, content: str) -> Path:
    """写入测试 catalog 并返回路径。"""
    path.write_text(content, encoding="utf-8")
    return path


def _manager_type() -> type[object]:
    """延迟读取待实现 Manager，让测试以断言失败而非收集失败呈现。"""
    module = importlib.import_module("infrastructure.config.model_catalog_manager")
    manager_type = getattr(module, "ModelCatalogManager", None)
    assert isinstance(manager_type, type)
    return manager_type


def _selection(*, effort: str | None = None) -> object:
    """构造最小运行选择。"""
    selection_type = getattr(config_models, "ModelSelectionConfig", None)
    assert isinstance(selection_type, type)
    return selection_type(preset_id="glm-52", reasoning_effort=effort)


def test_model_selection_persists_only_runtime_fields() -> None:
    """setting.model 只保留 preset 和默认 effort。"""
    selection_type = getattr(config_models, "ModelSelectionConfig", None)
    assert isinstance(selection_type, type)
    assert set(selection_type.model_fields) == {"preset_id", "reasoning_effort"}


def test_v06_setting_rejects_static_model_fields(tmp_path: Path) -> None:
    """v0.6 setting 对旧静态模型字段给出严格 schema 错误。"""
    path = _write(
        tmp_path / "setting.yaml",
        """\
config_schema_version: v0.6
model:
  preset_id: glm-52
  name: glm-5.2
""",
    )

    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(path, load_env_file=False, migrate=False)

    assert exc_info.value.details["errors"][0]["loc"] == ("model", "name")


def test_retired_static_model_env_has_actionable_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧静态 env 指向 catalog 与新 preset 选择入口。"""
    path = _write(
        tmp_path / "setting.yaml",
        "config_schema_version: v0.6\nmodel:\n  preset_id: glm-52\n",
    )
    monkeypatch.setenv("KONGMING_MODEL_BASE_URL", "https://retired.example/v1")

    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(path, load_env_file=False, migrate=False)

    assert exc_info.value.details["code"] == "retired_model_environment"
    assert exc_info.value.details["replacement"] == (
        "KONGMING_MODEL_PROVIDER_CATALOG",
        "KONGMING_MODEL_PRESET_ID",
        "KONGMING_MODEL_REASONING_EFFORT",
    )


def test_catalog_v2_projects_static_model_and_reasoning_capability(tmp_path: Path) -> None:
    """catalog v2 同时承载请求默认值、上下文和 reasoning 能力。"""
    manager_type = _manager_type()
    manager = manager_type(builtin_path=_write(tmp_path / "builtin.yaml", _BUILTIN_CATALOG))

    snapshot = manager.load_catalog()
    provider = snapshot.providers[0]
    model = provider.models[0]

    assert snapshot.version == 2
    assert provider.protocol.value == "openai"
    assert provider.request_defaults.timeout_seconds == 75
    assert model.context_window_tokens == 1_000_000
    assert model.request_defaults.max_tokens == 65_536
    assert model.reasoning.adapter.value == "glm_thinking_toggle"
    assert model.reasoning.default_effort == "high"
    assert model.reasoning.supports_disabled is True


def test_user_catalog_replaces_complete_provider(tmp_path: Path) -> None:
    """同 provider_id 的用户定义按完整 provider 替换。"""
    user_catalog = _BUILTIN_CATALOG.replace("model: glm-5.2", "model: glm-user").replace(
        "display_name: GLM\n", "display_name: GLM User\n"
    )
    manager_type = _manager_type()
    manager = manager_type(
        builtin_path=_write(tmp_path / "builtin.yaml", _BUILTIN_CATALOG),
        user_path=_write(tmp_path / "user.yaml", user_catalog),
    )

    snapshot = manager.load_catalog()

    assert len(snapshot.providers) == 1
    assert snapshot.providers[0].display_name == "GLM User"
    assert snapshot.providers[0].models[0].model == "glm-user"
    assert snapshot.providers[0].source.value == "user"


def test_global_preset_id_conflict_is_rejected(tmp_path: Path) -> None:
    """不同 provider 不能声明相同 preset ID。"""
    conflicting = (
        _BUILTIN_CATALOG
        + """
  - provider_id: duplicate
    default_preset_id: glm-52
    display_name: Duplicate
    region_label: Local
    description: duplicate preset
    logo_text: D
    protocol: openai
    default_base_url: http://127.0.0.1:1234/v1
    default_api_key_env:
    default_api_key_header: authorization-bearer
    request_defaults: {}
    models:
      - preset_id: glm-52
        display_name: Duplicate
        model: duplicate
"""
    )
    manager_type = _manager_type()
    manager = manager_type(builtin_path=_write(tmp_path / "builtin.yaml", conflicting))

    with pytest.raises(ModelProviderCatalogError) as exc_info:
        manager.load_catalog()

    assert exc_info.value.details["code"] == "catalog_invalid"


def test_runtime_snapshot_is_frozen_and_contains_no_secret(tmp_path: Path) -> None:
    """解析结果不可变，且仅保存 credential 引用。"""
    manager_type = _manager_type()
    manager = manager_type(
        builtin_path=_write(tmp_path / "builtin.yaml", _BUILTIN_CATALOG),
        environ={"GLM_API_KEY": "super-secret"},
    )

    runtime = manager.resolve_runtime(_selection())

    assert runtime.name == "glm-5.2"
    assert runtime.max_tokens == 65_536
    assert runtime.api_key_env == "GLM_API_KEY"
    assert "super-secret" not in repr(runtime)
    with pytest.raises((AttributeError, TypeError)):
        runtime.name = "mutated"


def test_unknown_preset_and_missing_credential_use_stable_errors(tmp_path: Path) -> None:
    """未知 preset 与缺 credential 返回稳定错误类别。"""
    manager_type = _manager_type()
    manager = manager_type(
        builtin_path=_write(tmp_path / "builtin.yaml", _BUILTIN_CATALOG),
        environ={},
    )
    selection_type = getattr(config_models, "ModelSelectionConfig", None)
    assert isinstance(selection_type, type)

    with pytest.raises(ModelProviderCatalogError) as unknown:
        manager.resolve_runtime(selection_type(preset_id="missing"))
    assert unknown.value.details["code"] == "preset_unknown"

    runtime = manager.resolve_runtime(_selection())
    with pytest.raises(ModelProviderCatalogError) as missing:
        manager.resolve_credential(runtime)
    assert missing.value.details["code"] == "credential_missing"


def test_effort_precedence_preserves_explicit_none(tmp_path: Path) -> None:
    """显式 none 高于 env、setting 和 catalog default。"""
    manager_type = _manager_type()
    manager = manager_type(
        builtin_path=_write(tmp_path / "builtin.yaml", _BUILTIN_CATALOG),
        environ={"KONGMING_MODEL_REASONING_EFFORT": "medium"},
    )

    explicit = manager.resolve_runtime(_selection(effort="low"), reasoning_effort="none")
    from_env = manager.resolve_runtime(_selection(effort="low"))
    from_setting = manager_type(builtin_path=tmp_path / "builtin.yaml", environ={}).resolve_runtime(
        _selection(effort="low")
    )
    from_catalog = manager_type(builtin_path=tmp_path / "builtin.yaml", environ={}).resolve_runtime(
        _selection()
    )

    assert explicit.default_reasoning_effort == "none"
    assert from_env.default_reasoning_effort == "medium"
    assert from_setting.default_reasoning_effort == "low"
    assert from_catalog.default_reasoning_effort == "high"


def test_unsupported_effort_fails_before_provider_io(tmp_path: Path) -> None:
    """catalog 不支持的 effort 在 runtime resolve 阶段失败。"""
    manager_type = _manager_type()
    manager = manager_type(
        builtin_path=_write(tmp_path / "builtin.yaml", _BUILTIN_CATALOG), environ={}
    )

    with pytest.raises(ModelProviderCatalogError) as exc_info:
        manager.resolve_runtime(_selection(), reasoning_effort="max")

    assert exc_info.value.details["code"] == "reasoning_unsupported"


@pytest.mark.asyncio
async def test_concurrent_none_and_high_resolution_are_isolated(tmp_path: Path) -> None:
    """并发请求分别获得自己的 immutable effort。"""
    manager_type = _manager_type()
    manager = manager_type(
        builtin_path=_write(tmp_path / "builtin.yaml", _BUILTIN_CATALOG), environ={}
    )

    async def resolve(effort: str) -> object:
        await asyncio.sleep(0)
        return manager.resolve_runtime(_selection(), reasoning_effort=effort)

    disabled, high = await asyncio.gather(resolve("none"), resolve("high"))

    assert disabled.default_reasoning_effort == "none"
    assert high.default_reasoning_effort == "high"
