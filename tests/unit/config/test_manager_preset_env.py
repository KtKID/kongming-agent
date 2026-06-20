"""infra ConfigManager 的 preset 与 `.env` 写回测试。

覆盖：
- `web.llm_presets` upsert / remove；
- mtime 冲突；
- `.env` 原地更新、追加和当前进程同步。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from infrastructure.config.env_writer import EnvWriterError
from infrastructure.config.manager import ConfigManager
from infrastructure.config.models import LLMPresetConfig
from infrastructure.config.writer import ConflictError, ValidationFailedError

_YAML = """\
model:
  name: local
  base_url: http://127.0.0.1:1234/v1
web:
  initial_password: ""
  llm_presets:
    - id: local
      display_name: Local
      base_url: http://127.0.0.1:1234/v1
      model: local
"""


@pytest.fixture
def yaml_path(tmp_path: Path) -> Path:
    """写入临时 setting.yaml。"""
    path = tmp_path / "config" / "setting.yaml"
    path.parent.mkdir()
    path.write_text(_YAML, encoding="utf-8")
    return path


def test_upsert_web_llm_preset_adds_and_replaces(yaml_path: Path, tmp_path: Path) -> None:
    manager = ConfigManager(yaml_path=yaml_path, env_path=tmp_path / ".env")

    added = LLMPresetConfig(
        id="minimax-m3",
        display_name="MiniMax M3",
        provider="anthropic",
        base_url="https://api.minimaxi.com/anthropic",
        model="MiniMax-M3",
        api_key_env="KONGMING_PROVIDER_MINIMAX_API_KEY",
    )
    result = manager.upsert_web_llm_preset(added)
    assert result.ok is True
    assert result.preset_id == "minimax-m3"
    assert result.preset_count == 2
    text = yaml_path.read_text(encoding="utf-8")
    assert "id: minimax-m3" in text
    assert "MiniMax M3" in text

    time.sleep(0.01)
    replaced = added.model_copy(update={"display_name": "MiniMax M3 Updated"})
    result = manager.upsert_web_llm_preset(replaced)
    assert result.preset_count == 2
    text = yaml_path.read_text(encoding="utf-8")
    assert "MiniMax M3 Updated" in text
    assert "MiniMax M3\n" not in text


def test_remove_web_llm_preset(yaml_path: Path, tmp_path: Path) -> None:
    manager = ConfigManager(yaml_path=yaml_path, env_path=tmp_path / ".env")
    result = manager.remove_web_llm_preset("local")
    assert result.ok is True
    assert result.preset_count == 0
    assert "id: local" not in yaml_path.read_text(encoding="utf-8")


def test_upsert_web_llm_preset_conflict(yaml_path: Path, tmp_path: Path) -> None:
    manager = ConfigManager(yaml_path=yaml_path, env_path=tmp_path / ".env")
    stale_mtime = yaml_path.stat().st_mtime - 1000
    preset = LLMPresetConfig(
        id="p",
        display_name="P",
        base_url="http://127.0.0.1:1234/v1",
        model="p",
    )
    with pytest.raises(ConflictError):
        manager.upsert_web_llm_preset(preset, expected_mtime=stale_mtime)


def test_upsert_web_llm_preset_materializes_missing_web_section(tmp_path: Path) -> None:
    yaml_path = tmp_path / "config" / "setting.yaml"
    yaml_path.parent.mkdir()
    yaml_path.write_text(
        """\
model:
  name: local
  base_url: http://127.0.0.1:1234/v1
""",
        encoding="utf-8",
    )
    manager = ConfigManager(yaml_path=yaml_path, env_path=tmp_path / ".env")
    preset = LLMPresetConfig(
        id="p",
        display_name="P",
        base_url="http://127.0.0.1:1234/v1",
        model="p",
    )

    result = manager.upsert_web_llm_preset(preset)

    assert result.ok is True
    text = yaml_path.read_text(encoding="utf-8")
    assert "web:" in text
    assert "llm_presets:" in text
    assert "id: p" in text


def test_upsert_web_llm_preset_uses_env_for_unrelated_remote_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yaml_path = tmp_path / "config" / "setting.yaml"
    yaml_path.parent.mkdir()
    yaml_path.write_text(
        """\
model:
  name: remote
  base_url: https://api.example.com/v1
web:
  initial_password: ""
""",
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    env_path.write_text("KONGMING_MODEL_API_KEY=secret\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KONGMING_SKIP_DOTENV", raising=False)
    monkeypatch.delenv("KONGMING_MODEL_API_KEY", raising=False)
    manager = ConfigManager(yaml_path=yaml_path, env_path=env_path)
    preset = LLMPresetConfig(
        id="p",
        display_name="P",
        base_url="http://127.0.0.1:1234/v1",
        model="p",
    )

    result = manager.upsert_web_llm_preset(preset)

    assert result.ok is True
    assert "id: p" in yaml_path.read_text(encoding="utf-8")


def test_upsert_web_llm_preset_validation_failure_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yaml_path = tmp_path / "config" / "setting.yaml"
    yaml_path.parent.mkdir()
    original = """\
model:
  name: local
  base_url: http://127.0.0.1:1234/v1
  temperature: 99
web:
  initial_password: ""
"""
    yaml_path.write_text(original, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KONGMING_MODEL_API_KEY", raising=False)
    manager = ConfigManager(yaml_path=yaml_path, env_path=tmp_path / ".env")
    preset = LLMPresetConfig(
        id="p",
        display_name="P",
        base_url="http://127.0.0.1:1234/v1",
        model="p",
    )

    with pytest.raises(ValidationFailedError):
        manager.upsert_web_llm_preset(preset)

    assert yaml_path.read_text(encoding="utf-8") == original
    assert not list(yaml_path.parent.glob("setting.yaml.tmp.*"))


def test_write_env_values_updates_file_and_process(yaml_path: Path, tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# keep\nOLD=value\nKONGMING_PROVIDER_MINIMAX_API_KEY=old\n", encoding="utf-8"
    )
    manager = ConfigManager(yaml_path=yaml_path, env_path=env_path)

    result = manager.write_env_values(
        {
            "KONGMING_PROVIDER_MINIMAX_API_KEY": "new secret",
            "KONGMING_PROVIDER_MINIMAX_GROUP_ID": "group-1",
        }
    )

    assert result.ok is True
    text = env_path.read_text(encoding="utf-8")
    assert "# keep" in text
    assert "OLD=value" in text
    assert 'KONGMING_PROVIDER_MINIMAX_API_KEY="new secret"' in text
    assert "KONGMING_PROVIDER_MINIMAX_GROUP_ID=group-1" in text
    assert os.environ["KONGMING_PROVIDER_MINIMAX_API_KEY"] == "new secret"
    assert os.environ["KONGMING_PROVIDER_MINIMAX_GROUP_ID"] == "group-1"
    assert not list(tmp_path.glob(".env.tmp.*"))


def test_default_env_path_for_config_dir_layout(
    yaml_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    manager = ConfigManager(yaml_path=yaml_path)

    result = manager.write_env_values({"MINIMAX_API_KEY": "secret"})

    assert result.path == str(tmp_path / ".env")
    assert "MINIMAX_API_KEY=secret" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert not (tmp_path / "config" / ".env").exists()


def test_default_env_path_for_single_file_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    yaml_path = tmp_path / ".kongming" / "setting.yaml"
    yaml_path.parent.mkdir()
    yaml_path.write_text(_YAML, encoding="utf-8")
    manager = ConfigManager(yaml_path=yaml_path)

    result = manager.write_env_values({"MINIMAX_API_KEY": "secret"})

    assert result.path == str(tmp_path / ".kongming" / ".env")
    assert "MINIMAX_API_KEY=secret" in (tmp_path / ".kongming" / ".env").read_text(encoding="utf-8")
    assert not (tmp_path / ".env").exists()


def test_write_env_values_rejects_invalid_name_without_touching_file(
    yaml_path: Path,
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    original = "GOOD=value\n"
    env_path.write_text(original, encoding="utf-8")
    manager = ConfigManager(yaml_path=yaml_path, env_path=env_path)

    with pytest.raises(EnvWriterError):
        manager.write_env_values({"bad-key": "x"})

    assert env_path.read_text(encoding="utf-8") == original
