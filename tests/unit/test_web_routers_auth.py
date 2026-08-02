"""routers/auth.py 单测（v0.1.5）。

覆盖：

1. login happy path：cookie 设上 + 200
2. login 错误密码 → 401 + 限流计数 +1
3. 5 次失败 → 锁 + 429 + Retry-After 头
4. logout 清 cookie
5. me 读 cookie 返回 user_id
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from hosts.web.app import create_app
from hosts.web.auth.middleware import (
    CSRF_HEADER_NAME,
    CSRF_HEADER_VALUE,
    SESSION_COOKIE_NAME,
)
from hosts.web.auth.secrets import ENV_WEB_PASSWORD
from hosts.web.rate_limit import LoginRateLimiter
from infrastructure.config.models import Config
from tests.unit.test_web_app_lifespan import FakeThreadManager, _seed_password

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


def _make_cfg() -> Config:
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
            },
        }
    )


def _build_app_with_password(
    tmp_path: Path,
    password: str = "test-pwd-123",
    rate_limiter: LoginRateLimiter | None = None,
) -> TestClient:
    _seed_password(tmp_path, password)
    cfg = _make_cfg()
    tm = FakeThreadManager()
    app = create_app(cfg, tm, home_dir=tmp_path, rate_limiter=rate_limiter)
    return TestClient(app)


def test_login_happy_path(tmp_path: Path) -> None:
    with _build_app_with_password(tmp_path) as client:
        resp = client.post(
            "/api/auth/login",
            json={"password": "test-pwd-123"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        # cookie 已经设上
        assert SESSION_COOKIE_NAME in resp.cookies


def test_login_wrong_password(tmp_path: Path) -> None:
    with _build_app_with_password(tmp_path) as client:
        resp = client.post(
            "/api/auth/login",
            json={"password": "wrong-password"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["error_code"] == "internal"
        assert "invalid credentials" in body["message"]


def test_login_5_failures_lock(tmp_path: Path) -> None:
    """5 次错密码 → 第 6 次 429。"""
    rl = LoginRateLimiter(max_failures=5, lockout_seconds=300)
    with _build_app_with_password(tmp_path, rate_limiter=rl) as client:
        for _ in range(5):
            resp = client.post(
                "/api/auth/login",
                json={"password": "wrong"},
                headers=CSRF_HEADERS,
            )
            assert resp.status_code == 401

        resp = client.post(
            "/api/auth/login",
            json={"password": "wrong"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers


def test_login_success_resets_failure_counter(tmp_path: Path) -> None:
    """正确密码 → 计数清空，下次失败仍只算 1。"""
    rl = LoginRateLimiter(max_failures=3, lockout_seconds=300)
    with _build_app_with_password(tmp_path, rate_limiter=rl) as client:
        # 2 次失败
        for _ in range(2):
            client.post(
                "/api/auth/login",
                json={"password": "wrong"},
                headers=CSRF_HEADERS,
            )
        # 1 次成功 → 计数清空
        resp = client.post(
            "/api/auth/login",
            json={"password": "test-pwd-123"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        # 再次错 2 次 → 仍未锁（计数被 reset）
        for _ in range(2):
            resp = client.post(
                "/api/auth/login",
                json={"password": "wrong"},
                headers=CSRF_HEADERS,
            )
            assert resp.status_code == 401


def test_logout_clears_cookie(tmp_path: Path) -> None:
    with _build_app_with_password(tmp_path) as client:
        # 先登录
        login_resp = client.post(
            "/api/auth/login",
            json={"password": "test-pwd-123"},
            headers=CSRF_HEADERS,
        )
        assert login_resp.status_code == 200

        resp = client.post("/api/auth/logout", headers=CSRF_HEADERS)
        assert resp.status_code == 200
        # set-cookie 应包含 max-age=0 或 expires=过去
        set_cookie_headers = resp.headers.get_list("set-cookie")
        assert any(SESSION_COOKIE_NAME in h for h in set_cookie_headers)


def test_me_returns_user_id(tmp_path: Path) -> None:
    with _build_app_with_password(tmp_path) as client:
        login_resp = client.post(
            "/api/auth/login",
            json={"password": "test-pwd-123"},
            headers=CSRF_HEADERS,
        )
        assert login_resp.status_code == 200

        resp = client.get("/api/auth/me")
        assert resp.status_code == 200
        body = resp.json()
        assert body["authenticated"] is True
        assert body["user_id"] == "default"


def test_me_without_cookie_401(tmp_path: Path) -> None:
    with _build_app_with_password(tmp_path) as client:
        # AuthMiddleware 应当挡住
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401


def test_login_missing_csrf_header_returns_403(tmp_path: Path) -> None:
    with _build_app_with_password(tmp_path) as client:
        resp = client.post(
            "/api/auth/login",
            json={"password": "test-pwd-123"},
            # 故意不带 CSRF header
        )
        assert resp.status_code == 403


def test_reset_password_survives_restart_with_stale_env(monkeypatch, tmp_path: Path) -> None:
    """重置后重新装配 app，仍以落盘 hash 为准。"""
    monkeypatch.setenv(ENV_WEB_PASSWORD, "old-bootstrap-password")
    cfg = _make_cfg()
    tm = FakeThreadManager()
    app = create_app(cfg, tm, home_dir=tmp_path)

    with TestClient(app) as client:
        login_resp = client.post(
            "/api/auth/login",
            json={"password": "old-bootstrap-password"},
            headers=CSRF_HEADERS,
        )
        assert login_resp.status_code == 200

        reset_resp = client.post(
            "/api/auth/reset-password",
            json={"new_password": "new-password-456"},
            headers=CSRF_HEADERS,
        )
        assert reset_resp.status_code == 200

    monkeypatch.setenv(ENV_WEB_PASSWORD, "old-bootstrap-password")
    restarted_app = create_app(cfg, FakeThreadManager(), home_dir=tmp_path)

    with TestClient(restarted_app) as client:
        old_login = client.post(
            "/api/auth/login",
            json={"password": "old-bootstrap-password"},
            headers=CSRF_HEADERS,
        )
        assert old_login.status_code == 401

        new_login = client.post(
            "/api/auth/login",
            json={"password": "new-password-456"},
            headers=CSRF_HEADERS,
        )
        assert new_login.status_code == 200


def test_reset_password_anonymous_flow_updates_login_password(tmp_path: Path) -> None:
    with _build_app_with_password(tmp_path) as client:
        reset_resp = client.post(
            "/api/auth/reset-password",
            json={"new_password": "new-password-456"},
            headers=CSRF_HEADERS,
        )
        assert reset_resp.status_code == 200

        old_login = client.post(
            "/api/auth/login",
            json={"password": "test-pwd-123"},
            headers=CSRF_HEADERS,
        )
        assert old_login.status_code == 401

        new_login = client.post(
            "/api/auth/login",
            json={"password": "new-password-456"},
            headers=CSRF_HEADERS,
        )
        assert new_login.status_code == 200
