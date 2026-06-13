"""unit：infrastructure.config.load_config 基本行为。

- 从 yaml 加载出合法 Config
- 环境变量覆盖单字段
- 本地 base_url 可空 api_key
- 远端 base_url + 空 api_key 抛 ConfigValidationError
- 文件不存在抛 ConfigLoadError
"""

from __future__ import annotations

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
    assert cfg.model.name == raw["model"]["name"]
    # pydantic 会标准化 URL（去掉末尾 /），比较时统一 rstrip 再断言等价
    assert cfg.model.base_url.rstrip("/") == raw["model"]["base_url"].rstrip("/")
    assert cfg.runner.max_turns == raw["runner"]["max_turns"]
    assert cfg.approval.mode == raw["approval"]["mode"]


@pytest.mark.unit
def test_load_setting_yaml_has_empty_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # 隔离环境变量，还原"纯 yaml 无 env 覆盖"场景
    monkeypatch.delenv("KONGMING_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("KONGMING_MODEL_BASE_URL", raising=False)
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.model.api_key == ""
    assert cfg.model.is_local is True


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
    cfg = load_config(LOCAL_YAML, load_env_file=False)
    assert cfg.model.api_key == "sk-xyz"


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
    cfg = load_config(LOCAL_YAML, load_env_file=False)
    assert cfg.model.base_url == "http://127.0.0.1:9999"


@pytest.mark.unit
def test_remote_url_without_api_key_triggers_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """远端 url + 空 api_key 应当在 pydantic 校验阶段被拒。"""
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
    cfg = load_config(load_env_file=False)
    assert cfg.model.base_url == "http://127.0.0.1:1234/v1"


# ---------------------------------------------------------------------------
# .env 加载行为
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_config_calls_load_dotenv_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认 `load_env_file=True` 时 `load_config` 会调用 `dotenv.load_dotenv`。

    不直接测 dotenv 的路径搜索策略（那是 dotenv 自己的职责），而是 mock 它，
    断言"调了 + 带 override=False 参数"——这两点是本项目对 dotenv 的契约。
    """
    calls: list[dict[str, Any]] = []

    def fake_load_dotenv(*args: Any, **kwargs: Any) -> bool:
        calls.append({"args": args, "kwargs": kwargs})
        return True

    monkeypatch.delenv("KONGMING_SKIP_DOTENV", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", fake_load_dotenv)

    load_config(LOCAL_YAML)

    assert len(calls) == 1
    # 必须带 override=False：真实 env 优先，.env 不覆盖
    assert calls[0]["kwargs"].get("override") is False


@pytest.mark.unit
def test_load_env_file_false_skips_dotenv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式传 `load_env_file=False` 时不调用 `load_dotenv`。"""
    calls: list[dict[str, Any]] = []

    def fake_load_dotenv(*args: Any, **kwargs: Any) -> bool:
        calls.append({"args": args, "kwargs": kwargs})
        return True

    monkeypatch.setattr("dotenv.load_dotenv", fake_load_dotenv)

    load_config(LOCAL_YAML, load_env_file=False)

    assert calls == []


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
def test_dotenv_injected_env_vars_affect_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模拟 `.env` 被 dotenv 加载后的等价状态（env 变量已注入），
    验证这些变量真的通过 `_apply_env_overrides` 链路进入 Config。

    这是端到端契约：把值放进 `.env` 和把值 `export` 出来，效果必须一致。
    """
    monkeypatch.setenv("KONGMING_MODEL_API_KEY", "from-dotenv-simulated")

    # load_env_file=False：测试目的是验证"env var 存在时进入 Config"，
    # 不需要真正加载 .env 文件（避免 .env 内容污染后续测试）
    cfg = load_config(LOCAL_YAML, load_env_file=False)

    # LOCAL_YAML 的 api_key 是空字符串，被注入的 env 覆盖后应该是测试值
    assert cfg.model.api_key == "from-dotenv-simulated"


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
        "model:\n  name: test-model\n  base_url: http://127.0.0.1:1234/v1\n  api_key: ''\n",
        encoding="utf-8",
    )
    cfg = load_config(minimal_yaml, load_env_file=False)
    assert cfg.model.reasoning_effort is None


@pytest.mark.unit
def test_reasoning_effort_overridden_by_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KONGMING_MODEL_REASONING_EFFORT=low`` 通过 env 覆盖链路生效。"""
    monkeypatch.setenv("KONGMING_MODEL_REASONING_EFFORT", "low")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.model.reasoning_effort == "low"


@pytest.mark.unit
def test_reasoning_effort_invalid_value_raises(
    tmp_path: Path,
) -> None:
    """非法值（非 low/medium/high）应被 pydantic 拒绝。"""
    cfg_file = tmp_path / "bad_effort.yaml"
    # 读原始 setting.yaml 然后注入非法值
    raw = _read_yaml(SETTING_YAML)
    raw["model"]["reasoning_effort"] = "ultra"
    import yaml as _yaml

    cfg_file.write_text(_yaml.dump(raw), encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        load_config(cfg_file, load_env_file=False)
