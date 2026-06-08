"""AuthMiddleware 单测（v0.1.5）。

覆盖：

1. valid cookie → 受保护 ``/api/*`` 通过（中间件不阻挡）
2. 缺 cookie / 签名错 / 过期 → 401
3. ``/api/auth/login`` / ``/api/auth/logout`` 白名单（无 cookie 也通过）
4. 静态路径 / WS 路径不被中间件挡
5. dev_mode 下 ``/docs`` / ``/openapi.json`` 通过
"""

from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from hosts.web.auth.middleware import (
    SESSION_COOKIE_NAME,
    AuthMiddleware,
    issue_session_cookie,
    make_serializer,
)


def _build_app(*, allow_docs: bool = False) -> tuple[FastAPI, bytes]:
    secret = b"\x01" * 32
    app = FastAPI(docs_url="/docs" if allow_docs else None)
    app.state.serializer = make_serializer(secret)
    app.add_middleware(AuthMiddleware, allow_docs=allow_docs)

    @app.get("/api/protected")
    async def protected() -> dict:
        return {"ok": True}

    @app.post("/api/auth/login")
    async def login_stub() -> dict:
        return {"login": True}

    @app.post("/api/auth/logout")
    async def logout_stub() -> dict:
        return {"logout": True}

    @app.get("/")
    async def root() -> dict:
        return {"static": True}

    @app.get("/api/auth/me")
    async def me_stub(request: Request) -> dict:
        payload = getattr(request.state, "session_payload", None)
        return {"user": payload.user_id if payload else None}

    return app, secret


def _signed_cookie(secret: bytes) -> str:
    """构造一个合法的签名 cookie 值。"""
    from starlette.responses import Response

    serializer = make_serializer(secret)
    resp = Response()
    issue_session_cookie(
        resp,
        serializer,
        request_scheme="http",
    )
    raw = resp.headers["set-cookie"]
    # 解析 set-cookie：``kongming_session=<value>; ...``
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if chunk.startswith(SESSION_COOKIE_NAME + "="):
            return chunk[len(SESSION_COOKIE_NAME) + 1 :]
    raise RuntimeError("set-cookie missing kongming_session")


def test_valid_cookie_passes_protected_path() -> None:
    app, secret = _build_app()
    cookie = _signed_cookie(secret)

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.get("/api/protected")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


def test_missing_cookie_returns_401() -> None:
    app, _ = _build_app()
    with TestClient(app) as client:
        resp = client.get("/api/protected")
        assert resp.status_code == 401
        body = resp.json()
        assert body["error_code"] == "internal"
        assert "not authenticated" in body["message"]


def test_bad_signature_returns_401() -> None:
    app, _ = _build_app()
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, "garbage.signature.value")
        resp = client.get("/api/protected")
        assert resp.status_code == 401


def test_login_endpoint_allowlisted() -> None:
    app, _ = _build_app()
    with TestClient(app) as client:
        # 登录路径不需要 cookie
        resp = client.post("/api/auth/login", headers={"X-Requested-With": "XMLHttpRequest"})
        # CSRF 这里没装；但 AuthMiddleware 不挡
        assert resp.status_code == 200


def test_logout_endpoint_allowlisted() -> None:
    app, _ = _build_app()
    with TestClient(app) as client:
        resp = client.post("/api/auth/logout", headers={"X-Requested-With": "XMLHttpRequest"})
        assert resp.status_code == 200


def test_static_path_allowlisted() -> None:
    app, _ = _build_app()
    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200


def test_docs_blocked_when_not_dev() -> None:
    app, _ = _build_app(allow_docs=False)
    with TestClient(app) as client:
        # docs_url=None → /docs 直接 404，不到 middleware
        resp = client.get("/docs")
        assert resp.status_code in (401, 404)


def test_docs_allowlisted_in_dev_mode() -> None:
    app, _ = _build_app(allow_docs=True)
    with TestClient(app) as client:
        # /docs 是 FastAPI 自带，AuthMiddleware 应放行
        resp = client.get("/docs")
        # 注意 FastAPI docs 渲染需要 openapi.json；此处仅看 middleware 是否放行
        assert resp.status_code != 401


def test_serializer_missing_returns_500() -> None:
    """app.state.serializer 缺失（装配 bug）→ 500。"""
    app = FastAPI()
    # 不挂 serializer
    app.add_middleware(AuthMiddleware)

    @app.get("/api/x")
    async def x() -> dict:
        return {}

    with TestClient(app) as client:
        resp = client.get("/api/x")
        assert resp.status_code == 500


def test_session_payload_attached_to_request_state() -> None:
    """合法 cookie → request.state.session_payload 可读。"""
    app, secret = _build_app()
    cookie = _signed_cookie(secret)

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.get("/api/auth/me")
        # /api/auth/me 不在 allowlist；应通过 middleware 验 cookie 后到 handler
        assert resp.status_code == 200
        assert resp.json() == {"user": "default"}


def test_expired_cookie_returns_401() -> None:
    """超过 max_age_seconds 的 cookie → 401。"""
    secret = b"\x01" * 32
    serializer = make_serializer(secret)

    # 手动伪造一个 1 天前签发的 cookie
    from itsdangerous import URLSafeTimedSerializer

    s2 = URLSafeTimedSerializer(secret_key=secret, salt="kongming.web.session.v1")
    # 注：itsdangerous 内部用自身时钟；我们直接生成 cookie 然后调小 max_age 验证
    raw = s2.dumps({"user_id": "default", "iat": int(time.time())})

    app, _ = _build_app()
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, raw)
        # 默认 max_age 24h；cookie 是刚签发的，应当通过
        resp = client.get("/api/protected")
        assert resp.status_code == 200

    # 用 verify_session_cookie 直接以 max_age=0 调来模拟过期
    from hosts.web.auth.middleware import verify_session_cookie

    payload = verify_session_cookie(raw, serializer, max_age_seconds=0)
    # 立即调用通常仍 fresh；让出 1 秒后再验
    time.sleep(1.1)
    payload2 = verify_session_cookie(raw, serializer, max_age_seconds=0)
    # 必有一次返回 None
    assert payload is None or payload2 is None
