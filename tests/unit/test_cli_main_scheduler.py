"""unit：``cli/main.py`` 起 cron ticker + 优雅退出（v0.2 #18）。

v0.2 cron 装配重构后：cli 用 :func:`tools.register_schedule_tool_if_enabled`
helper 把 schedule_tool 外部 register 进 registry，并拿到 :class:`Store`
作为局部变量 ``scheduler_store``；ticker 直接用该 store。

覆盖：

- ``cfg.scheduler.enabled=True`` + register helper 返回非 ``None`` store 时
  cli 起 ticker（``run_ticker_loop`` 被调一次）；shutdown 时 set stop_event
  并 await ticker_task。
- ``cfg.scheduler.enabled=False`` 时不起 ticker（helper 返回 ``None``）。
- ticker 收尾后 ``ticker_runtime.aclose()`` 被调一次（释放 httpx 连接池）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import hosts.cli.main as cli_main
from infrastructure.config.models import (
    ApprovalConfig,
    Config,
    ModelConfig,
    RunnerConfig,
    SchedulerConfig,
    SessionConfig,
    TraceConfig,
)


class _DummyRuntime:
    async def aclose(self) -> None:
        return None


class _DummyRegistry:
    """注册表 stub：register / names 最小子集。"""

    def register(self, _tool: object) -> None:
        return None

    def names(self) -> list[str]:
        return []


class _DummyBridge:
    def __init__(
        self,
        *,
        runtime: object,
        adapter: object,
        session_id: str,
        echo_final_content: bool = True,
    ) -> None:
        del runtime, adapter, echo_final_content
        self.session_id = session_id

    async def run_loop(self) -> None:
        return None


def _build_cfg(*, scheduler_enabled: bool) -> Config:
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="stub-model",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        runner=RunnerConfig(max_turns=3),
        session=SessionConfig(backend="memory", store_path=".kongming/sessions.db"),
        trace=TraceConfig(output_path=".kongming/traces/test.jsonl"),
        approval=ApprovalConfig(mode="auto_allow"),
        scheduler=SchedulerConfig(enabled=scheduler_enabled, interval=0.1, max_inflight=2),
    )


def _common_monkeypatches(
    monkeypatch,
    registry: _DummyRegistry,
    cfg: Config,
    *,
    register_returns: object | None = None,
) -> None:
    monkeypatch.setattr(cli_main, "_load_config_or_exit", lambda _: cfg)

    async def _fake_assemble(_cfg, _files, *, skill_listing=""):
        return "system", [], None

    monkeypatch.setattr(cli_main, "_assemble_instructions", _fake_assemble)
    monkeypatch.setattr(cli_main, "build_default_registry", lambda **_: registry)

    # 关键：mock register_schedule_tool_if_enabled，返回 sentinel store。
    def _fake_register(_reg, _cfg, *, runtime_factory_fn=None):  # type: ignore[no-untyped-def]
        del _reg, _cfg, runtime_factory_fn
        return register_returns

    monkeypatch.setattr(cli_main, "register_schedule_tool_if_enabled", _fake_register)

    monkeypatch.setattr(cli_main, "build_default_approval", lambda *_, **__: object())
    monkeypatch.setattr(
        cli_main.NativeRuntime, "build", staticmethod(lambda *_, **__: _DummyRuntime())
    )
    monkeypatch.setattr(cli_main, "SessionBridge", _DummyBridge)
    monkeypatch.setattr(cli_main, "_generate_cli_session_id", lambda: "cli-test")
    monkeypatch.setattr(cli_main, "_print_banner", lambda *_, **__: None)
    monkeypatch.setattr(cli_main, "_discover_persistent_sessions", lambda _cfg: ([], None))

    async def _fake_load_skill_specs(*args, **kwargs):
        return []

    monkeypatch.setattr(cli_main, "load_skill_specs", _fake_load_skill_specs)
    monkeypatch.setattr(cli_main, "format_skill_listing", lambda _specs: "")


async def test_cli_starts_ticker_when_scheduler_enabled(monkeypatch, tmp_path: Path) -> None:
    """scheduler.enabled=True + register helper 返回非空 store → ticker 起跑 + 优雅退出。"""
    captured: dict[str, object] = {"ticker_calls": 0, "factory_calls": 0, "aclose_calls": 0}

    sentinel_store = object()
    registry = _DummyRegistry()
    cfg = _build_cfg(scheduler_enabled=True)
    _common_monkeypatches(monkeypatch, registry, cfg, register_returns=sentinel_store)

    class _FakeTickerRuntime:
        async def aclose(self) -> None:
            captured["aclose_calls"] = int(captured["aclose_calls"]) + 1  # type: ignore[arg-type]

    def _fake_build_bridge(_cfg, _store, *, event_sinks=None, dispatcher=None, **_kwargs):
        captured["factory_calls"] = int(captured["factory_calls"]) + 1  # type: ignore[arg-type]
        captured["bridge_store"] = _store
        # v0.3 M4/M5：cli/main.py 装配链路传 dispatcher；fake 接受但不强求非 None
        captured["dispatcher_set"] = dispatcher is not None
        return _FakeTickerRuntime(), object()

    async def _fake_run_ticker_loop(_store, _bridge, stop_event: asyncio.Event, **kwargs) -> None:
        captured["ticker_calls"] = int(captured["ticker_calls"]) + 1  # type: ignore[arg-type]
        captured["interval"] = kwargs.get("interval")
        captured["max_inflight"] = kwargs.get("max_inflight")
        # 等 stop_event；本测验证退出路径优雅 set + await。
        await stop_event.wait()

    import scheduler.runtime_factory as rf
    import scheduler.ticker as tk

    monkeypatch.setattr(rf, "build_cron_execution_bridge", _fake_build_bridge)
    monkeypatch.setattr(tk, "run_ticker_loop", _fake_run_ticker_loop)

    await cli_main._run(
        config_path=None,
        session_id=None,
        list_sessions=False,
        resume_last=False,
        verbose=False,
        smoke=False,
        instructions_files=[],
        trace_enabled=False,
        reasoning_effort=None,
    )

    assert captured["factory_calls"] == 1
    assert captured["ticker_calls"] == 1
    assert captured["bridge_store"] is sentinel_store
    assert captured["interval"] == 0.1
    assert captured["max_inflight"] == 2
    assert captured["aclose_calls"] == 1


async def test_cli_does_not_start_ticker_when_scheduler_disabled(
    monkeypatch, tmp_path: Path
) -> None:
    """scheduler.enabled=False → register helper 返回 None → ticker 不起。"""
    captured: dict[str, int] = {"factory_calls": 0, "ticker_calls": 0}

    registry = _DummyRegistry()
    cfg = _build_cfg(scheduler_enabled=False)
    _common_monkeypatches(monkeypatch, registry, cfg, register_returns=None)

    def _fake_build_bridge(*_args, **_kwargs):
        captured["factory_calls"] += 1
        raise AssertionError("ticker bridge should not be built when scheduler disabled")

    async def _fake_run_ticker_loop(*_args, **_kwargs) -> None:
        captured["ticker_calls"] += 1
        raise AssertionError("ticker loop should not run when scheduler disabled")

    import scheduler.runtime_factory as rf
    import scheduler.ticker as tk

    monkeypatch.setattr(rf, "build_cron_execution_bridge", _fake_build_bridge)
    monkeypatch.setattr(tk, "run_ticker_loop", _fake_run_ticker_loop)

    await cli_main._run(
        config_path=None,
        session_id=None,
        list_sessions=False,
        resume_last=False,
        verbose=False,
        smoke=False,
        instructions_files=[],
        trace_enabled=False,
        reasoning_effort=None,
    )

    assert captured["factory_calls"] == 0
    assert captured["ticker_calls"] == 0


async def test_cli_skips_ticker_in_smoke_mode(monkeypatch, tmp_path: Path) -> None:
    """smoke 路径短路 chat REPL，也跳过 ticker（避免 smoke 半秒装配后 ticker 还在跑）。"""
    captured: dict[str, int] = {"factory_calls": 0, "ticker_calls": 0}

    sentinel_store = object()
    registry = _DummyRegistry()
    cfg = _build_cfg(scheduler_enabled=True)
    _common_monkeypatches(monkeypatch, registry, cfg, register_returns=sentinel_store)

    async def _fake_smoke(_runtime, _sid):
        return None

    monkeypatch.setattr(cli_main, "_run_smoke", _fake_smoke)

    def _fake_build_bridge(*_args, **_kwargs):
        captured["factory_calls"] += 1
        raise AssertionError("ticker should not run in smoke mode")

    async def _fake_run_ticker_loop(*_args, **_kwargs) -> None:
        captured["ticker_calls"] += 1

    import scheduler.runtime_factory as rf
    import scheduler.ticker as tk

    monkeypatch.setattr(rf, "build_cron_execution_bridge", _fake_build_bridge)
    monkeypatch.setattr(tk, "run_ticker_loop", _fake_run_ticker_loop)

    await cli_main._run(
        config_path=None,
        session_id=None,
        list_sessions=False,
        resume_last=False,
        verbose=False,
        smoke=True,
        instructions_files=[],
        trace_enabled=False,
        reasoning_effort=None,
    )

    assert captured["factory_calls"] == 0
    assert captured["ticker_calls"] == 0
