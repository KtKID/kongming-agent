"""unit：``web/app.py`` lifespan 起 cron ticker（v0.2 #19）。

覆盖：

- ``cfg.scheduler.enabled=True`` → lifespan startup 起 ticker（``run_ticker_loop``
  被调一次）；shutdown 时 set stop_event + await。
- ``cfg.scheduler.enabled=False`` → 不起 ticker。
- ticker 用的 cron home 走 ``home_dir / cron``（测试隔离），不污染全局
  ``get_kongming_home()``。
- ticker shutdown 后 ``ticker_runtime.aclose()`` 被调一次。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from infrastructure.config.models import Config
from web.app import create_app


def _make_cfg(*, scheduler_enabled: bool) -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {
                "enabled": True,
                "dev_mode": True,
                "idle_timeout_seconds": 1800,
                "idle_check_interval_seconds": 60,
                "pending_approval_timeout_seconds": 60,
            },
            "scheduler": {
                "enabled": scheduler_enabled,
                "interval": 0.1,
                "max_inflight": 2,
            },
        }
    )


class FakeThreadManager:
    """同 test_web_app_lifespan.py 的 FakeThreadManager 最小子集。"""

    def __init__(self) -> None:
        self.start_called = 0
        self.aclose_called = 0
        self._started = False
        self._closed = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        self.start_called += 1
        self._started = True

    async def aclose_all(self) -> None:
        self.aclose_called += 1
        self._closed = True

    async def create_thread(self, name: str, preset_id: str) -> Any:
        raise NotImplementedError

    async def rename_thread(self, thread_id: str, new_name: str) -> Any:
        raise NotImplementedError

    async def delete_thread(self, thread_id: str, *, keep_history: bool = False) -> None:
        return None

    async def boot_or_attach(self, thread_id: str) -> Any:
        raise NotImplementedError

    async def evict_cell(
        self,
        thread_id: str,
        reason: str,
        message: str | None = None,
        *,
        notify_ws: bool = True,
    ) -> None:
        return None

    def list_threads(self) -> list[Any]:
        return []

    def list_cells(self) -> list[Any]:
        return []

    def get_cell(self, thread_id: str) -> Any:
        return None

    def find_thread_by_claude_thread_id(self, claude_thread_id: str) -> Any:
        return None

    async def bind_claude_thread(
        self,
        thread_id: str,
        claude_thread_id: str,
        cwd: str,
    ) -> Any:
        raise NotImplementedError

    def resolve_approval(self, thread_id: str, call_id: str, approved: bool) -> None:
        return None


def _seed_password(home: Path, password: str = "test-password") -> None:
    from web.auth.secrets import hash_password

    web_dir = home / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    (web_dir / "password.hash").write_text(hash_password(password), encoding="utf-8")


def test_lifespan_starts_ticker_when_scheduler_enabled(monkeypatch, tmp_path: Path) -> None:
    """scheduler.enabled=True → ticker 起跑 + shutdown 优雅退出。"""
    captured: dict[str, Any] = {
        "ticker_calls": 0,
        "factory_calls": 0,
        "aclose_calls": 0,
        "stop_event_set": False,
    }

    cfg = _make_cfg(scheduler_enabled=True)
    tm = FakeThreadManager()
    _seed_password(tmp_path)

    class _FakeTickerRuntime:
        async def aclose(self) -> None:
            captured["aclose_calls"] += 1

    def _fake_build_bridge(_cfg, _store, *, event_sinks=None, dispatcher=None, **_kwargs):
        captured["factory_calls"] += 1
        captured["bridge_store"] = _store
        # v0.3 cron-delivery M4：lifespan ticker 必须传 dispatcher
        # （P0-1 R2 round 1 修复）；这里记下来给测试断言用
        captured["dispatcher_set"] = dispatcher is not None
        captured["dispatcher_has_web_sink"] = (
            dispatcher is not None and getattr(dispatcher, "_web_sink", None) is not None
        )
        return _FakeTickerRuntime(), object()

    async def _fake_run_ticker_loop(_store, _bridge, stop_event: asyncio.Event, **kwargs):
        captured["ticker_calls"] += 1
        captured["interval"] = kwargs.get("interval")
        captured["max_inflight"] = kwargs.get("max_inflight")
        await stop_event.wait()
        captured["stop_event_set"] = True

    import scheduler.runtime_factory as rf
    import scheduler.ticker as tk

    monkeypatch.setattr(rf, "build_cron_execution_bridge", _fake_build_bridge)
    monkeypatch.setattr(tk, "run_ticker_loop", _fake_run_ticker_loop)

    app = create_app(cfg, tm, home_dir=tmp_path)
    with TestClient(app):
        # 进入 with → startup 已起 ticker；退出 → shutdown 收尾。
        # 进入后给一点点时间让 ticker_task schedule 上去（asyncio 调度）
        pass

    assert captured["factory_calls"] == 1
    assert captured["ticker_calls"] == 1
    assert captured["interval"] == 0.1
    assert captured["max_inflight"] == 2
    assert captured["aclose_calls"] == 1
    assert captured["stop_event_set"] is True
    # cron home 应在 home_dir / "cron"
    assert (tmp_path / "cron").exists()
    # app.state 挂载 scheduler_store
    assert app.state.scheduler_store is not None
    # v0.3 cron-delivery M4 R2 fix（P0-1）：lifespan ticker 必须装 dispatcher
    # 含 web_sink，否则 cron 触发不会 broadcast 到 /ws/cron 客户端
    assert captured["dispatcher_set"] is True, (
        "lifespan ticker 必须传 dispatcher；否则 cron 主路径不投递"
    )
    assert captured["dispatcher_has_web_sink"] is True, (
        "dispatcher.web_sink 必须装好（WebDeliverySink + get_broker()）"
    )


def test_lifespan_does_not_start_ticker_when_scheduler_disabled(
    monkeypatch, tmp_path: Path
) -> None:
    """scheduler.enabled=False → ticker 不起。"""
    captured: dict[str, int] = {"factory_calls": 0, "ticker_calls": 0}

    cfg = _make_cfg(scheduler_enabled=False)
    tm = FakeThreadManager()
    _seed_password(tmp_path)

    def _fake_build_bridge(*_args, **_kwargs):
        captured["factory_calls"] += 1
        raise AssertionError("ticker should not start when scheduler disabled")

    async def _fake_run_ticker_loop(*_args, **_kwargs) -> None:
        captured["ticker_calls"] += 1
        raise AssertionError("ticker should not run when scheduler disabled")

    import scheduler.runtime_factory as rf
    import scheduler.ticker as tk

    monkeypatch.setattr(rf, "build_cron_execution_bridge", _fake_build_bridge)
    monkeypatch.setattr(tk, "run_ticker_loop", _fake_run_ticker_loop)

    app = create_app(cfg, tm, home_dir=tmp_path)
    with TestClient(app):
        pass

    assert captured["factory_calls"] == 0
    assert captured["ticker_calls"] == 0
    # 没装时不挂 scheduler_store 属性
    assert getattr(app.state, "scheduler_store", None) is None


def test_lifespan_ticker_startup_failure_does_not_block_app(monkeypatch, tmp_path: Path) -> None:
    """ticker 起跑挂掉时 lifespan 不抛——继续 yield 并正常 shutdown thread_manager。"""
    cfg = _make_cfg(scheduler_enabled=True)
    tm = FakeThreadManager()
    _seed_password(tmp_path)

    def _explode_build_bridge(*_args, **_kwargs):
        raise RuntimeError("boom")

    import scheduler.runtime_factory as rf

    monkeypatch.setattr(rf, "build_cron_execution_bridge", _explode_build_bridge)

    app = create_app(cfg, tm, home_dir=tmp_path)
    # 不应抛
    with TestClient(app):
        pass
    # thread_manager 仍正常收尾
    assert tm.aclose_called == 1


def test_lifespan_prefers_injected_scheduler_runtime_factory(tmp_path: Path) -> None:
    """Injected scheduler runtime factory should drive lifespan ticker wiring."""
    captured: dict[str, Any] = {
        "factory_calls": 0,
        "ticker_calls": 0,
        "aclose_calls": 0,
    }

    cfg = _make_cfg(scheduler_enabled=True)
    tm = FakeThreadManager()
    _seed_password(tmp_path)

    class _FakeTickerRuntime:
        async def aclose(self) -> None:
            captured["aclose_calls"] += 1

    class _FakeBridge:
        def __init__(self) -> None:
            self._dispatcher = None
            self._preset_map = None
            self._trace_dir = None

    def _scheduler_runtime_factory(_store):
        captured["factory_calls"] += 1
        bridge = _FakeBridge()
        captured["bridge"] = bridge
        return _FakeTickerRuntime(), bridge

    async def _fake_run_ticker_loop(_store, _bridge, stop_event: asyncio.Event, **_kwargs) -> None:
        captured["ticker_calls"] += 1
        await stop_event.wait()

    import scheduler.runtime_factory as rf
    import scheduler.ticker as tk

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        rf,
        "build_cron_execution_bridge",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("injected scheduler runtime factory should be used")
        ),
    )
    monkeypatch.setattr(tk, "run_ticker_loop", _fake_run_ticker_loop)
    try:
        app = create_app(
            cfg,
            tm,
            home_dir=tmp_path,
            scheduler_runtime_factory=_scheduler_runtime_factory,
        )
        with TestClient(app):
            pass
    finally:
        monkeypatch.undo()

    bridge = captured["bridge"]
    assert captured["factory_calls"] == 1
    assert captured["ticker_calls"] == 1
    assert captured["aclose_calls"] == 1
    assert bridge._dispatcher is not None
    assert bridge._trace_dir == tmp_path / "cron" / "traces"
