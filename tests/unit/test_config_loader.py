"""unit：infrastructure.config.load_config 基本行为。

- 从 yaml 加载出合法 Config
- 环境变量覆盖运行时选择字段
- 旧静态 model 环境变量给出迁移错误
- 文件不存在抛 ConfigLoadError
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from infrastructure.config import load_config
from infrastructure.config.errors import ConfigLoadError, ConfigValidationError
from infrastructure.config.models import Config

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SETTING_YAML = REPO_ROOT / "config" / "setting.yaml"
DEFAULT_YAML = SETTING_YAML  # 向后兼容别名
LOCAL_YAML = SETTING_YAML  # 向后兼容别名


@pytest.fixture(autouse=True)
def _clean_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离宿主或前序测试留下的模型 env 覆盖。"""
    for name in (
        "KONGMING_MODEL_PROVIDER",
        "KONGMING_MODEL_NAME",
        "KONGMING_MODEL_BASE_URL",
        "KONGMING_MODEL_API_KEY",
        "KONGMING_MODEL_API_KEY_HEADER",
        "KONGMING_MODEL_PRESET_ID",
        "KONGMING_MODEL_REASONING_EFFORT",
    ):
        monkeypatch.delenv(name, raising=False)


def _read_yaml(path: Path) -> dict[str, Any]:
    """从配置文件读出原始 dict，用作断言基准——避免测试硬编码和 yaml 不同步。"""
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.mark.unit
def test_load_default_yaml_returns_config() -> None:
    """从 yaml 加载出合法 Config；关键字段和 yaml 原文保持一致。

    断言基准读自 yaml 本身（而非硬编码常量），避免 yaml 改了但测试没跟的漂移。
    测试只守契约："yaml 里写了什么，Config 就应该是什么"。
    load_env_file=False 隔离本地 .env，确保断言不受环境变量干扰。
    """
    cfg = load_config(DEFAULT_YAML, load_env_file=False)
    raw = _read_yaml(DEFAULT_YAML)

    assert isinstance(cfg, Config)
    # 关键字段：从 yaml 动态取基准值比对
    assert cfg.model.preset_id == raw["model"]["preset_id"]
    assert cfg.model.reasoning_effort == raw["model"]["reasoning_effort"]
    assert cfg.runner.max_turns == raw["runner"]["max_turns"]
    assert cfg.approval.mode == raw["approval"]["mode"]


@pytest.mark.unit
def test_load_setting_yaml_has_runtime_selection() -> None:
    """setting.yaml 的 model 节只保留运行时选择。"""
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.model.model_dump() == {
        "preset_id": "local-gemma-4-e4b-it",
        "reasoning_effort": None,
    }


@pytest.mark.unit
def test_missing_file_raises_config_load_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    with pytest.raises(ConfigLoadError):
        load_config(missing, load_env_file=False)


@pytest.mark.unit
def test_env_override_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KONGMING_MODEL_API_KEY", "sk-xyz")
    with pytest.raises(ConfigValidationError, match="retired"):
        load_config(LOCAL_YAML, load_env_file=False)


@pytest.mark.unit
def test_env_override_api_key_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KONGMING_MODEL_API_KEY_HEADER", "authorization-bearer")
    with pytest.raises(ConfigValidationError, match="retired"):
        load_config(LOCAL_YAML, load_env_file=False)


@pytest.mark.unit
def test_env_override_api_key_header_empty_means_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KONGMING_MODEL_API_KEY_HEADER", "")
    with pytest.raises(ConfigValidationError, match="retired"):
        load_config(LOCAL_YAML, load_env_file=False)


@pytest.mark.unit
def test_env_override_max_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KONGMING_RUNNER_MAX_TURNS", "42")
    cfg = load_config(LOCAL_YAML, load_env_file=False)
    assert cfg.runner.max_turns == 42


@pytest.mark.unit
def test_dashboard_poll_interval_defaults_to_five_seconds() -> None:
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.web.dashboard_poll_interval_seconds == 5
    assert cfg.web.normalized_dashboard_poll_interval_seconds == 5


@pytest.mark.unit
def test_dashboard_poll_interval_normalizes_to_minimum_three_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KONGMING_WEB_DASHBOARD_POLL_INTERVAL_SECONDS", "1")
    cfg = load_config(LOCAL_YAML, load_env_file=False)
    assert cfg.web.dashboard_poll_interval_seconds == 1
    assert cfg.web.normalized_dashboard_poll_interval_seconds == 3


@pytest.mark.unit
def test_env_override_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KONGMING_MODEL_BASE_URL", "http://127.0.0.1:9999")
    with pytest.raises(ConfigValidationError, match="retired"):
        load_config(LOCAL_YAML, load_env_file=False)


@pytest.mark.unit
def test_remote_url_without_api_key_triggers_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """静态 endpoint/key 环境变量退出后直接返回迁移错误。"""
    monkeypatch.setenv("KONGMING_MODEL_BASE_URL", "https://api.openai.com")
    monkeypatch.setenv("KONGMING_MODEL_API_KEY", "")
    with pytest.raises(ConfigValidationError):
        load_config(LOCAL_YAML, load_env_file=False)


@pytest.mark.unit
def test_load_config_invalid_yaml_raises_load_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    # 制造一个肯定无法 parse 的 YAML：未闭合的 flow map。
    bad.write_text("{foo: bar,", encoding="utf-8")
    with pytest.raises(ConfigLoadError):
        load_config(bad, load_env_file=False)


@pytest.mark.unit
def test_load_config_non_mapping_root_raises_load_error(tmp_path: Path) -> None:
    bad = tmp_path / "list.yaml"
    bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ConfigLoadError):
        load_config(bad, load_env_file=False)


@pytest.mark.unit
def test_load_config_respects_kongming_config_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未显式传 path 时应该优先读取 ``KONGMING_CONFIG``。"""
    monkeypatch.setenv("KONGMING_CONFIG", str(SETTING_YAML))
    monkeypatch.delenv("KONGMING_MODEL_BASE_URL", raising=False)
    cfg = load_config(load_env_file=False)
    raw = _read_yaml(SETTING_YAML)
    assert cfg.model.preset_id == raw["model"]["preset_id"]


# ---------------------------------------------------------------------------
# .env 加载行为
# ---------------------------------------------------------------------------


def _write_minimal_config(path: Path) -> None:
    """写入只覆盖配置加载测试所需字段的最小配置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "config_schema_version: v0.6\nmodel:\n  preset_id: local-gemma-4-e4b-it\n",
        encoding="utf-8",
    )


@pytest.mark.unit
def test_load_config_reads_home_dotenv_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """默认只加载 `KONGMING_HOME/.env`。

    home `.env` 是 Web 配置写回路径；provider key 进入当前进程环境，
    KONGMING_* 配置覆盖只进入 Config。
    """
    project = tmp_path / "project"
    config = project / "config" / "setting.yaml"
    home = tmp_path / "home"
    _write_minimal_config(config)
    (project / ".env").write_text(
        "GLM_API_KEY=project-glm\n",
        encoding="utf-8",
    )
    home.mkdir()
    (home / ".env").write_text(
        "GLM_API_KEY=home-glm\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("KONGMING_SKIP_DOTENV", raising=False)
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.setenv("KONGMING_HOME", str(home))

    cfg = load_config(config)

    assert cfg.model.preset_id == "local-gemma-4-e4b-it"
    assert os.environ["GLM_API_KEY"] == "home-glm"


@pytest.mark.unit
def test_project_dotenv_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """配置文件旁的项目 `.env` 不参与本地密钥读取。"""
    project = tmp_path / "project"
    config = project / "config" / "setting.yaml"
    home = tmp_path / "home"
    _write_minimal_config(config)
    (project / ".env").write_text(
        "GLM_API_KEY=project-glm\n",
        encoding="utf-8",
    )
    home.mkdir()
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.setenv("KONGMING_HOME", str(home))

    cfg = load_config(config)

    assert cfg.model.preset_id == "local-gemma-4-e4b-it"
    assert "GLM_API_KEY" not in os.environ


@pytest.mark.unit
def test_load_env_file_false_skips_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """显式传 `load_env_file=False` 时不读取任何 `.env`。"""
    config = tmp_path / "project" / "config" / "setting.yaml"
    home = tmp_path / "home"
    _write_minimal_config(config)
    home.mkdir()
    (home / ".env").write_text("GLM_API_KEY=home-key\n", encoding="utf-8")
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.setenv("KONGMING_HOME", str(home))

    load_config(config, load_env_file=False)

    assert "GLM_API_KEY" not in os.environ


@pytest.mark.unit
def test_real_env_wins_over_dotenv_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """真实进程 env 优先于 home `.env`。"""
    project = tmp_path / "project"
    config = project / "config" / "setting.yaml"
    home = tmp_path / "home"
    _write_minimal_config(config)
    (project / ".env").write_text(
        "GLM_API_KEY=project-glm\n",
        encoding="utf-8",
    )
    home.mkdir()
    (home / ".env").write_text(
        "GLM_API_KEY=home-glm\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KONGMING_HOME", str(home))
    monkeypatch.setenv("GLM_API_KEY", "real-glm")

    cfg = load_config(config)

    assert cfg.model.preset_id == "local-gemma-4-e4b-it"
    assert os.environ["GLM_API_KEY"] == "real-glm"


@pytest.mark.unit
def test_trace_raw_llm_defaults_to_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认 cfg.trace.raw_llm 必须为 False —— 开箱不会自动 dump。"""
    monkeypatch.delenv("KONGMING_TRACE_RAW_LLM", raising=False)
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.trace.raw_llm is False


@pytest.mark.unit
def test_trace_raw_llm_overridden_by_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KONGMING_TRACE_RAW_LLM=1`` 环境变量走标准 env 覆盖链路，生效到 Config。"""
    monkeypatch.setenv("KONGMING_TRACE_RAW_LLM", "1")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.trace.raw_llm is True


@pytest.mark.unit
def test_runtime_selection_env_affects_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """preset env 通过 `_apply_env_overrides` 进入运行时选择。"""
    monkeypatch.setenv("KONGMING_MODEL_PRESET_ID", "bigmodel-glm5-1m")

    # load_env_file=False：测试目的是验证"env var 存在时进入 Config"，
    # 不需要真正加载 .env 文件（避免 .env 内容污染后续测试）
    cfg = load_config(LOCAL_YAML, load_env_file=False)

    assert cfg.model.preset_id == "bigmodel-glm5-1m"


# ---------------------------------------------------------------------------
# reasoning_effort
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reasoning_effort_defaults_to_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """未显式配置 reasoning_effort 时默认为 None —— 开箱不发 reasoning 参数。

    注意：项目 config/setting.yaml 自 commit 7185541 起默认 reasoning_effort="high"（启用 GLM-5 深度
    推理），所以不能直接加载 SETTING_YAML 断言 None。本用例用临时 yaml 验证"不显式配置时的默认值"。
    """
    monkeypatch.delenv("KONGMING_MODEL_REASONING_EFFORT", raising=False)
    minimal_yaml = tmp_path / "minimal.yaml"
    minimal_yaml.write_text(
        "config_schema_version: v0.6\nmodel:\n  preset_id: local-gemma-4-e4b-it\n",
        encoding="utf-8",
    )
    cfg = load_config(minimal_yaml, load_env_file=False)
    assert cfg.model.reasoning_effort is None


@pytest.mark.unit
@pytest.mark.parametrize("effort", ["low", "none"])
def test_reasoning_effort_overridden_by_env(
    monkeypatch: pytest.MonkeyPatch,
    effort: str,
) -> None:
    """``KONGMING_MODEL_REASONING_EFFORT`` 通过 env 覆盖链路生效。"""
    monkeypatch.setenv("KONGMING_MODEL_REASONING_EFFORT", effort)
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.model.reasoning_effort == effort


@pytest.mark.unit
def test_reasoning_effort_invalid_value_raises(
    tmp_path: Path,
) -> None:
    """非法值（非 none/low/medium/high/max）应被 pydantic 拒绝。"""
    cfg_file = tmp_path / "bad_effort.yaml"
    # 读原始 setting.yaml 然后注入非法值
    raw = _read_yaml(SETTING_YAML)
    raw["model"]["reasoning_effort"] = "ultra"
    import yaml as _yaml

    cfg_file.write_text(_yaml.dump(raw), encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        load_config(cfg_file, load_env_file=False)
