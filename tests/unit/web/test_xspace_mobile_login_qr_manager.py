"""XSpace Mobile 登录二维码 Manager 单元测试。

本脚本验证 ``LoginQrManager`` 的业务状态机，作用是固定 create、status、claim、
confirm、exchange、cancel 和事务回滚语义。关键流程是用临时 SQLite 仓库创建真实
Manager，执行公网和 LAN 两类扫码登录链路。关键测试函数职责：主链路覆盖成功状态
迁移，边界测试覆盖过期、nonce mismatch、browser token mismatch、重复 claim 和
重复 exchange。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hosts.web.xspace_mobile import (
    HandoffTokenRecord,
    LoginQrClaimStatus,
    LoginQrManager,
    LoginQrSessionStatus,
    MobileDeviceDescriptor,
    MobileDeviceRecord,
    MobileDeviceTokenService,
    MobilePairingError,
    MobilePairingRepository,
)


def _manager(tmp_path: Path) -> tuple[LoginQrManager, MobilePairingRepository]:
    """创建测试 Manager。

    关键输入：pytest 临时目录。
    关键输出：共享同一 SQLite 文件的 Manager 和 Repository。
    """
    repo = MobilePairingRepository(tmp_path / "mobile_pairing.db")
    return LoginQrManager(repo), repo


def _device(device_id: str = "android-pixel-9") -> MobileDeviceDescriptor:
    """创建测试设备描述。"""
    return MobileDeviceDescriptor(
        device_id=device_id,
        label="Pixel 9",
        platform="android",
        app_version="1.0.0",
    )


def _create(
    manager: LoginQrManager,
    *,
    raw_server_origin: str = "https://kongming.example.com",
    now: datetime | None = None,
):
    """创建测试登录二维码会话。"""
    return manager.create_login_qr_session(
        protocol_version="1",
        client="kongming-login",
        requested_scopes=["webview", "thread.read"],
        raw_server_origin=raw_server_origin,
        now=now,
    )


def _claim(
    manager: LoginQrManager,
    login_qr_id: str,
    nonce: str,
    *,
    device_id: str = "android-pixel-9",
    now: datetime | None = None,
):
    """执行测试 claim。"""
    return manager.claim_login_qr(
        login_qr_id=login_qr_id,
        protocol_version="1",
        nonce=nonce,
        device=_device(device_id),
        capabilities={"webview": True, "camera_scan": True},
        now=now,
    )


def _confirm(manager: LoginQrManager, created, claim, *, now: datetime | None = None):
    """执行测试确认。"""
    return manager.confirm_login_qr(
        login_qr_id=created.login_qr_id,
        claim_id=claim.claim_id,
        browser_token=created.browser_token,
        now=now,
    )


def test_login_qr_happy_path_public_https_claim_confirm_exchange(tmp_path: Path) -> None:
    """验证公网 HTTPS create 到 handoff consume 的完整主链路。"""
    manager, repo = _manager(tmp_path)

    created = _create(manager)

    assert created.login_qr_id.startswith("lq_")
    assert created.browser_token.startswith("kgm_lqt_")
    assert created.server_origin.mode == "public_https"
    assert created.server == "https://kongming.example.com"
    assert created.qr_payload.startswith("xspace://login-kongming?")
    assert "purpose=login" in created.qr_payload
    assert created.copy_url.startswith("https://kongming.example.com/-/xspace/mobile/login?")

    claim = _claim(manager, created.login_qr_id, created.nonce)
    assert claim.status == LoginQrClaimStatus.PENDING_CONFIRM

    session, stored_claim = manager.get_login_qr_view(
        created.login_qr_id,
        browser_token=created.browser_token,
    )
    assert session.status == LoginQrSessionStatus.PENDING_CONFIRM
    assert stored_claim is not None
    assert stored_claim.device_id == "android-pixel-9"

    confirmed = _confirm(manager, created, claim)
    assert confirmed.status == LoginQrSessionStatus.CONFIRMED

    exchanged = manager.exchange_login_qr(
        login_qr_id=created.login_qr_id,
        claim_id=claim.claim_id,
        nonce=created.nonce,
        device_id="android-pixel-9",
    )
    assert exchanged.device_token.startswith("kgm_dt_")
    assert exchanged.handoff_token.startswith("kgm_ht_")
    assert exchanged.server_origin.origin == "https://kongming.example.com"

    stored_session = repo.get_login_qr_session(created.login_qr_id)
    stored_claim = repo.get_login_qr_claim(claim.claim_id)
    device = repo.get_device("android-pixel-9")
    assert stored_session is not None
    assert stored_claim is not None
    assert device is not None
    assert stored_session.status == LoginQrSessionStatus.EXCHANGED
    assert stored_claim.status == LoginQrClaimStatus.EXCHANGED
    assert stored_session.authorization_method == "password"
    assert stored_session.authorized_user_id == "default"
    assert device.token_hash != exchanged.device_token

    context = MobileDeviceTokenService(repo).consume_handoff_token(exchanged.handoff_token)
    assert context.device_id == "android-pixel-9"
    assert context.scopes == ["webview", "thread.read"]
    assert context.user_id == "default"


def test_login_qr_happy_path_lan_ip_origin(tmp_path: Path) -> None:
    """验证 LAN HTTP 私网 IP origin 进入 lan_ip 模式。"""
    manager, _repo = _manager(tmp_path)

    created = _create(manager, raw_server_origin="http://192.168.31.23:8765")

    assert created.server_origin.mode == "lan_ip"
    assert created.server_origin.scheme == "http"
    assert created.server_origin.host == "192.168.31.23"
    assert created.server_origin.port == 8765
    assert "origin_mode=lan_ip" in created.qr_payload


def test_browser_token_mismatch_blocks_status_and_confirm(tmp_path: Path) -> None:
    """验证 status 和 confirm 都校验 browser token。"""
    manager, _repo = _manager(tmp_path)
    created = _create(manager)

    with pytest.raises(MobilePairingError) as status_error:
        manager.get_login_qr_view(created.login_qr_id, browser_token="wrong-token")
    assert status_error.value.code == "browser_token_mismatch"

    claim = _claim(manager, created.login_qr_id, created.nonce)
    with pytest.raises(MobilePairingError) as confirm_error:
        manager.confirm_login_qr(
            login_qr_id=created.login_qr_id,
            claim_id=claim.claim_id,
            browser_token="wrong-token",
        )
    assert confirm_error.value.code == "browser_token_mismatch"


def test_login_qr_expired_blocks_claim_and_status_marks_expired(tmp_path: Path) -> None:
    """验证过期登录二维码拒绝 claim，状态查询返回 expired 快照。"""
    manager, repo = _manager(tmp_path)
    now = datetime.now(UTC)
    created = manager.create_login_qr_session(
        protocol_version="1",
        client="kongming-login",
        requested_scopes=["webview"],
        raw_server_origin="https://kongming.example.com",
        ttl_seconds=1,
        now=now,
    )

    with pytest.raises(MobilePairingError) as exc_info:
        _claim(
            manager,
            created.login_qr_id,
            created.nonce,
            now=now + timedelta(seconds=2),
        )

    assert exc_info.value.code == "login_qr_expired"
    session, claim = manager.get_login_qr_view(
        created.login_qr_id,
        browser_token=created.browser_token,
        now=now + timedelta(seconds=2),
    )
    assert claim is None
    assert session.status == LoginQrSessionStatus.EXPIRED
    stored = repo.get_login_qr_session(created.login_qr_id)
    assert stored is not None
    assert stored.status == LoginQrSessionStatus.EXPIRED


def test_nonce_mismatch_blocks_claim_and_exchange(tmp_path: Path) -> None:
    """验证 claim 和 exchange 均执行 nonce 校验。"""
    manager, _repo = _manager(tmp_path)
    created = _create(manager)

    with pytest.raises(MobilePairingError) as claim_error:
        _claim(manager, created.login_qr_id, "wrong-nonce")
    assert claim_error.value.code == "nonce_mismatch"

    claim = _claim(manager, created.login_qr_id, created.nonce)
    _confirm(manager, created, claim)

    with pytest.raises(MobilePairingError) as exchange_error:
        manager.exchange_login_qr(
            login_qr_id=created.login_qr_id,
            claim_id=claim.claim_id,
            nonce="wrong-nonce",
            device_id="android-pixel-9",
        )
    assert exchange_error.value.code == "nonce_mismatch"


def test_repeated_claim_is_rejected(tmp_path: Path) -> None:
    """验证同一个登录二维码只能被一个设备 claim。"""
    manager, _repo = _manager(tmp_path)
    created = _create(manager)
    _claim(manager, created.login_qr_id, created.nonce, device_id="android-a")

    with pytest.raises(MobilePairingError) as exc_info:
        _claim(manager, created.login_qr_id, created.nonce, device_id="android-b")

    assert exc_info.value.code == "login_qr_already_claimed"


def test_pending_exchange_returns_retryable_approval_pending(tmp_path: Path) -> None:
    """验证未确认 exchange 返回 pending 错误。"""
    manager, _repo = _manager(tmp_path)
    created = _create(manager)
    claim = _claim(manager, created.login_qr_id, created.nonce)

    with pytest.raises(MobilePairingError) as exc_info:
        manager.exchange_login_qr(
            login_qr_id=created.login_qr_id,
            claim_id=claim.claim_id,
            nonce=created.nonce,
            device_id="android-pixel-9",
        )

    assert exc_info.value.code == "approval_pending"
    assert exc_info.value.retryable is True


def test_cancelled_login_qr_blocks_exchange(tmp_path: Path) -> None:
    """验证取消后 exchange 返回 denied。"""
    manager, repo = _manager(tmp_path)
    created = _create(manager)
    claim = _claim(manager, created.login_qr_id, created.nonce)

    cancelled = manager.cancel_login_qr(
        created.login_qr_id,
        browser_token=created.browser_token,
    )

    assert cancelled.status == LoginQrSessionStatus.CANCELLED
    stored_claim = repo.get_login_qr_claim(claim.claim_id)
    assert stored_claim is not None
    assert stored_claim.status == LoginQrClaimStatus.DENIED
    with pytest.raises(MobilePairingError) as exc_info:
        manager.exchange_login_qr(
            login_qr_id=created.login_qr_id,
            claim_id=claim.claim_id,
            nonce=created.nonce,
            device_id="android-pixel-9",
        )
    assert exc_info.value.code == "approval_denied"


def test_repeated_exchange_does_not_issue_second_token(tmp_path: Path) -> None:
    """验证重复 exchange 在状态检查处被拦截。"""
    manager, repo = _manager(tmp_path)
    created = _create(manager)
    claim = _claim(manager, created.login_qr_id, created.nonce)
    _confirm(manager, created, claim)
    exchanged = manager.exchange_login_qr(
        login_qr_id=created.login_qr_id,
        claim_id=claim.claim_id,
        nonce=created.nonce,
        device_id="android-pixel-9",
    )
    device = repo.get_device("android-pixel-9")
    assert device is not None
    first_hash = device.token_hash

    with pytest.raises(MobilePairingError) as exc_info:
        manager.exchange_login_qr(
            login_qr_id=created.login_qr_id,
            claim_id=claim.claim_id,
            nonce=created.nonce,
            device_id="android-pixel-9",
        )

    assert exc_info.value.code == "login_qr_already_exchanged"
    assert repo.get_device("android-pixel-9") is not None
    assert repo.get_device("android-pixel-9").token_hash == first_hash  # type: ignore[union-attr]
    assert exchanged.device_token.startswith("kgm_dt_")


def test_login_qr_exchange_write_failure_keeps_session_retryable(tmp_path: Path) -> None:
    """验证真实 SQLite exchange 中途失败后登录二维码仍可重试。"""
    manager, repo = _manager(tmp_path)
    now = datetime(2026, 6, 19, tzinfo=UTC)
    repo.upsert_device(
        MobileDeviceRecord(
            device_id="android-pixel-9",
            label="Pixel 9",
            platform="android",
            app_version="1.0.0",
            scopes=["webview"],
            token_hash="existing-device-token-hash",
            created_at=now,
            last_seen_at=now,
        )
    )
    repo.create_handoff_token(
        HandoffTokenRecord(
            handoff_id="ho_conflict",
            token_hash="existing-handoff-token-hash",
            device_id="android-pixel-9",
            scopes=["webview"],
            user_id="default",
            expires_at=now + timedelta(seconds=60),
            consumed_at=None,
            created_at=now,
        )
    )
    created = _create(manager, now=now)
    claim = _claim(manager, created.login_qr_id, created.nonce, now=now)
    _confirm(manager, created, claim, now=now)

    with pytest.raises(sqlite3.IntegrityError):
        repo.complete_login_qr_exchange(
            login_qr_id=created.login_qr_id,
            claim_id=claim.claim_id,
            device=MobileDeviceRecord(
                device_id="android-pixel-9",
                label="Pixel 9",
                platform="android",
                app_version="1.0.0",
                scopes=["webview"],
                token_hash="new-device-token-hash",
                created_at=now,
                last_seen_at=now,
            ),
            handoff=HandoffTokenRecord(
                handoff_id="ho_conflict",
                token_hash="new-handoff-token-hash",
                device_id="android-pixel-9",
                scopes=["webview"],
                user_id="default",
                expires_at=now + timedelta(seconds=60),
                consumed_at=None,
                created_at=now,
            ),
            now=now,
        )

    session = repo.get_login_qr_session(created.login_qr_id)
    stored_claim = repo.get_login_qr_claim(claim.claim_id)
    device = repo.get_device("android-pixel-9")
    assert session is not None
    assert stored_claim is not None
    assert device is not None
    assert session.status == LoginQrSessionStatus.CONFIRMED
    assert stored_claim.status == LoginQrClaimStatus.APPROVED
    assert device.token_hash == "existing-device-token-hash"

    exchanged = manager.exchange_login_qr(
        login_qr_id=created.login_qr_id,
        claim_id=claim.claim_id,
        nonce=created.nonce,
        device_id="android-pixel-9",
        now=now,
    )
    assert exchanged.device_token.startswith("kgm_dt_")
