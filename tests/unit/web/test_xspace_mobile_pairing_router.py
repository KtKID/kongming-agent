"""XSpace Mobile 配对 Router 单元测试。

本脚本验证 ``hosts.web.routers.xspace_mobile`` 的 FastAPI 合同，作用是固定
claim/exchange/session-handoff/consume 的真实 HTTP 主链路，以及 Auth/CSRF
精确例外。关键流程是用真实 ``create_app`` 和 ``TestClient`` 装配 middleware、
router、SQLite repository，再分别用登录 client 和匿名 client 模拟桌面 Web 与
XSpace Android。关键测试职责：主链路覆盖 cookie handoff，边界测试覆盖公开路径
和受保护路径。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hosts.web.app import create_app
from infrastructure.config.models import Config
from tests.unit.test_web_app_lifespan import _seed_password
from tests.unit.test_web_routers_threads import CSRF_HEADERS, FakeTM, _make_cfg


def _authed_client(tmp_path: Path, cfg: Config | None = None) -> TestClient:
    """创建已登录的 Web TestClient。

    关键输入：pytest 临时目录。
    关键输出：带 ``kongming_session`` cookie 的 TestClient。
    """
    _seed_password(tmp_path, "pwd")
    app = create_app(cfg or _make_cfg(), FakeTM(), home_dir=tmp_path)
    client = TestClient(app)
    client.__enter__()
    response = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 200, response.text
    return client


def _anonymous_client(app: FastAPI) -> TestClient:
    """创建共享同一 app.state 的匿名 Android TestClient。

    关键输入：已装配 app。
    关键输出：无 cookie 的 TestClient。
    """
    return TestClient(app)


def _create_pairing(client: TestClient, scopes: list[str] | None = None) -> dict[str, str]:
    """通过登录 Web client 创建 pairing session。

    关键输入：已登录 TestClient 和可选 scopes。
    关键输出：创建响应 JSON。
    """
    response = client.post(
        "/api/xspace/mobile/pairing-sessions",
        json={
            "protocol_version": "1",
            "client": "kongming-web",
            "requested_scopes": scopes or ["webview", "thread.read"],
        },
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _cfg_with_public_origin(public_origin: str) -> Config:
    """构造带移动配对公开 origin 的测试 Config。"""
    cfg = _make_cfg()
    web_cfg = cfg.web.model_copy(update={"public_origin": public_origin})
    return cfg.model_copy(update={"web": web_cfg})


def test_mobile_pairing_router_happy_path_and_handoff_consume(tmp_path: Path) -> None:
    """验证 HTTP 主链路：create、claim、approve、exchange、handoff、consume。"""
    authed = _authed_client(tmp_path)
    anonymous = _anonymous_client(authed.app)
    try:
        created = _create_pairing(authed)
        assert created["pairing_id"].startswith("pr_")
        assert "nonce" not in created
        assert created["copy_url"].startswith("http://testserver/-/xspace/mobile/pair")

        query = parse_qs(urlparse(created["copy_url"]).query)
        nonce = query["nonce"][0]

        claim_response = anonymous.post(
            f"/api/xspace/mobile/pairing-sessions/{created['pairing_id']}/claim",
            json={
                "protocol_version": "1",
                "nonce": nonce,
                "device": {
                    "device_id": "android-pixel-9",
                    "label": "Pixel 9",
                    "platform": "android",
                    "app_version": "0.1.0",
                },
                "capabilities": {"webview": True, "camera_scan": True, "push": False},
            },
        )
        assert claim_response.status_code == 200, claim_response.text
        claim = claim_response.json()
        assert claim["status"] == "pending_approval"

        pending_response = anonymous.post(
            f"/api/xspace/mobile/pairing-sessions/{created['pairing_id']}/exchange",
            json={
                "protocol_version": "1",
                "claim_id": claim["claim_id"],
                "nonce": nonce,
                "device_id": "android-pixel-9",
            },
        )
        assert pending_response.status_code == 202
        assert pending_response.json() == {"status": "pending_approval", "poll_after_ms": 1000}

        status_response = authed.get(
            f"/api/xspace/mobile/pairing-sessions/{created['pairing_id']}",
        )
        assert status_response.status_code == 200
        assert status_response.json()["claim"]["device_id"] == "android-pixel-9"

        approve_response = authed.post(
            f"/api/xspace/mobile/pairing-sessions/{created['pairing_id']}/approve",
            json={"claim_id": claim["claim_id"], "approved": True},
            headers=CSRF_HEADERS,
        )
        assert approve_response.status_code == 200
        assert approve_response.json() == {"status": "approved"}

        exchange_response = anonymous.post(
            f"/api/xspace/mobile/pairing-sessions/{created['pairing_id']}/exchange",
            json={
                "protocol_version": "1",
                "claim_id": claim["claim_id"],
                "nonce": nonce,
                "device_id": "android-pixel-9",
            },
        )
        assert exchange_response.status_code == 200, exchange_response.text
        exchanged = exchange_response.json()
        assert exchanged["device_token"].startswith("kgm_dt_")
        assert exchanged["web_session_url"].startswith(
            "http://testserver/-/xspace/mobile/session/consume?handoff_token=kgm_ht_"
        )
        assert exchanged["scopes"] == ["webview", "thread.read"]

        exchanged_status = authed.get(
            f"/api/xspace/mobile/pairing-sessions/{created['pairing_id']}",
        )
        assert exchanged_status.status_code == 200
        exchanged_view = exchanged_status.json()
        assert exchanged_view["status"] == "exchanged"
        assert exchanged_view["claim"]["status"] == "exchanged"

        handoff_response = anonymous.post(
            "/api/xspace/mobile/session-handoff",
            headers={"Authorization": f"Bearer {exchanged['device_token']}"},
        )
        assert handoff_response.status_code == 200, handoff_response.text
        handoff = handoff_response.json()
        assert handoff["web_session_url"].startswith(
            "http://testserver/-/xspace/mobile/session/consume?handoff_token=kgm_ht_"
        )

        consume_response = anonymous.get(handoff["web_session_url"], follow_redirects=False)
        assert consume_response.status_code == 302
        assert consume_response.headers["location"] == "/"
        assert "kongming_session=" in consume_response.headers["set-cookie"]

        devices_response = authed.get("/api/xspace/mobile/devices")
        assert devices_response.status_code == 200
        devices = devices_response.json()["devices"]
        assert [device["device_id"] for device in devices] == ["android-pixel-9"]

        revoke_response = authed.delete(
            "/api/xspace/mobile/devices/android-pixel-9",
            headers=CSRF_HEADERS,
        )
        assert revoke_response.status_code == 204

        revoked_response = anonymous.post(
            "/api/xspace/mobile/session-handoff",
            headers={"Authorization": f"Bearer {exchanged['device_token']}"},
        )
        assert revoked_response.status_code == 401
        assert revoked_response.json()["error"]["code"] == "device_revoked"
    finally:
        anonymous.close()
        authed.__exit__(None, None, None)


def test_mobile_pairing_auth_and_csrf_boundaries(tmp_path: Path) -> None:
    """验证 public mobile 路径和 Web 保护路径的 Auth/CSRF 行为。"""
    authed = _authed_client(tmp_path)
    anonymous = _anonymous_client(authed.app)
    try:
        capabilities = anonymous.get("/api/xspace/mobile/capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json()["mobile_pairing"] is True

        unauth_create = anonymous.post(
            "/api/xspace/mobile/pairing-sessions",
            json={
                "protocol_version": "1",
                "client": "kongming-web",
                "requested_scopes": ["webview"],
            },
            headers=CSRF_HEADERS,
        )
        assert unauth_create.status_code == 401

        csrf_create = authed.post(
            "/api/xspace/mobile/pairing-sessions",
            json={
                "protocol_version": "1",
                "client": "kongming-web",
                "requested_scopes": ["webview"],
            },
        )
        assert csrf_create.status_code == 403

        created = _create_pairing(authed, scopes=["webview"])
        nonce = parse_qs(urlparse(created["copy_url"]).query)["nonce"][0]
        claim = anonymous.post(
            f"/api/xspace/mobile/pairing-sessions/{created['pairing_id']}/claim",
            json={
                "protocol_version": "1",
                "nonce": nonce,
                "device": {
                    "device_id": "android-auth-test",
                    "label": "Pixel",
                    "platform": "android",
                    "app_version": "0.1.0",
                },
                "capabilities": {"webview": True},
            },
        )
        assert claim.status_code == 200, claim.text

        csrf_approve = authed.post(
            f"/api/xspace/mobile/pairing-sessions/{created['pairing_id']}/approve",
            json={"claim_id": claim.json()["claim_id"], "approved": True},
        )
        assert csrf_approve.status_code == 403

        anonymous_devices = anonymous.get("/api/xspace/mobile/devices")
        assert anonymous_devices.status_code == 401

        anonymous_connect = anonymous.get("/-/xspace/mobile/connect")
        assert anonymous_connect.status_code == 401

        authed_connect = authed.get("/-/xspace/mobile/connect")
        assert authed_connect.status_code == 200
        assert "连接 XSpace Android" in authed_connect.text
        assert "function renderState(data)" in authed_connect.text
        assert 'status === "approved"' in authed_connect.text
        assert 'status === "exchanged"' in authed_connect.text
        assert 'status === "denied"' in authed_connect.text
        assert "setActionsVisible(false)" in authed_connect.text

        pair_page = anonymous.get(created["copy_url"])
        assert pair_page.status_code == 200
        assert "xspace://pair-kongming" in pair_page.text
    finally:
        anonymous.close()
        authed.__exit__(None, None, None)


def test_mobile_pairing_status_view_after_denial(tmp_path: Path) -> None:
    """验证拒绝后状态轮询接口返回终态，页面可隐藏授权按钮。"""
    authed = _authed_client(tmp_path)
    anonymous = _anonymous_client(authed.app)
    try:
        created = _create_pairing(authed, scopes=["webview"])
        nonce = parse_qs(urlparse(created["copy_url"]).query)["nonce"][0]
        claim_response = anonymous.post(
            f"/api/xspace/mobile/pairing-sessions/{created['pairing_id']}/claim",
            json={
                "protocol_version": "1",
                "nonce": nonce,
                "device": {
                    "device_id": "android-deny-test",
                    "label": "Pixel Deny",
                    "platform": "android",
                    "app_version": "0.1.0",
                },
                "capabilities": {"webview": True},
            },
        )
        assert claim_response.status_code == 200, claim_response.text
        claim = claim_response.json()

        deny_response = authed.post(
            f"/api/xspace/mobile/pairing-sessions/{created['pairing_id']}/approve",
            json={"claim_id": claim["claim_id"], "approved": False},
            headers=CSRF_HEADERS,
        )
        assert deny_response.status_code == 200
        assert deny_response.json() == {"status": "denied"}

        denied_status = authed.get(
            f"/api/xspace/mobile/pairing-sessions/{created['pairing_id']}",
        )
        assert denied_status.status_code == 200
        denied_view = denied_status.json()
        assert denied_view["status"] == "denied"
        assert denied_view["claim"]["status"] == "denied"
    finally:
        anonymous.close()
        authed.__exit__(None, None, None)


def test_mobile_pairing_uses_configured_public_origin(tmp_path: Path) -> None:
    """验证局域网 public origin 会写入扫码、exchange 和 handoff URL。"""
    public_origin = "http://192.168.31.23:57567"
    authed = _authed_client(tmp_path, cfg=_cfg_with_public_origin(public_origin))
    anonymous = _anonymous_client(authed.app)
    try:
        created = _create_pairing(authed, scopes=["webview"])
        assert created["copy_url"].startswith(f"{public_origin}/-/xspace/mobile/pair")

        copy_query = parse_qs(urlparse(created["copy_url"]).query)
        qr_query = parse_qs(urlparse(created["qr_payload"]).query)
        nonce = copy_query["nonce"][0]
        assert copy_query["server"] == [public_origin]
        assert qr_query["server"] == [public_origin]

        pair_page = anonymous.get(created["copy_url"])
        assert pair_page.status_code == 200
        assert f"server={public_origin.replace(':', '%3A').replace('/', '%2F')}" in pair_page.text

        claim_response = anonymous.post(
            f"/api/xspace/mobile/pairing-sessions/{created['pairing_id']}/claim",
            json={
                "protocol_version": "1",
                "nonce": nonce,
                "device": {
                    "device_id": "android-lan-test",
                    "label": "Pixel LAN",
                    "platform": "android",
                    "app_version": "0.1.0",
                },
                "capabilities": {"webview": True},
            },
        )
        assert claim_response.status_code == 200, claim_response.text
        claim = claim_response.json()

        approve_response = authed.post(
            f"/api/xspace/mobile/pairing-sessions/{created['pairing_id']}/approve",
            json={"claim_id": claim["claim_id"], "approved": True},
            headers=CSRF_HEADERS,
        )
        assert approve_response.status_code == 200

        exchange_response = anonymous.post(
            f"/api/xspace/mobile/pairing-sessions/{created['pairing_id']}/exchange",
            json={
                "protocol_version": "1",
                "claim_id": claim["claim_id"],
                "nonce": nonce,
                "device_id": "android-lan-test",
            },
        )
        assert exchange_response.status_code == 200, exchange_response.text
        exchanged = exchange_response.json()
        assert exchanged["server"] == public_origin
        assert exchanged["instance"]["url"] == public_origin
        assert exchanged["web_session_url"].startswith(
            f"{public_origin}/-/xspace/mobile/session/consume?handoff_token=kgm_ht_"
        )

        handoff_response = anonymous.post(
            "/api/xspace/mobile/session-handoff",
            headers={"Authorization": f"Bearer {exchanged['device_token']}"},
        )
        assert handoff_response.status_code == 200, handoff_response.text
        assert handoff_response.json()["web_session_url"].startswith(
            f"{public_origin}/-/xspace/mobile/session/consume?handoff_token=kgm_ht_"
        )
    finally:
        anonymous.close()
        authed.__exit__(None, None, None)
