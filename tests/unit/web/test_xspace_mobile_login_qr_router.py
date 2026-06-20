"""XSpace Mobile 登录二维码 Router 单元测试。

本脚本验证 ``hosts.web.routers.login_qr`` 的 FastAPI 合同，作用是固定 create/status/
claim/confirm/exchange/fallback 的真实 HTTP 主链路，以及 Auth/CSRF 精确例外。关键流程
是用真实 ``create_app`` 和 ``TestClient`` 装配 middleware、router、SQLite repository，
再用匿名登录页和匿名 XSpace Android fake client 贯穿扫码登录。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from hosts.web.app import create_app
from infrastructure.config.models import Config
from tests.unit.test_web_app_lifespan import _seed_password
from tests.unit.test_web_routers_threads import CSRF_HEADERS, FakeTM, _make_cfg

LOGIN_QR_TOKEN_HEADER = "X-Kongming-Login-Qr-Token"


def _cfg_with_server_origin(server_origin: str | None) -> Config:
    """构造带扫码登录 server origin 的测试 Config。"""
    cfg = _make_cfg()
    web_cfg = cfg.web.model_copy(update={"server_origin": server_origin})
    return cfg.model_copy(update={"web": web_cfg})


def _client(tmp_path: Path, *, server_origin: str | None = "https://kongming.example.com"):
    """创建测试 TestClient。"""
    _seed_password(tmp_path, "pwd")
    app = create_app(_cfg_with_server_origin(server_origin), FakeTM(), home_dir=tmp_path)
    client = TestClient(app)
    client.__enter__()
    return client


def _create_login_qr(client: TestClient) -> dict[str, object]:
    """通过登录页创建扫码登录 session。"""
    response = client.post(
        "/api/xspace/mobile/login-qr-sessions",
        json={
            "protocol_version": "1",
            "client": "kongming-login",
            "requested_scopes": ["webview", "thread.read"],
        },
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _claim_login_qr(client: TestClient, created: dict[str, object]) -> dict[str, object]:
    """通过 fake APK claim 扫码登录 session。"""
    nonce = parse_qs(urlparse(str(created["copy_url"])).query)["nonce"][0]
    response = client.post(
        f"/api/xspace/mobile/login-qr-sessions/{created['login_qr_id']}/claim",
        json={
            "protocol_version": "1",
            "nonce": nonce,
            "device": {
                "device_id": "android-pixel-9",
                "label": "Pixel 9",
                "platform": "android",
                "app_version": "0.1.0",
            },
            "capabilities": {"webview": True, "camera_scan": True},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_login_qr_router_happy_path_and_handoff_consume(tmp_path: Path) -> None:
    """验证 HTTP 主链路：create、claim、confirm、exchange、consume。"""
    client = _client(tmp_path)
    try:
        created = _create_login_qr(client)
        assert str(created["login_qr_id"]).startswith("lq_")
        assert str(created["browser_token"]).startswith("kgm_lqt_")
        assert "nonce" not in created
        assert created["server"] == "https://kongming.example.com"
        assert created["server_origin"]["mode"] == "public_https"  # type: ignore[index]
        assert str(created["qr_payload"]).startswith("xspace://login-kongming?")
        assert str(created["copy_url"]).startswith(
            "https://kongming.example.com/-/xspace/mobile/login?"
        )

        claim = _claim_login_qr(client, created)
        assert claim["status"] == "pending_confirm"

        status_response = client.get(
            f"/api/xspace/mobile/login-qr-sessions/{created['login_qr_id']}",
            headers={LOGIN_QR_TOKEN_HEADER: str(created["browser_token"])},
        )
        assert status_response.status_code == 200, status_response.text
        status = status_response.json()
        assert status["status"] == "pending_confirm"
        assert status["claim"]["device_id"] == "android-pixel-9"

        nonce = parse_qs(urlparse(str(created["copy_url"])).query)["nonce"][0]
        pending_response = client.post(
            f"/api/xspace/mobile/login-qr-sessions/{created['login_qr_id']}/exchange",
            json={
                "protocol_version": "1",
                "claim_id": claim["claim_id"],
                "nonce": nonce,
                "device_id": "android-pixel-9",
            },
        )
        assert pending_response.status_code == 202
        assert pending_response.json() == {"status": "pending_approval", "poll_after_ms": 1000}

        confirm_response = client.post(
            f"/api/xspace/mobile/login-qr-sessions/{created['login_qr_id']}/confirm",
            json={
                "browser_token": created["browser_token"],
                "claim_id": claim["claim_id"],
                "password": "pwd",
            },
            headers=CSRF_HEADERS,
        )
        assert confirm_response.status_code == 200, confirm_response.text
        assert confirm_response.json() == {"status": "confirmed", "poll_after_ms": 1000}

        exchange_response = client.post(
            f"/api/xspace/mobile/login-qr-sessions/{created['login_qr_id']}/exchange",
            json={
                "protocol_version": "1",
                "claim_id": claim["claim_id"],
                "nonce": nonce,
                "device_id": "android-pixel-9",
            },
        )
        assert exchange_response.status_code == 200, exchange_response.text
        exchanged = exchange_response.json()
        assert exchanged["server"] == "https://kongming.example.com"
        assert exchanged["server_origin"]["origin"] == "https://kongming.example.com"
        assert exchanged["device_token"].startswith("kgm_dt_")
        assert exchanged["web_session_url"].startswith(
            "https://kongming.example.com/-/xspace/mobile/session/consume?handoff_token=kgm_ht_"
        )

        local_consume_url = exchanged["web_session_url"].replace(
            "https://kongming.example.com",
            "http://testserver",
        )
        consume_response = client.get(local_consume_url, follow_redirects=False)
        assert consume_response.status_code == 302
        assert consume_response.headers["location"] == "/"
        assert "kongming_session=" in consume_response.headers["set-cookie"]

        fallback = client.get(str(created["copy_url"]))
        assert fallback.status_code == 200
        assert "xspace://login-kongming" in fallback.text
    finally:
        client.__exit__(None, None, None)


def test_login_qr_auth_and_csrf_boundaries(tmp_path: Path) -> None:
    """验证登录二维码 public 路径和浏览器 CSRF 行为。"""
    client = _client(tmp_path, server_origin="http://192.168.31.23:8765")
    try:
        csrf_create = client.post(
            "/api/xspace/mobile/login-qr-sessions",
            json={"protocol_version": "1"},
        )
        assert csrf_create.status_code == 403

        created = _create_login_qr(client)
        assert created["server_origin"]["mode"] == "lan_ip"  # type: ignore[index]
        assert "origin_mode=lan_ip" in str(created["qr_payload"])

        wrong_status = client.get(
            f"/api/xspace/mobile/login-qr-sessions/{created['login_qr_id']}",
            headers={LOGIN_QR_TOKEN_HEADER: "wrong-token"},
        )
        assert wrong_status.status_code == 403
        assert wrong_status.json()["error"]["code"] == "browser_token_mismatch"

        claim = _claim_login_qr(client, created)

        csrf_confirm = client.post(
            f"/api/xspace/mobile/login-qr-sessions/{created['login_qr_id']}/confirm",
            json={
                "browser_token": created["browser_token"],
                "claim_id": claim["claim_id"],
                "password": "pwd",
            },
        )
        assert csrf_confirm.status_code == 403

        wrong_password = client.post(
            f"/api/xspace/mobile/login-qr-sessions/{created['login_qr_id']}/confirm",
            json={
                "browser_token": created["browser_token"],
                "claim_id": claim["claim_id"],
                "password": "wrong",
            },
            headers=CSRF_HEADERS,
        )
        assert wrong_password.status_code == 401
        assert wrong_password.json()["error"]["code"] == "invalid_credentials"

        nonce = parse_qs(urlparse(str(created["copy_url"])).query)["nonce"][0]
        exchange = client.post(
            f"/api/xspace/mobile/login-qr-sessions/{created['login_qr_id']}/exchange",
            json={
                "protocol_version": "1",
                "claim_id": claim["claim_id"],
                "nonce": nonce,
                "device_id": "android-pixel-9",
            },
        )
        assert exchange.status_code == 202

        protected_devices = client.get("/api/xspace/mobile/devices")
        assert protected_devices.status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_login_qr_create_requires_configured_server_origin(tmp_path: Path) -> None:
    """验证缺少 server origin 时返回稳定配置错误。"""
    client = _client(tmp_path, server_origin=None)
    try:
        response = client.post(
            "/api/xspace/mobile/login-qr-sessions",
            json={"protocol_version": "1"},
            headers=CSRF_HEADERS,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "server_origin_required"
    finally:
        client.__exit__(None, None, None)
