"""ConfigManager v0.6 运行选择与 credential 写回测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from infrastructure.config.env_writer import EnvWriterError
from infrastructure.config.manager import ConfigManager
from infrastructure.config.writer import PatchItem

_YAML = """\
config_schema_version: v0.6
model:
  preset_id: local-gemma-4-e4b-it
  reasoning_effort:
web:
  initial_password: ""
"""


@pytest.fixture
def yaml_path(tmp_path: Path) -> Path:
    """写入临时 v0.6 setting。"""
    path = tmp_path / "config" / "setting.yaml"
    path.parent.mkdir()
    path.write_text(_YAML, encoding="utf-8")
    return path


def test_manager_has_no_preset_persistence_api(yaml_path: Path, tmp_path: Path) -> None:
    """用户自定义模型经 user catalog 管理，setting manager 不暴露 preset writer。"""
    manager = ConfigManager(yaml_path=yaml_path, env_path=tmp_path / ".env")
    assert not hasattr(manager, "upsert_web_llm_preset")
    assert not hasattr(manager, "remove_web_llm_preset")


def test_write_provider_credential_updates_file_and_process(
    yaml_path: Path,
    tmp_path: Path,
) -> None:
    """provider-specific credential 只写 .env 并同步进程。"""
    env_path = tmp_path / ".env"
    env_path.write_text("# keep\nGLM_API_KEY=old\n", encoding="utf-8")
    manager = ConfigManager(yaml_path=yaml_path, env_path=env_path)

    result = manager.write_env_values({"GLM_API_KEY": "new secret"})

    assert result.ok is True
    assert "# keep" in env_path.read_text(encoding="utf-8")
    assert 'GLM_API_KEY="new secret"' in env_path.read_text(encoding="utf-8")
    assert os.environ["GLM_API_KEY"] == "new secret"
    assert "GLM_API_KEY" not in yaml_path.read_text(encoding="utf-8")


def test_default_env_path_for_config_dir_layout(
    yaml_path: Path,
    tmp_path: Path,
) -> None:
    manager = ConfigManager(yaml_path=yaml_path)
    result = manager.write_env_values({"MINIMAX_API_KEY": "secret"})
    assert result.path == str(tmp_path / ".env")


def test_read_effective_tracks_runtime_selection_env(
    yaml_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """preset/effort env 覆盖在 dashboard 中标记为 env source。"""
    monkeypatch.setenv("KONGMING_MODEL_PRESET_ID", "bigmodel-glm5-1m")
    monkeypatch.setenv("KONGMING_MODEL_REASONING_EFFORT", "high")
    manager = ConfigManager(yaml_path=yaml_path, env_path=tmp_path / ".env")

    effective = manager.read_effective()

    assert effective.values["model.preset_id"] == "bigmodel-glm5-1m"
    assert effective.values["model.reasoning_effort"] == "high"
    assert effective.sources["model.preset_id"] == "env"
    assert effective.sources["model.reasoning_effort"] == "env"


def test_save_patch_updates_only_runtime_selection(yaml_path: Path, tmp_path: Path) -> None:
    manager = ConfigManager(yaml_path=yaml_path, env_path=tmp_path / ".env")
    raw = manager.read_raw()

    result = manager.save_patch(
        [PatchItem(path="model.reasoning_effort", value="high")],
        expected_mtime=raw.mtime,
    )

    assert result.ok is True
    assert "reasoning_effort: high" in yaml_path.read_text(encoding="utf-8")


def test_write_env_values_rejects_invalid_name(
    yaml_path: Path,
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("GOOD=value\n", encoding="utf-8")
    manager = ConfigManager(yaml_path=yaml_path, env_path=env_path)

    with pytest.raises(EnvWriterError):
        manager.write_env_values({"bad-key": "x"})

    assert env_path.read_text(encoding="utf-8") == "GOOD=value\n"
