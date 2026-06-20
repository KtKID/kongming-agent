"""XSpace Mobile 登录二维码协议 fixture 测试。

本脚本验证 `tests/fixtures/xspace_mobile_login_qr/login_qr_contract.json`，作用是把
XSpace APK 需要实现的二维码、claim、confirm、exchange 和错误响应字段固定下来。
关键流程是读取 fixture，解析 DTO 和 deeplink/copy URL，断言推荐 scheme 与兼容 scheme
都带 `purpose=login`。
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from hosts.web.xspace_mobile import (
    LoginQrClaimRecord,
    LoginQrSessionRecord,
    MobileDeviceDescriptor,
    errors,
)
from hosts.web.xspace_mobile.server_origin import LoginQrOriginView


def _fixture() -> dict[str, object]:
    """读取登录二维码协议 fixture。"""
    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "xspace_mobile_login_qr"
        / "login_qr_contract.json"
    )
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def test_login_qr_records_fixture_matches_dto_contract() -> None:
    """验证 session/claim fixture 可被 DTO 直接解析。"""
    payload = _fixture()

    session = LoginQrSessionRecord.model_validate(payload["login_qr_session"])
    claim = LoginQrClaimRecord.model_validate(payload["login_qr_claim"])
    device = MobileDeviceDescriptor.model_validate(
        payload["claim_request"]["device"]  # type: ignore[index]
    )

    assert session.login_qr_id == claim.login_qr_id
    assert session.claim_id == claim.claim_id
    assert claim.device_id == device.device_id
    assert not session.nonce_hash.startswith("nonce_")
    assert not session.browser_token_hash.startswith("kgm_lqt_")


def test_login_qr_deeplink_and_copy_url_contract() -> None:
    """验证推荐 deeplink、兼容 deeplink 和 copy URL 字段。"""
    payload = _fixture()

    for key, scheme in [
        ("recommended_qr_payload", "login-kongming"),
        ("compatible_qr_payload", "pair-kongming"),
    ]:
        parsed = urlparse(str(payload[key]))
        query = parse_qs(parsed.query)
        assert parsed.scheme == "xspace"
        assert parsed.netloc == scheme
        assert query["purpose"] == ["login"]
        assert query["server"] == ["https://kongming.example.com"]
        assert query["origin_mode"] == ["public_https"]
        assert query["login_qr_id"] == ["lq_example"]
        assert query["nonce"] == ["nonce_example"]
        assert query["v"] == ["1"]

    copy_url = urlparse(str(payload["copy_url"]))
    assert copy_url.scheme == "https"
    assert copy_url.netloc == "kongming.example.com"
    assert copy_url.path == "/-/xspace/mobile/login"


def test_login_qr_exchange_and_error_fixture_contract() -> None:
    """验证 exchange response 和错误响应 fixture 字段。"""
    payload = _fixture()
    exchange = payload["exchange_response"]  # type: ignore[index]
    origin = LoginQrOriginView.model_validate(exchange["server_origin"])

    assert origin.mode == "public_https"
    assert exchange["server"] == origin.origin
    assert str(exchange["device_token"]).startswith("kgm_dt_")
    assert str(exchange["web_session_url"]).startswith(
        "https://kongming.example.com/-/xspace/mobile/session/consume?handoff_token=kgm_ht_"
    )

    error_responses = payload["error_responses"]  # type: ignore[index]
    for code, item in error_responses.items():
        if code == "approval_pending":
            assert item["status"] == 202
            assert item["body"]["status"] == "pending_approval"
            continue
        assert item["status"] == errors.mobile_pairing_http_status(code)
        assert item["body"]["error"]["code"] == code
