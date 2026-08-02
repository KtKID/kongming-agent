"""Scheduler config unit tests.

Coverage:
- SchedulerConfig defaults and pydantic bounds.
- KONGMING_SCHEDULER_* env overrides.
- Invalid scheduler env values falling back with warnings.
- setting.yaml default scheduler timezone regression coverage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from infrastructure.config import load_config
from infrastructure.config.models import (
    Config,
    ModelSelectionConfig,
    SchedulerApprovalConfig,
    SchedulerConfig,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SETTING_YAML = REPO_ROOT / "config" / "setting.yaml"


def _minimal_model() -> ModelSelectionConfig:
    """Build the smallest valid model config for Config(model=...)."""
    return ModelSelectionConfig(preset_id="local-gemma-4-e4b-it")


# ---------------------------------------------------------------------------
# defaults
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scheduler_config_defaults_match_spec() -> None:
    """SchedulerConfig defaults should match the scheduler spec."""
    cfg = SchedulerConfig()
    assert cfg.enabled is True
    assert cfg.home is None
    assert cfg.interval == 1.0
    assert cfg.max_inflight == 8
    assert cfg.max_task_age_seconds is None
    assert isinstance(cfg.approval, SchedulerApprovalConfig)
    assert cfg.approval.allow_write_file_create_in_cwd is True


@pytest.mark.unit
def test_config_default_scheduler_field_is_scheduler_config() -> None:
    """Config(model=...) should materialize a SchedulerConfig by default."""
    cfg = Config(model=_minimal_model())
    assert isinstance(cfg.scheduler, SchedulerConfig)
    assert cfg.scheduler.enabled is True
    assert cfg.scheduler.interval == 1.0
    assert cfg.scheduler.max_inflight == 8
    assert cfg.scheduler.approval.allow_write_file_create_in_cwd is True


# ---------------------------------------------------------------------------
# pydantic validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scheduler_interval_below_minimum_raises() -> None:
    """interval < 0.1 should be rejected."""
    with pytest.raises(ValidationError):
        SchedulerConfig(interval=0.05)


@pytest.mark.unit
def test_scheduler_max_inflight_zero_raises() -> None:
    """max_inflight < 1 should be rejected."""
    with pytest.raises(ValidationError):
        SchedulerConfig(max_inflight=0)


@pytest.mark.unit
def test_scheduler_accepts_path_for_home() -> None:
    """home should accept a Path instance."""
    home = Path("/tmp/kongming-test/cron")
    cfg = SchedulerConfig(home=home)
    assert cfg.home == home


# ---------------------------------------------------------------------------
# env overrides
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_env_override_scheduler_enabled_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KONGMING_SCHEDULER_ENABLED=false disables the scheduler."""
    monkeypatch.setenv("KONGMING_SCHEDULER_ENABLED", "false")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.enabled is False


@pytest.mark.unit
def test_env_override_scheduler_enabled_true_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Truthy enabled tokens should enable the scheduler."""
    monkeypatch.setenv("KONGMING_SCHEDULER_ENABLED", "1")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.enabled is True


@pytest.mark.unit
def test_env_override_scheduler_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KONGMING_SCHEDULER_INTERVAL should override the ticker interval."""
    monkeypatch.setenv("KONGMING_SCHEDULER_INTERVAL", "2.5")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.interval == 2.5


@pytest.mark.unit
def test_env_override_scheduler_max_inflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KONGMING_SCHEDULER_MAX_INFLIGHT should override max inflight."""
    monkeypatch.setenv("KONGMING_SCHEDULER_MAX_INFLIGHT", "16")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.max_inflight == 16


@pytest.mark.unit
def test_env_override_scheduler_write_file_create_whitelist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested scheduler approval env should override the whitelist toggle."""
    monkeypatch.setenv("KONGMING_SCHEDULER_APPROVAL_ALLOW_WRITE_FILE_CREATE_IN_CWD", "false")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.approval.allow_write_file_create_in_cwd is False


# ---------------------------------------------------------------------------
# invalid env values fall back with warnings
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_env_invalid_interval_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Invalid interval env should warn and keep the default value."""
    monkeypatch.setenv("KONGMING_SCHEDULER_INTERVAL", "abc")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.interval == 1.0
    captured = capsys.readouterr()
    assert "KONGMING_SCHEDULER_INTERVAL" in captured.err
    assert "abc" in captured.err


@pytest.mark.unit
def test_env_invalid_max_inflight_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Invalid max inflight env should warn and keep the default value."""
    monkeypatch.setenv("KONGMING_SCHEDULER_MAX_INFLIGHT", "lots")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.max_inflight == 8
    captured = capsys.readouterr()
    assert "KONGMING_SCHEDULER_MAX_INFLIGHT" in captured.err


@pytest.mark.unit
def test_env_invalid_enabled_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Invalid enabled env should warn and keep the default value."""
    monkeypatch.setenv("KONGMING_SCHEDULER_ENABLED", "maybe")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.enabled is True
    captured = capsys.readouterr()
    assert "KONGMING_SCHEDULER_ENABLED" in captured.err


# ---------------------------------------------------------------------------
# non-scheduler envs should keep working
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_other_env_overrides_still_work_alongside_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheduler env parsing should not break other KONGMING_* overrides."""
    monkeypatch.setenv("KONGMING_SCHEDULER_INTERVAL", "3.0")
    monkeypatch.setenv("KONGMING_RUNNER_MAX_TURNS", "99")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.interval == 3.0
    assert cfg.runner.max_turns == 99


@pytest.mark.unit
def test_load_config_default_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_config should pick scheduler defaults from setting.yaml."""
    for name in (
        "KONGMING_SCHEDULER_ENABLED",
        "KONGMING_SCHEDULER_INTERVAL",
        "KONGMING_SCHEDULER_MAX_INFLIGHT",
        "KONGMING_SCHEDULER_APPROVAL_ALLOW_WRITE_FILE_CREATE_IN_CWD",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.enabled is True
    assert cfg.scheduler.interval == 1.0
    assert cfg.scheduler.max_inflight == 8
    assert cfg.scheduler.default_timezone == "Asia/Shanghai"
    assert cfg.scheduler.approval.allow_write_file_create_in_cwd is True


# ---------------------------------------------------------------------------
# schema guardrail
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scheduler_config_has_expected_field_set() -> None:
    """SchedulerConfig should retain the expected field set."""
    fields: dict[str, Any] = SchedulerConfig.model_fields
    assert set(fields.keys()) == {
        "approval",
        "default_delivery_channel",
        "default_max_turns",
        "default_timezone",
        "enabled",
        "home",
        "interval",
        "max_inflight",
        "max_task_age_seconds",
    }


# ---------------------------------------------------------------------------
# v0.5: scheduler.approval.mode
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scheduler_approval_mode_default_is_trust() -> None:
    """C0: 默认值 mode='trust'（v0.5 调整：cron 即用户预批准任务，不应再次审批）。

    严格审批需在 yaml / env 显式声明 'fail_closed'。
    """
    cfg = SchedulerApprovalConfig()
    assert cfg.mode == "trust"


@pytest.mark.unit
def test_scheduler_approval_mode_yaml_fail_closed() -> None:
    """C0b: 显式 yaml 'fail_closed' 仍能切回严格模式。"""
    cfg = SchedulerApprovalConfig(mode="fail_closed")
    assert cfg.mode == "fail_closed"


@pytest.mark.unit
def test_scheduler_approval_mode_yaml_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: yaml 配置 scheduler.approval.mode='trust' 解析正常。"""
    for name in (
        "KONGMING_SCHEDULER_APPROVAL_MODE",
        "KONGMING_SCHEDULER_APPROVAL_ALLOW_WRITE_FILE_CREATE_IN_CWD",
    ):
        monkeypatch.delenv(name, raising=False)
    yaml_path = tmp_path / "setting.yaml"
    yaml_path.write_text(
        "model:\n"
        '  name: "test-model"\n'
        '  base_url: "http://127.0.0.1:1234/v1"\n'
        '  api_key: ""\n'
        "scheduler:\n"
        "  approval:\n"
        '    mode: "trust"\n',
        encoding="utf-8",
    )
    cfg = load_config(yaml_path, load_env_file=False)
    assert cfg.scheduler.approval.mode == "trust"


@pytest.mark.unit
def test_scheduler_approval_mode_invalid_yaml_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2: yaml 配置 mode='invalid' 触发 pydantic ValidationError。"""
    for name in (
        "KONGMING_SCHEDULER_APPROVAL_MODE",
        "KONGMING_SCHEDULER_APPROVAL_ALLOW_WRITE_FILE_CREATE_IN_CWD",
    ):
        monkeypatch.delenv(name, raising=False)
    yaml_path = tmp_path / "setting.yaml"
    yaml_path.write_text(
        "model:\n"
        '  name: "test-model"\n'
        '  base_url: "http://127.0.0.1:1234/v1"\n'
        '  api_key: ""\n'
        "scheduler:\n"
        "  approval:\n"
        '    mode: "invalid"\n',
        encoding="utf-8",
    )
    # load_config 把 pydantic ValidationError 包成 ConfigValidationError
    from infrastructure.config.errors import ConfigValidationError

    with pytest.raises((ValidationError, ConfigValidationError)):
        load_config(yaml_path, load_env_file=False)


@pytest.mark.unit
def test_scheduler_approval_mode_env_override_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C3: KONGMING_SCHEDULER_APPROVAL_MODE=trust 覆盖 yaml 默认。"""
    monkeypatch.setenv("KONGMING_SCHEDULER_APPROVAL_MODE", "trust")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.approval.mode == "trust"


@pytest.mark.unit
def test_scheduler_approval_mode_invalid_env_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """C4: 非法 env 值发出 warning 并回退 yaml/pydantic 默认（v0.5 调整后为 trust）。"""
    monkeypatch.setenv("KONGMING_SCHEDULER_APPROVAL_MODE", "wrong")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    # config/setting.yaml 的 scheduler.approval 段未显式写 mode → 走 pydantic 默认（trust）
    assert cfg.scheduler.approval.mode == "trust"
    captured = capsys.readouterr()
    assert "KONGMING_SCHEDULER_APPROVAL_MODE" in captured.err
    assert "wrong" in captured.err


@pytest.mark.unit
def test_scheduler_approval_mode_env_override_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C5: KONGMING_SCHEDULER_APPROVAL_MODE=fail_closed 显式覆盖 yaml/默认。"""
    monkeypatch.setenv("KONGMING_SCHEDULER_APPROVAL_MODE", "fail_closed")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.approval.mode == "fail_closed"


# ---------------------------------------------------------------------------
# v0.5.1: scheduler.default_max_turns
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scheduler_default_max_turns_pydantic_default_is_90() -> None:
    """T1: SchedulerConfig.default_max_turns 默认值 = 90（v0.5.1 调整）。"""
    cfg = SchedulerConfig()
    assert cfg.default_max_turns == 90


@pytest.mark.unit
def test_scheduler_default_max_turns_yaml_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2: yaml 显式 default_max_turns 覆盖 pydantic 默认。"""
    monkeypatch.delenv("KONGMING_SCHEDULER_DEFAULT_MAX_TURNS", raising=False)
    yaml_path = tmp_path / "setting.yaml"
    yaml_path.write_text(
        "model:\n"
        '  name: "test-model"\n'
        '  base_url: "http://127.0.0.1:1234/v1"\n'
        '  api_key: ""\n'
        "scheduler:\n"
        "  default_max_turns: 50\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path, load_env_file=False)
    assert cfg.scheduler.default_max_turns == 50


@pytest.mark.unit
def test_scheduler_default_max_turns_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T3: KONGMING_SCHEDULER_DEFAULT_MAX_TURNS=120 env 覆盖 yaml。"""
    monkeypatch.setenv("KONGMING_SCHEDULER_DEFAULT_MAX_TURNS", "120")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.default_max_turns == 120


@pytest.mark.unit
def test_scheduler_default_max_turns_invalid_env_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T4: 非法 env 值发出 warning 并回退（yaml 或 pydantic 默认）。"""
    monkeypatch.setenv("KONGMING_SCHEDULER_DEFAULT_MAX_TURNS", "abc")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    # SETTING_YAML 里有 default_max_turns: 90（v0.5.1 显式声明）
    assert cfg.scheduler.default_max_turns == 90
    captured = capsys.readouterr()
    assert "KONGMING_SCHEDULER_DEFAULT_MAX_TURNS" in captured.err
    assert "abc" in captured.err


@pytest.mark.unit
def test_scheduler_default_max_turns_zero_env_rejected(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T5: env 值 <= 0 触发 warning 并回退（Field gt=0 约束）。"""
    monkeypatch.setenv("KONGMING_SCHEDULER_DEFAULT_MAX_TURNS", "0")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.default_max_turns == 90
    captured = capsys.readouterr()
    assert "must be > 0" in captured.err


@pytest.mark.unit
def test_scheduler_default_max_turns_yaml_zero_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T6: yaml 显式 0 触发 pydantic ValidationError（gt=0）。"""
    monkeypatch.delenv("KONGMING_SCHEDULER_DEFAULT_MAX_TURNS", raising=False)
    yaml_path = tmp_path / "setting.yaml"
    yaml_path.write_text(
        "model:\n"
        '  name: "test-model"\n'
        '  base_url: "http://127.0.0.1:1234/v1"\n'
        '  api_key: ""\n'
        "scheduler:\n"
        "  default_max_turns: 0\n",
        encoding="utf-8",
    )
    from infrastructure.config.errors import ConfigValidationError

    with pytest.raises((ValidationError, ConfigValidationError)):
        load_config(yaml_path, load_env_file=False)
