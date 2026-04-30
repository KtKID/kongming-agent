"""static.py 单测（v0.1.5）。

覆盖：

1. dist 缺失 + dev_mode=True → 占位 JSON
2. dist 缺失 + dev_mode=False → install_static 抛 RuntimeError
3. dist 存在 → /assets/* 返回静态文件 + SPA fallback 返 index.html
4. /api/ 路径不被 catch-all 抢
5. /ws/ 路径不被 catch-all 抢
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config_loader.models import Config
from web.static import DEFAULT_DIST_DIR, install_static


def _cfg(*, dev_mode: bool) -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {"enabled": True, "dev_mode": dev_mode},
        }
    )


def _build_app_with_dist() -> FastAPI:
    """构建一个最小 app（不预设 dist；调用方按需 chdir 到含 dist 的 tmp_path）。"""
    app = FastAPI()

    @app.get("/api/x")
    async def x_get() -> dict:
        return {"api": True}

    return app


def test_no_dist_dev_mode_returns_placeholder(tmp_path: Path, monkeypatch) -> None:
    """dev_mode=True + dist 缺失 → 占位 JSON。"""
    monkeypatch.chdir(tmp_path)  # web/dist 不存在
    cfg = _cfg(dev_mode=True)
    app = _build_app_with_dist()
    install_static(app, cfg)

    with TestClient(app) as client:
        # /api/x 仍走原 handler
        resp = client.get("/api/x")
        assert resp.status_code == 200
        assert resp.json() == {"api": True}

        # /random-path 走占位
        resp = client.get("/random-path")
        assert resp.status_code == 200
        body = resp.json()
        assert body["dev_mode"] is True
        assert "frontend not built" in body["message"]


def test_no_dist_prod_mode_raises(tmp_path: Path, monkeypatch) -> None:
    """dev_mode=False + dist 缺失 → RuntimeError。"""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(dev_mode=False)
    app = _build_app_with_dist()

    with pytest.raises(RuntimeError, match="frontend dist not found"):
        install_static(app, cfg)


def test_with_dist_serves_index_and_assets(tmp_path: Path, monkeypatch) -> None:
    """dist 存在 → /assets/* + SPA fallback 都通。"""
    monkeypatch.chdir(tmp_path)
    dist = tmp_path / DEFAULT_DIST_DIR
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>", encoding="utf-8")
    (dist / "assets" / "main.js").write_text("console.log('x');", encoding="utf-8")

    cfg = _cfg(dev_mode=False)
    app = _build_app_with_dist()
    install_static(app, cfg)

    with TestClient(app) as client:
        # SPA fallback
        resp = client.get("/some-page")
        assert resp.status_code == 200
        assert "<html>app</html>" in resp.text

        # 静态资源
        resp = client.get("/assets/main.js")
        assert resp.status_code == 200
        assert "console.log" in resp.text

        # /api/x 还是走 handler
        resp = client.get("/api/x")
        assert resp.status_code == 200


def test_api_path_not_swallowed_by_fallback(tmp_path: Path, monkeypatch) -> None:
    """SPA fallback 不抢 /api/ 路径（即使 path 不存在也是 404 而非 200 index.html）。"""
    monkeypatch.chdir(tmp_path)
    dist = tmp_path / DEFAULT_DIST_DIR
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("INDEX", encoding="utf-8")

    cfg = _cfg(dev_mode=False)
    app = FastAPI()
    install_static(app, cfg)

    with TestClient(app) as client:
        resp = client.get("/api/nonexistent")
        # 不是 200 INDEX；而是 404
        assert resp.status_code == 404
        assert resp.text != "INDEX"


def test_ws_path_not_swallowed_by_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    dist = tmp_path / DEFAULT_DIST_DIR
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("INDEX", encoding="utf-8")

    cfg = _cfg(dev_mode=False)
    app = FastAPI()
    install_static(app, cfg)

    with TestClient(app) as client:
        resp = client.get("/ws/nonexistent")
        assert resp.status_code == 404


def test_assets_path_not_in_dev_placeholder(tmp_path: Path, monkeypatch) -> None:
    """dev_mode + dist 缺失：/api 与 /ws 不被占位抢。"""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(dev_mode=True)
    app = FastAPI()
    install_static(app, cfg)

    with TestClient(app) as client:
        resp = client.get("/api/foo")
        assert resp.status_code == 404
        resp = client.get("/ws/bar")
        assert resp.status_code == 404
