"""unit：config_loader.load_config 基本行为。

- 从 yaml 加载出合法 Config
- 环境变量覆盖单字段
- 本地 base_url 可空 api_key
- 远端 base_url + 空 api_key 抛 ConfigValidationError
- 文件不存在抛 ConfigLoadError
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config_loader import load_config
from config_loader.errors import ConfigLoadError, ConfigValidationError
from config_loader.models import Config

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_YAML = REPO_ROOT / "config" / "default.yaml"
LOCAL_YAML = REPO_ROOT / "config" / "local-model.yaml"


@pytest.mark.unit
def test_load_default_yaml_returns_config() -> None:
    cfg = load_config(DEFAULT_YAML)
    assert isinstance(cfg, Config)
    assert cfg.model.name == "gemma-4-e4b-it"
    assert cfg.model.base_url == "http://127.0.0.1:1234"
    assert cfg.runner.max_turns == 10
    assert cfg.approval.mode == "interactive"


@pytest.mark.unit
def test_load_local_model_yaml_has_empty_api_key() -> None:
    cfg = load_config(LOCAL_YAML)
    assert cfg.model.api_key == ""
    assert cfg.model.is_local is True


@pytest.mark.unit
def test_missing_file_raises_config_load_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    with pytest.raises(ConfigLoadError):
        load_config(missing)


@pytest.mark.unit
def test_env_override_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KONGMING_MODEL_API_KEY", "sk-xyz")
    cfg = load_config(LOCAL_YAML)
    assert cfg.model.api_key == "sk-xyz"


@pytest.mark.unit
def test_env_override_max_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KONGMING_RUNNER_MAX_TURNS", "42")
    cfg = load_config(LOCAL_YAML)
    assert cfg.runner.max_turns == 42


@pytest.mark.unit
def test_env_override_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KONGMING_MODEL_BASE_URL", "http://127.0.0.1:9999")
    cfg = load_config(LOCAL_YAML)
    assert cfg.model.base_url == "http://127.0.0.1:9999"


@pytest.mark.unit
def test_remote_url_without_api_key_triggers_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """远端 url + 空 api_key 应当在 pydantic 校验阶段被拒。"""
    monkeypatch.setenv("KONGMING_MODEL_BASE_URL", "https://api.openai.com")
    monkeypatch.setenv("KONGMING_MODEL_API_KEY", "")
    with pytest.raises(ConfigValidationError):
        load_config(LOCAL_YAML)


@pytest.mark.unit
def test_load_config_invalid_yaml_raises_load_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    # 制造一个肯定无法 parse 的 YAML：未闭合的 flow map。
    bad.write_text("{foo: bar,", encoding="utf-8")
    with pytest.raises(ConfigLoadError):
        load_config(bad)


@pytest.mark.unit
def test_load_config_non_mapping_root_raises_load_error(tmp_path: Path) -> None:
    bad = tmp_path / "list.yaml"
    bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ConfigLoadError):
        load_config(bad)


@pytest.mark.unit
def test_load_config_respects_kongming_config_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未显式传 path 时应该优先读取 ``KONGMING_CONFIG``。"""
    monkeypatch.setenv("KONGMING_CONFIG", str(LOCAL_YAML))
    cfg = load_config()
    assert cfg.model.base_url == "http://127.0.0.1:1234"
