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

from config_loader import load_config
from config_loader.models import Config, ModelConfig, SchedulerApprovalConfig, SchedulerConfig

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SETTING_YAML = REPO_ROOT / "config" / "setting.yaml"


def _minimal_model() -> ModelConfig:
    """Build the smallest valid model config for Config(model=...)."""
    return ModelConfig(name="test-model", base_url="http://127.0.0.1:1234/v1", api_key="")


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
    monkeypatch.setenv(
        "KONGMING_SCHEDULER_APPROVAL_ALLOW_WRITE_FILE_CREATE_IN_CWD", "false"
    )
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
        "default_timezone",
        "enabled",
        "home",
        "interval",
        "max_inflight",
        "max_task_age_seconds",
    }
