"""health 路由单元测试（manage-config-tab #7）。

覆盖：

1. ``GET /api/health`` 返回 ``200 + {"status": "ok"}``
2. **未登录**（无 cookie）也能拿到 200，不返回 401/403——前端在重启
   探测时 cookie 可能失效 / lifespan 未完成。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from config_loader.models import Config
from tests.unit.test_web_app_lifespan import _seed_password
from web.app import create_app


class _FakeTM:
    """只满足 create_app 不报错的最小 ThreadManager 桩。"""

    _started = False
    _closed = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        self._started = True

    async def aclose_all(self) -> None:
        self._closed = True

    def list_threads(self) -> list:
        return []

    def list_cells(self) -> list:
        return []

    def get_cell(self, thread_id: str) -> None:
        return None


def _make_cfg() -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {"enabled": True, "dev_mode": True},
        }
    )


def _make_client(tmp_path: Path) -> TestClient:
    """构造已装配的 TestClient——不登录、不带 cookie。"""
    _seed_password(tmp_path, "pwd")
    app = create_app(_make_cfg(), _FakeTM(), home_dir=tmp_path)  # type: ignore[arg-type]
    app.state.workspace_root = tmp_path
    client = TestClient(app)
    client.__enter__()
    return client


def test_health_returns_ok(tmp_path: Path) -> None:
    """正常调用应返回 200 + {"status": "ok"}。"""
    client = _make_client(tmp_path)
    try:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
    finally:
        client.__exit__(None, None, None)


def test_health_works_without_auth(tmp_path: Path) -> None:
    """未登录访问也应 200——白名单生效，不被 AuthMiddleware 挡成 401/403。"""
    client = _make_client(tmp_path)
    try:
        # 显式不带任何 cookie / 不调 /api/auth/login
        resp = client.get("/api/health")
        assert resp.status_code == 200, f"health 应公开，实际拿到 {resp.status_code}：{resp.text}"
        assert resp.status_code not in (401, 403)
        assert resp.json() == {"status": "ok"}
    finally:
        client.__exit__(None, None, None)
