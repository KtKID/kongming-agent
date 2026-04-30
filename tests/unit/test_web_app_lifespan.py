"""create_app + lifespan 单测（v0.1.5）。

覆盖：

1. startup 调 thread_manager.start()
2. shutdown 调 thread_manager.aclose_all() 含 5s 超时
3. aclose_all 超时不阻断 shutdown
4. WebAuthNotConfiguredError：缺 password 时抛
5. app.state 正确挂载
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from config_loader.models import Config
from web.app import create_app


def _make_cfg(*, dev_mode: bool = True) -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {
                "enabled": True,
                "dev_mode": dev_mode,
                "idle_timeout_seconds": 1800,
                "idle_check_interval_seconds": 60,
                "pending_approval_timeout_seconds": 60,
            },
        }
    )


class FakeThreadManager:
    """最小 ThreadManagerProtocol 实现（用于装配测试）。"""

    def __init__(self) -> None:
        self.start_called = 0
        self.aclose_called = 0
        self.aclose_delay = 0.0
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
        if self.aclose_delay > 0:
            await asyncio.sleep(self.aclose_delay)
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

    def resolve_approval(self, thread_id: str, call_id: str, approved: bool) -> None:
        return None


def _seed_password(home: Path, password: str = "test-password") -> None:
    """提前在 home/web/password.hash 落 hash，避免装配时抛 WebAuthNotConfiguredError。"""
    from web.auth_secrets import hash_password

    web_dir = home / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    h = hash_password(password)
    (web_dir / "password.hash").write_text(h, encoding="utf-8")


def test_lifespan_startup_calls_start(tmp_path: Path) -> None:
    """启动 app 时 thread_manager.start() 被调。"""
    cfg = _make_cfg()
    tm = FakeThreadManager()
    _seed_password(tmp_path)

    app = create_app(cfg, tm, home_dir=tmp_path)

    with TestClient(app) as _client:
        # 进入 with 触发 startup
        assert tm.start_called == 1
        assert tm.started is True


def test_lifespan_shutdown_calls_aclose_all(tmp_path: Path) -> None:
    cfg = _make_cfg()
    tm = FakeThreadManager()
    _seed_password(tmp_path)

    app = create_app(cfg, tm, home_dir=tmp_path)

    with TestClient(app):
        pass
    # 退出 with → shutdown
    assert tm.aclose_called == 1


def test_lifespan_shutdown_timeout_warns_but_does_not_raise(tmp_path: Path) -> None:
    """aclose_all 超时不让 app 退出失败。"""
    cfg = _make_cfg()
    tm = FakeThreadManager()
    tm.aclose_delay = 1.0  # > 0.2s 超时
    _seed_password(tmp_path)

    app = create_app(cfg, tm, home_dir=tmp_path, lifespan_shutdown_timeout=0.2)

    # 不应抛
    with TestClient(app):
        pass
    assert tm.aclose_called == 1


def test_app_state_attached(tmp_path: Path) -> None:
    cfg = _make_cfg()
    tm = FakeThreadManager()
    _seed_password(tmp_path)

    app = create_app(cfg, tm, home_dir=tmp_path)

    assert app.state.config is cfg
    assert app.state.thread_manager is tm
    assert app.state.serializer is not None
    assert app.state.password_hash is not None
    assert app.state.rate_limiter is not None
    assert app.state.kongming_home == tmp_path


def test_password_not_configured_raises(tmp_path: Path) -> None:
    """缺 password.hash + env → WebAuthNotConfiguredError。"""
    cfg = _make_cfg()
    tm = FakeThreadManager()
    # 不调 _seed_password

    from web.errors import WebAuthNotConfiguredError

    with pytest.raises(WebAuthNotConfiguredError):
        create_app(cfg, tm, home_dir=tmp_path)
