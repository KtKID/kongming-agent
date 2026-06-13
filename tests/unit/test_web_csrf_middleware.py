"""CSRFMiddleware 单测（v0.1.5）。

覆盖：

1. mutating 方法（POST/PATCH/DELETE/PUT）缺 X-Requested-With → 403 + ErrorResponseDTO
2. mutating 方法带正确 header → 通过
3. safe 方法（GET/HEAD/OPTIONS）不需 header
4. 错误 header 值 → 403
5. ``/api/auth/login`` 也要 CSRF 保护
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE, CSRFMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.get("/api/x")
    async def x_get() -> dict:
        return {"method": "GET"}

    @app.post("/api/x")
    async def x_post() -> dict:
        return {"method": "POST"}

    @app.patch("/api/x")
    async def x_patch() -> dict:
        return {"method": "PATCH"}

    @app.delete("/api/x", status_code=204)
    async def x_delete() -> None:
        return None

    @app.post("/api/auth/login")
    async def login() -> dict:
        return {"login": True}

    return app


def test_get_passes_without_header() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.get("/api/x")
        assert resp.status_code == 200
        assert resp.json()["method"] == "GET"


def test_post_without_header_blocked() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/api/x")
        assert resp.status_code == 403
        body = resp.json()
        assert body["error_code"] == "internal"
        assert "CSRF" in body["message"]


def test_post_with_correct_header_passes() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/x",
            headers={CSRF_HEADER_NAME: CSRF_HEADER_VALUE},
        )
        assert resp.status_code == 200


def test_post_with_wrong_header_value_blocked() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/x",
            headers={CSRF_HEADER_NAME: "WrongValue"},
        )
        assert resp.status_code == 403


def test_patch_without_header_blocked() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.patch("/api/x")
        assert resp.status_code == 403


def test_delete_without_header_blocked() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.delete("/api/x")
        assert resp.status_code == 403


def test_login_endpoint_also_requires_csrf() -> None:
    """登录端点也必须带 X-Requested-With。"""
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/api/auth/login")
        assert resp.status_code == 403


def test_login_endpoint_passes_with_csrf_header() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/login",
            headers={CSRF_HEADER_NAME: CSRF_HEADER_VALUE},
        )
        assert resp.status_code == 200
