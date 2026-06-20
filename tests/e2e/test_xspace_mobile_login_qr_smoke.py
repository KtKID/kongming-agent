"""XSpace Mobile 登录二维码 fake APK smoke。

本脚本用 FastAPI TestClient 模拟 `/login` 页面和 XSpace APK，作用是贯穿公网 HTTPS
和 LAN HTTP 两种 server origin 的 create、claim、confirm、exchange、consume 链路。
关键流程是登录页创建 QR，fake APK 从 copy URL 解析 nonce，claim 后由登录页密码确认，
fake APK exchange 得到 device token 与 WebView handoff URL，最后 consume 设置
``kongming_session`` cookie。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from hosts.web.app import create_app
from infrastructure.config.models import Config
from tests.unit.test_web_app_lifespan import _seed_password
from tests.unit.test_web_routers_threads import CSRF_HEADERS, FakeTM, _make_cfg


def _cfg_with_server_origin(server_origin: str) -> Config:
    """构造带扫码登录 server origin 的测试配置。"""
    cfg = _make_cfg()
    web_cfg = cfg.web.model_copy(update={"server_origin": server_origin})
    return cfg.model_copy(update={"web": web_cfg})


@pytest.mark.parametrize(
    ("server_origin", "origin_mode"),
    [
        ("https://kongming.example.com", "public_https"),
        ("http://192.168.1.20:8765", "lan_ip"),
    ],
)
def test_fake_apk_login_qr_smoke(
    tmp_path: Path,
    server_origin: str,
    origin_mode: str,
) -> None:
    """验证 fake APK 扫码登录主链路。"""
    _seed_password(tmp_path, "pwd")
    app = create_app(_cfg_with_server_origin(server_origin), FakeTM(), home_dir=tmp_path)
    with TestClient(app) as client:
        create_response = client.post(
            "/api/xspace/mobile/login-qr-sessions",
            json={
                "protocol_version": "1",
                "client": "kongming-login",
                "requested_scopes": ["webview", "thread.read", "approval.resolve"],
            },
            headers=CSRF_HEADERS,
        )
        assert create_response.status_code == 200, create_response.text
        created = create_response.json()
        assert created["server"] == server_origin
        assert created["server_origin"]["mode"] == origin_mode

        query = parse_qs(urlparse(created["copy_url"]).query)
        nonce = query["nonce"][0]
        assert query["purpose"] == ["login"]
        assert query["server"] == [server_origin]
        assert query["origin_mode"] == [origin_mode]

        claim_response = client.post(
            f"/api/xspace/mobile/login-qr-sessions/{created['login_qr_id']}/claim",
            json={
                "protocol_version": "1",
                "nonce": nonce,
                "device": {
                    "device_id": f"android-{origin_mode}",
                    "label": "Pixel 9",
                    "platform": "android",
                    "app_version": "0.1.0",
                },
                "capabilities": {
                    "webview": True,
                    "camera_scan": True,
                    "secure_storage": True,
                },
            },
        )
        assert claim_response.status_code == 200, claim_response.text
        claim = claim_response.json()
        assert claim["status"] == "pending_confirm"

        status_response = client.get(
            f"/api/xspace/mobile/login-qr-sessions/{created['login_qr_id']}",
            headers={"X-Kongming-Login-Qr-Token": created["browser_token"]},
        )
        assert status_response.status_code == 200, status_response.text
        assert status_response.json()["claim"]["device_id"] == f"android-{origin_mode}"

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
        assert confirm_response.json()["status"] == "confirmed"

        exchange_response = client.post(
            f"/api/xspace/mobile/login-qr-sessions/{created['login_qr_id']}/exchange",
            json={
                "protocol_version": "1",
                "claim_id": claim["claim_id"],
                "nonce": nonce,
                "device_id": f"android-{origin_mode}",
            },
        )
        assert exchange_response.status_code == 200, exchange_response.text
        exchanged = exchange_response.json()
        assert exchanged["server"] == server_origin
        assert exchanged["server_origin"]["mode"] == origin_mode
        assert exchanged["device_token"].startswith("kgm_dt_")
        assert exchanged["web_session_url"].startswith(
            f"{server_origin}/-/xspace/mobile/session/consume?handoff_token=kgm_ht_"
        )

        local_consume_url = exchanged["web_session_url"].replace(
            server_origin,
            "http://testserver",
        )
        consume_response = client.get(local_consume_url, follow_redirects=False)
        assert consume_response.status_code == 302
        assert consume_response.headers["location"] == "/"
        assert "kongming_session=" in consume_response.headers["set-cookie"]
