"""XSpace Mobile 登录二维码授权服务单元测试。

本脚本验证 ``LoginQrAuthService`` 的密码确认语义，作用是固定扫码登录签发前授权
边界。关键流程是用真实 ``LoginQrManager`` 和 ``LoginRateLimiter`` 执行 claim 后的
密码确认，断言错误密码不推进状态，正确密码写入授权证据，限流复用 Web 登录策略。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hosts.web.auth.secrets import hash_password
from hosts.web.rate_limit import LoginRateLimiter
from hosts.web.xspace_mobile import (
    LoginQrAuthService,
    LoginQrClaimStatus,
    LoginQrManager,
    LoginQrSessionStatus,
    MobileDeviceDescriptor,
    MobilePairingError,
    MobilePairingRepository,
)


def _stack(tmp_path: Path) -> tuple[LoginQrManager, LoginQrAuthService, MobilePairingRepository]:
    """创建测试 Manager/AuthService/Repository。"""
    repo = MobilePairingRepository(tmp_path / "mobile_pairing.db")
    manager = LoginQrManager(repo)
    auth_service = LoginQrAuthService(
        manager=manager,
        rate_limiter=LoginRateLimiter(max_failures=2, lockout_seconds=300),
    )
    return manager, auth_service, repo


def _created_and_claim(manager: LoginQrManager):
    """创建并 claim 一个登录二维码会话。"""
    created = manager.create_login_qr_session(
        protocol_version="1",
        client="kongming-login",
        requested_scopes=["webview"],
        raw_server_origin="https://kongming.example.com",
    )
    claim = manager.claim_login_qr(
        login_qr_id=created.login_qr_id,
        protocol_version="1",
        nonce=created.nonce,
        device=MobileDeviceDescriptor(
            device_id="android-pixel-9",
            label="Pixel 9",
            platform="android",
            app_version="1.0.0",
        ),
        capabilities={"webview": True},
    )
    return created, claim


@pytest.mark.asyncio
async def test_confirm_with_password_writes_authorization_evidence(tmp_path: Path) -> None:
    """验证正确密码写入 password 授权证据。"""
    manager, auth_service, repo = _stack(tmp_path)
    created, claim = _created_and_claim(manager)

    result = await auth_service.confirm_with_password(
        login_qr_id=created.login_qr_id,
        claim_id=claim.claim_id,
        browser_token=created.browser_token,
        password="pwd",
        password_hash=hash_password("pwd"),
        client_ip="127.0.0.1",
    )

    assert result.status == LoginQrSessionStatus.CONFIRMED
    session = repo.get_login_qr_session(created.login_qr_id)
    stored_claim = repo.get_login_qr_claim(claim.claim_id)
    assert session is not None
    assert stored_claim is not None
    assert session.authorization_method == "password"
    assert session.authorized_user_id == "default"
    assert stored_claim.status == LoginQrClaimStatus.APPROVED


@pytest.mark.asyncio
async def test_wrong_password_keeps_exchange_blocked(tmp_path: Path) -> None:
    """验证错误密码不会推进状态和签发路径。"""
    manager, auth_service, repo = _stack(tmp_path)
    created, claim = _created_and_claim(manager)

    with pytest.raises(MobilePairingError) as exc_info:
        await auth_service.confirm_with_password(
            login_qr_id=created.login_qr_id,
            claim_id=claim.claim_id,
            browser_token=created.browser_token,
            password="wrong",
            password_hash=hash_password("pwd"),
            client_ip="127.0.0.1",
        )

    assert exc_info.value.code == "invalid_credentials"
    session = repo.get_login_qr_session(created.login_qr_id)
    stored_claim = repo.get_login_qr_claim(claim.claim_id)
    assert session is not None
    assert stored_claim is not None
    assert session.status == LoginQrSessionStatus.PENDING_CONFIRM
    assert session.authorization_method is None
    assert stored_claim.status == LoginQrClaimStatus.PENDING_CONFIRM
    with pytest.raises(MobilePairingError) as exchange_error:
        manager.exchange_login_qr(
            login_qr_id=created.login_qr_id,
            claim_id=claim.claim_id,
            nonce=created.nonce,
            device_id="android-pixel-9",
        )
    assert exchange_error.value.code == "approval_pending"


@pytest.mark.asyncio
async def test_password_failures_reuse_login_rate_limiter(tmp_path: Path) -> None:
    """验证二维码确认复用 Web 登录限流器。"""
    manager, auth_service, _repo = _stack(tmp_path)
    created, claim = _created_and_claim(manager)

    for _ in range(2):
        with pytest.raises(MobilePairingError) as exc_info:
            await auth_service.confirm_with_password(
                login_qr_id=created.login_qr_id,
                claim_id=claim.claim_id,
                browser_token=created.browser_token,
                password="wrong",
                password_hash=hash_password("pwd"),
                client_ip="10.0.0.5",
            )
        assert exc_info.value.code == "invalid_credentials"

    with pytest.raises(MobilePairingError) as rate_error:
        await auth_service.confirm_with_password(
            login_qr_id=created.login_qr_id,
            claim_id=claim.claim_id,
            browser_token=created.browser_token,
            password="pwd",
            password_hash=hash_password("pwd"),
            client_ip="10.0.0.5",
        )

    assert rate_error.value.code == "rate_limited"
    assert rate_error.value.retryable is True
