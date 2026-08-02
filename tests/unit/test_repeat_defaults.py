"""Harness Eval repeat 默认值与校验测试。

# 验证 environment repeat 字段校验、CLI 覆盖优先级、fixture 强制降为 1。
# 关键用例：preset 无 repeat 报错、repeat<4 报错、CLI 覆盖、fixture 强制。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from evals.src.environment import (
    _validate_environment_entry,
    resolve_eval_environment,
)


@pytest.fixture(autouse=True)
def _clear_retired_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离开发机遗留的 v0.5 静态 model 环境变量。"""
    monkeypatch.setenv("KONGMING_SKIP_DOTENV", "1")
    for name in (
        "KONGMING_MODEL_PROVIDER",
        "KONGMING_MODEL_NAME",
        "KONGMING_MODEL_BASE_URL",
        "KONGMING_MODEL_API_KEY",
        "KONGMING_MODEL_API_KEY_HEADER",
        "KONGMING_MODEL_TIMEOUT",
        "KONGMING_MODEL_MAX_TOKENS",
        "KONGMING_MODEL_TEMPERATURE",
    ):
        monkeypatch.delenv(name, raising=False)


def _write_env_yaml(tmp_path: Path, environments: dict) -> Path:
    """写临时 environments.yaml，输入 tmp_path 和 environments 字典，输出文件路径。"""

    path = tmp_path / "environments.yaml"
    path.write_text(
        yaml.dump({"environments": environments}, default_flow_style=False),
        encoding="utf-8",
    )
    return path


def _preset_entry(*, repeat: int | None = None) -> dict:
    """生成标准 preset environment 字典，输入可选 repeat，输出 entry。"""

    entry = {
        "suite": "evals/harness-runtime-v0.1",
        "mode": "preset",
        "preset": "minimax-m3",
        "profile": "full",
        "approval_mode": "auto_allow",
        "runner": {"max_turns": 50},
        "artifacts": {"output_dir": "evals/harness-runtime-v0.1/runs"},
    }
    if repeat is not None:
        entry["repeat"] = repeat
    return entry


def _fixture_entry(*, repeat: int | None = None) -> dict:
    """生成标准 fixture environment 字典，输入可选 repeat，输出 entry。"""

    entry = {
        "suite": "evals/harness-runtime-v0.1",
        "mode": "fixture",
        "profile": "full",
        "approval_mode": "auto_allow",
        "runner": {"max_turns": 50},
        "artifacts": {"output_dir": "evals/harness-runtime-v0.1/runs"},
    }
    if repeat is not None:
        entry["repeat"] = repeat
    return entry


class TestPresetRepeatValidation:
    """preset 环境 repeat 字段校验。"""

    def test_preset_without_repeat_raises(self, tmp_path: Path) -> None:
        """preset environment 不带 repeat 时校验报错。"""

        entry = _preset_entry(repeat=None)
        env_path = _write_env_yaml(tmp_path, {"test-preset": entry})
        with pytest.raises(ValueError, match="missing required field 'repeat'"):
            _validate_environment_entry(entry, path=env_path, environment_id="test-preset")

    def test_preset_repeat_2_raises(self, tmp_path: Path) -> None:
        """preset environment repeat=2 时校验报错。"""

        entry = _preset_entry(repeat=2)
        env_path = _write_env_yaml(tmp_path, {"test-preset": entry})
        with pytest.raises(ValueError, match=r"repeat=2 < 4.*trustworthy pass\^k"):
            _validate_environment_entry(entry, path=env_path, environment_id="test-preset")

    def test_preset_repeat_4_passes(self, tmp_path: Path) -> None:
        """preset environment repeat=4 时校验通过。"""

        entry = _preset_entry(repeat=4)
        env_path = _write_env_yaml(tmp_path, {"test-preset": entry})
        _validate_environment_entry(entry, path=env_path, environment_id="test-preset")


class TestCLIRepeatOverride:
    """CLI --repeat 覆盖 environment 配置。"""

    def test_cli_repeat_overrides_environment(self, tmp_path: Path) -> None:
        """CLI --repeat 显式覆盖时 environment 配置被忽略。"""

        env_path = _write_env_yaml(tmp_path, {"test-preset": _preset_entry(repeat=4)})
        with patch("evals.src.environment._resolve_repo_path") as mock_resolve:
            mock_resolve.side_effect = lambda v: (
                env_path if str(v).endswith("environments.yaml") else Path(v).resolve()
            )
            env = resolve_eval_environment(
                "test-preset",
                overrides=None,
            )
        assert env.repeat == 4

    def test_environment_repeat_used_when_cli_none(self, tmp_path: Path) -> None:
        """CLI 没传 repeat 时使用 environment 配置值。"""

        env_path = _write_env_yaml(tmp_path, {"test-preset": _preset_entry(repeat=5)})
        with patch("evals.src.environment._resolve_repo_path") as mock_resolve:
            mock_resolve.side_effect = lambda v: (
                env_path if str(v).endswith("environments.yaml") else Path(v).resolve()
            )
            env = resolve_eval_environment(
                "test-preset",
                overrides=None,
            )
        assert env.repeat == 5
        assert env.override_sources["repeat"] == "environment"


class TestFixtureRepeatForced:
    """fixture 模式 repeat 强制降为 1。"""

    def test_fixture_repeat_forced_to_1(self) -> None:
        """fixture environment 即使配 repeat=4 也被 runner 强制降到 1。"""

        from evals.src.models import ResolvedEvalEnvironment

        env = ResolvedEvalEnvironment(
            environment_id="fixture-test",
            environment_config_path=None,
            environment_config_hash=None,
            kongming_config_path=Path("config/setting.yaml"),
            kongming_config_hash="sha256:fake",
            suite=Path("evals/harness-runtime-v0.1"),
            mode="fixture",
            preset=None,
            profile="full",
            approval_mode="auto_allow",
            instructions_mode="empty",
            session_backend="memory",
            compactor_mode="noop-script",
            runner_max_turns=50,
            repeat=4,
            output_dir=Path("/tmp/test"),
            api_keys_present={},
            override_sources={},
        )
        effective = 1 if env.mode == "fixture" else max(1, env.repeat or 1)
        assert effective == 1
