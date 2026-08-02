"""unit：``tools.register_schedule_tool_if_enabled`` helper（v0.2 cron 重构）。

设计：``build_default_registry`` 不再 hard-code schedule_tool；调用方先拿
registry，再调本 helper 决定是否补一个 schedule_tool（与 memory_tool 同款
"外部 register"模式）。

覆盖：

- ``cfg.scheduler.enabled=False`` 不 register，返回 ``None``。
- ``cfg.scheduler.enabled=True`` register schedule 工具，返回 :class:`Store`。
- ``cfg.scheduler.home=None`` 时回退到 ``get_kongming_home() / "cron"``。
- ``runtime_factory_fn`` 透传到 :class:`ScheduleTool`（``run_now`` 路径用）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.config.models import (
    ApprovalConfig,
    Config,
    ModelSelectionConfig,
    RunnerConfig,
    SchedulerConfig,
    SessionConfig,
    TraceConfig,
)
from tools import build_default_registry, register_schedule_tool_if_enabled


def _make_cfg(*, enabled: bool, home: Path | None = None) -> Config:
    return Config(
        model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"),
        runner=RunnerConfig(max_turns=3),
        session=SessionConfig(backend="memory", store_path=".kongming/sessions.db"),
        trace=TraceConfig(output_path=".kongming/traces/test.jsonl"),
        approval=ApprovalConfig(mode="auto_allow"),
        scheduler=SchedulerConfig(enabled=enabled, home=home, interval=0.1, max_inflight=2),
    )


@pytest.mark.unit
def test_disabled_returns_none(tmp_path: Path) -> None:
    """cfg.scheduler.enabled=False → 不 register，返回 None。"""
    cfg = _make_cfg(enabled=False)
    reg = build_default_registry(file_enabled=False, shell_enabled=False)

    result = register_schedule_tool_if_enabled(reg, cfg)

    assert result is None
    assert "schedule" not in reg


@pytest.mark.unit
def test_enabled_registers_and_returns_store(tmp_path: Path) -> None:
    """cfg.scheduler.enabled=True → registry 含 schedule，返回 Store 实例。"""
    from scheduler.store import Store

    cron_home = tmp_path / "cron"
    cfg = _make_cfg(enabled=True, home=cron_home)
    reg = build_default_registry(file_enabled=False, shell_enabled=False)

    store = register_schedule_tool_if_enabled(reg, cfg)

    assert store is not None
    assert isinstance(store, Store)
    assert "schedule" in reg
    tool = reg["schedule"]
    assert tool.name == "schedule"
    # Store.__init__ 会 mkdir
    assert cron_home.exists()


@pytest.mark.unit
def test_enabled_uses_default_home_when_none(monkeypatch, tmp_path: Path) -> None:
    """cfg.scheduler.home=None → 用 get_kongming_home() / 'cron'。"""
    fake_home = tmp_path / "kongming-home"
    fake_home.mkdir()

    # patch get_kongming_home（lazy import 在 helper 内部，patch 模块级符号）
    import infrastructure.config.paths as paths_mod

    monkeypatch.setattr(paths_mod, "get_kongming_home", lambda: fake_home)

    cfg = _make_cfg(enabled=True, home=None)
    reg = build_default_registry(file_enabled=False, shell_enabled=False)

    store = register_schedule_tool_if_enabled(reg, cfg)

    assert store is not None
    expected_cron = (fake_home / "cron").resolve()
    assert expected_cron.exists()
    assert "schedule" in reg


@pytest.mark.unit
def test_enabled_resolves_kongming_relative_home(monkeypatch, tmp_path: Path) -> None:
    """cfg.scheduler.home=.kongming/* → 派生到 kongming_home。"""
    home = tmp_path / "home"
    monkeypatch.setenv("KONGMING_HOME", str(home))
    cfg = _make_cfg(enabled=True, home=Path(".kongming/cron-custom"))
    reg = build_default_registry(file_enabled=False, shell_enabled=False)

    store = register_schedule_tool_if_enabled(reg, cfg)

    assert store is not None
    expected_cron = home / "cron-custom"
    assert expected_cron.exists()


@pytest.mark.unit
def test_enabled_passes_runtime_factory_fn(tmp_path: Path) -> None:
    """runtime_factory_fn 透传到 schedule_tool（run_now 走它装配 fresh runtime）。"""
    cron_home = tmp_path / "cron"
    cfg = _make_cfg(enabled=True, home=cron_home)
    reg = build_default_registry(file_enabled=False, shell_enabled=False)

    sentinel = object()

    def _factory(_store: object) -> tuple[object, object]:
        return sentinel, sentinel

    register_schedule_tool_if_enabled(reg, cfg, runtime_factory_fn=_factory)

    tool = reg["schedule"]
    # ScheduleTool 把 runtime_factory_fn 存到 _runtime_factory（私有但稳定）
    assert getattr(tool, "_runtime_factory", None) is _factory
