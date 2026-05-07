"""unit：SchedulerConfig 默认值 + env 覆盖 + 非法值降级。

覆盖 v0.2 cron 配置层契约：

- ``Config()`` 默认实例化时 ``cfg.scheduler`` 走 :class:`SchedulerConfig` 默认值
- pydantic 字段约束（``interval >= 0.1`` / ``max_inflight >= 1``）能拒非法值
- ``KONGMING_SCHEDULER_*`` env 覆盖三件套（enabled / interval / max_inflight）
- 非法 env（如 ``INTERVAL=abc``）→ stderr warning + 用默认值，不抛异常

为什么 scheduler 走"非法值降级"而不是抛 ConfigValidationError：
cron 配置错不应让整个进程起不来；其他 section 的 env 解析继续走 pydantic
强校验路径。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from config_loader import load_config
from config_loader.models import Config, ModelConfig, SchedulerConfig

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SETTING_YAML = REPO_ROOT / "config" / "setting.yaml"


def _minimal_model() -> ModelConfig:
    """构造合法的最小 ModelConfig，专供 ``Config(model=...)`` 默认实例化测试用。"""
    return ModelConfig(name="test-model", base_url="http://127.0.0.1:1234/v1", api_key="")


# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scheduler_config_defaults_match_spec() -> None:
    """``SchedulerConfig()`` 默认值与 v0.2 plan 模块 5 设计完全一致。"""
    cfg = SchedulerConfig()
    assert cfg.enabled is True
    assert cfg.home is None
    assert cfg.interval == 1.0
    assert cfg.max_inflight == 8
    assert cfg.max_task_age_seconds is None


@pytest.mark.unit
def test_config_default_scheduler_field_is_scheduler_config() -> None:
    """``Config(model=...)`` 默认实例化时 ``scheduler`` 是 :class:`SchedulerConfig`。"""
    cfg = Config(model=_minimal_model())
    assert isinstance(cfg.scheduler, SchedulerConfig)
    assert cfg.scheduler.enabled is True
    assert cfg.scheduler.interval == 1.0
    assert cfg.scheduler.max_inflight == 8


# ---------------------------------------------------------------------------
# pydantic 字段约束
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scheduler_interval_below_minimum_raises() -> None:
    """``interval < 0.1`` 应被 pydantic 拒，防 ticker 把 CPU 烧穿。"""
    with pytest.raises(ValidationError):
        SchedulerConfig(interval=0.05)


@pytest.mark.unit
def test_scheduler_max_inflight_zero_raises() -> None:
    """``max_inflight < 1`` 没有意义，pydantic 拒。"""
    with pytest.raises(ValidationError):
        SchedulerConfig(max_inflight=0)


@pytest.mark.unit
def test_scheduler_accepts_path_for_home() -> None:
    """``home`` 接受 :class:`Path` 实例（cron Store 解析时的入参）。"""
    home = Path("/tmp/kongming-test/cron")
    cfg = SchedulerConfig(home=home)
    assert cfg.home == home


# ---------------------------------------------------------------------------
# env 覆盖
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_env_override_scheduler_enabled_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KONGMING_SCHEDULER_ENABLED=false`` 关停 cron 模块。"""
    monkeypatch.setenv("KONGMING_SCHEDULER_ENABLED", "false")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.enabled is False


@pytest.mark.unit
def test_env_override_scheduler_enabled_true_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KONGMING_SCHEDULER_ENABLED=1`` / ``yes`` 等 truthy token 也生效。"""
    monkeypatch.setenv("KONGMING_SCHEDULER_ENABLED", "1")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.enabled is True


@pytest.mark.unit
def test_env_override_scheduler_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KONGMING_SCHEDULER_INTERVAL=2.5`` 把 ticker 间隔改成 2.5s。"""
    monkeypatch.setenv("KONGMING_SCHEDULER_INTERVAL", "2.5")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.interval == 2.5


@pytest.mark.unit
def test_env_override_scheduler_max_inflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KONGMING_SCHEDULER_MAX_INFLIGHT=16`` 把并发上限改成 16。"""
    monkeypatch.setenv("KONGMING_SCHEDULER_MAX_INFLIGHT", "16")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.max_inflight == 16


# ---------------------------------------------------------------------------
# env 非法值降级（warning + default）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_env_invalid_interval_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``KONGMING_SCHEDULER_INTERVAL=abc`` → stderr warning + 用默认值 1.0。"""
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
    """``KONGMING_SCHEDULER_MAX_INFLIGHT=lots`` → stderr warning + 默认 8。"""
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
    """``KONGMING_SCHEDULER_ENABLED=maybe`` → stderr warning + 默认 True。"""
    monkeypatch.setenv("KONGMING_SCHEDULER_ENABLED", "maybe")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.enabled is True
    captured = capsys.readouterr()
    assert "KONGMING_SCHEDULER_ENABLED" in captured.err


# ---------------------------------------------------------------------------
# 非 scheduler 字段保持原有行为（防回归）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_other_env_overrides_still_work_alongside_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """加 scheduler env 解析不能破坏现有 ``KONGMING_*`` env 链路。"""
    monkeypatch.setenv("KONGMING_SCHEDULER_INTERVAL", "3.0")
    monkeypatch.setenv("KONGMING_RUNNER_MAX_TURNS", "99")
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.interval == 3.0
    assert cfg.runner.max_turns == 99


@pytest.mark.unit
def test_load_config_default_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 ``KONGMING_SCHEDULER_*`` env 时 ``load_config`` 出来的 scheduler 走默认值。"""
    for name in (
        "KONGMING_SCHEDULER_ENABLED",
        "KONGMING_SCHEDULER_INTERVAL",
        "KONGMING_SCHEDULER_MAX_INFLIGHT",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = load_config(SETTING_YAML, load_env_file=False)
    assert cfg.scheduler.enabled is True
    assert cfg.scheduler.interval == 1.0
    assert cfg.scheduler.max_inflight == 8


# ---------------------------------------------------------------------------
# 类型注解防漏测（mypy 过的话本测试是 redundant，但留作 sanity check）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scheduler_config_has_expected_field_set() -> None:
    """SchedulerConfig 字段集合必须与 plan.md 模块 5 一致；防字段被悄悄删。"""
    fields: dict[str, Any] = SchedulerConfig.model_fields
    assert set(fields.keys()) == {
        "default_delivery_channel",
        "default_timezone",
        "enabled",
        "home",
        "interval",
        "max_inflight",
        "max_task_age_seconds",
    }
