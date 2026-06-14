"""XSpace Mobile 配对 Manager 单元测试。

本脚本验证 ``MobilePairingManager`` 的业务状态机，作用是固定 create、claim、
approve、exchange、handoff consume、revoke 这些后端核心语义。关键流程是用临时
SQLite 仓库创建真实 Manager，执行主链路和错误分支。关键测试函数职责：主链路覆盖
成功状态迁移，边界测试覆盖过期、nonce mismatch、重复 claim、pending 和 denied。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hosts.web.xspace_mobile import (
    HandoffTokenRecord,
    MobileDeviceDescriptor,
    MobileDeviceRecord,
    MobileDeviceTokenService,
    MobilePairingError,
    MobilePairingManager,
    MobilePairingRepository,
    PairingClaimStatus,
    PairingSessionStatus,
)


def _manager(tmp_path: Path) -> tuple[MobilePairingManager, MobilePairingRepository]:
    """创建测试 Manager。

    关键输入：pytest 临时目录。
    关键输出：共享同一 SQLite 文件的 Manager 和 Repository。
    """
    repo = MobilePairingRepository(tmp_path / "mobile_pairing.db")
    return MobilePairingManager(repo), repo


def _device(device_id: str = "android-pixel-9") -> MobileDeviceDescriptor:
    """创建测试设备描述。

    关键输入：可选设备 ID。
    关键输出：Android 设备 DTO。
    """
    return MobileDeviceDescriptor(
        device_id=device_id,
        label="Pixel 9",
        platform="android",
        app_version="1.0.0",
    )


def _claim(
    manager: MobilePairingManager,
    pairing_id: str,
    nonce: str,
    *,
    device_id: str = "android-pixel-9",
):
    """执行测试 claim。

    关键输入：Manager、pairing ID、nonce 和设备 ID。
    关键输出：claim 结果 DTO。
    """
    return manager.claim_session(
        pairing_id=pairing_id,
        protocol_version="1",
        nonce=nonce,
        device=_device(device_id),
        capabilities={"webview": True, "camera_scan": True},
    )


def test_pairing_happy_path_create_claim_approve_exchange(tmp_path: Path) -> None:
    """验证 create 到 handoff consume 的完整主链路。

    关键输入：临时 SQLite repository。
    关键输出：exchange 返回明文 token，持久化状态推进到 exchanged。
    """
    manager, repo = _manager(tmp_path)
    created = manager.create_pairing_session(
        protocol_version="1",
        client="kongming-web",
        requested_scopes=["webview", "thread.read"],
        server_origin="https://kongming.local",
    )

    assert created.pairing_id.startswith("pr_")
    assert "xspace://pair-kongming" in created.qr_payload
    assert created.nonce in created.copy_url

    claim = _claim(manager, created.pairing_id, created.nonce)
    assert claim.status == PairingClaimStatus.PENDING_APPROVAL

    approval = manager.approve_claim(
        pairing_id=created.pairing_id,
        claim_id=claim.claim_id,
        approved=True,
    )
    assert approval.session_status == PairingSessionStatus.APPROVED
    assert approval.claim_status == PairingClaimStatus.APPROVED

    exchanged = manager.exchange_pairing(
        pairing_id=created.pairing_id,
        claim_id=claim.claim_id,
        nonce=created.nonce,
        device_id="android-pixel-9",
    )
    assert exchanged.device_token.startswith("kgm_dt_")
    assert exchanged.handoff_token.startswith("kgm_ht_")

    session = repo.get_pairing_session(created.pairing_id)
    stored_claim = repo.get_claim(claim.claim_id)
    device = repo.get_device("android-pixel-9")
    assert session is not None
    assert stored_claim is not None
    assert device is not None
    assert session.status == PairingSessionStatus.EXCHANGED
    assert stored_claim.status == PairingClaimStatus.EXCHANGED
    assert device.token_hash != exchanged.device_token

    context = MobileDeviceTokenService(repo).consume_handoff_token(exchanged.handoff_token)
    assert context.device_id == "android-pixel-9"
    assert context.scopes == ["webview", "thread.read"]
    assert context.user_id == "android-pixel-9"


def test_pairing_expired_blocks_claim(tmp_path: Path) -> None:
    """验证过期 pairing 拒绝 claim。

    关键输入：TTL 为 1 秒的配对会话和过期后的当前时间。
    关键输出：``pairing_expired`` 错误。
    """
    manager, repo = _manager(tmp_path)
    now = datetime.now(UTC)
    created = manager.create_pairing_session(
        protocol_version="1",
        client="kongming-web",
        requested_scopes=["webview"],
        server_origin="https://kongming.local",
        ttl_seconds=1,
        now=now,
    )

    with pytest.raises(MobilePairingError) as exc_info:
        manager.claim_session(
            pairing_id=created.pairing_id,
            protocol_version="1",
            nonce=created.nonce,
            device=_device(),
            capabilities={"webview": True},
            now=now + timedelta(seconds=2),
        )

    assert exc_info.value.code == "pairing_expired"
    session = repo.get_pairing_session(created.pairing_id)
    assert session is not None
    assert session.status == PairingSessionStatus.EXPIRED


def test_nonce_mismatch_blocks_claim_and_exchange(tmp_path: Path) -> None:
    """验证 claim 和 exchange 均执行 nonce 校验。

    关键输入：错误 nonce。
    关键输出：``nonce_mismatch`` 错误。
    """
    manager, _repo = _manager(tmp_path)
    created = manager.create_pairing_session(
        protocol_version="1",
        client="kongming-web",
        requested_scopes=["webview"],
        server_origin="https://kongming.local",
    )

    with pytest.raises(MobilePairingError) as claim_error:
        _claim(manager, created.pairing_id, "wrong-nonce")
    assert claim_error.value.code == "nonce_mismatch"

    claim = _claim(manager, created.pairing_id, created.nonce)
    manager.approve_claim(pairing_id=created.pairing_id, claim_id=claim.claim_id, approved=True)

    with pytest.raises(MobilePairingError) as exchange_error:
        manager.exchange_pairing(
            pairing_id=created.pairing_id,
            claim_id=claim.claim_id,
            nonce="wrong-nonce",
            device_id="android-pixel-9",
        )
    assert exchange_error.value.code == "nonce_mismatch"


def test_repeated_claim_is_rejected(tmp_path: Path) -> None:
    """验证同一个 pairing 只能被一个设备 claim。

    关键输入：两个不同设备连续 claim 同一 pairing。
    关键输出：第二次 claim 返回 ``pairing_already_claimed``。
    """
    manager, _repo = _manager(tmp_path)
    created = manager.create_pairing_session(
        protocol_version="1",
        client="kongming-web",
        requested_scopes=["webview"],
        server_origin="https://kongming.local",
    )
    _claim(manager, created.pairing_id, created.nonce, device_id="android-a")

    with pytest.raises(MobilePairingError) as exc_info:
        _claim(manager, created.pairing_id, created.nonce, device_id="android-b")

    assert exc_info.value.code == "pairing_already_claimed"


def test_pending_exchange_returns_retryable_approval_pending(tmp_path: Path) -> None:
    """验证未审批 exchange 返回 pending 错误。

    关键输入：pending_approval claim。
    关键输出：可重试的 ``approval_pending`` 错误。
    """
    manager, _repo = _manager(tmp_path)
    created = manager.create_pairing_session(
        protocol_version="1",
        client="kongming-web",
        requested_scopes=["webview"],
        server_origin="https://kongming.local",
    )
    claim = _claim(manager, created.pairing_id, created.nonce)

    with pytest.raises(MobilePairingError) as exc_info:
        manager.exchange_pairing(
            pairing_id=created.pairing_id,
            claim_id=claim.claim_id,
            nonce=created.nonce,
            device_id="android-pixel-9",
        )

    assert exc_info.value.code == "approval_pending"
    assert exc_info.value.retryable is True


def test_denied_claim_blocks_exchange(tmp_path: Path) -> None:
    """验证拒绝后 exchange 返回 denied。

    关键输入：已拒绝 claim。
    关键输出：``approval_denied`` 错误。
    """
    manager, _repo = _manager(tmp_path)
    created = manager.create_pairing_session(
        protocol_version="1",
        client="kongming-web",
        requested_scopes=["webview"],
        server_origin="https://kongming.local",
    )
    claim = _claim(manager, created.pairing_id, created.nonce)
    denied = manager.deny_claim(pairing_id=created.pairing_id, claim_id=claim.claim_id)
    assert denied.approved is False
    assert denied.session_status == PairingSessionStatus.DENIED
    assert denied.claim_status == PairingClaimStatus.DENIED

    with pytest.raises(MobilePairingError) as exc_info:
        manager.exchange_pairing(
            pairing_id=created.pairing_id,
            claim_id=claim.claim_id,
            nonce=created.nonce,
            device_id="android-pixel-9",
        )

    assert exc_info.value.code == "approval_denied"


def test_repeated_exchange_does_not_issue_second_token(tmp_path: Path) -> None:
    """验证重复 exchange 在状态检查处被拦截。

    关键输入：已完成 exchange 的 pairing。
    关键输出：第二次 exchange 返回错误，设备表中仍只有首次 token hash。
    """
    manager, repo = _manager(tmp_path)
    created = manager.create_pairing_session(
        protocol_version="1",
        client="kongming-web",
        requested_scopes=["webview"],
        server_origin="https://kongming.local",
    )
    claim = _claim(manager, created.pairing_id, created.nonce)
    manager.approve_claim(pairing_id=created.pairing_id, claim_id=claim.claim_id, approved=True)
    exchanged = manager.exchange_pairing(
        pairing_id=created.pairing_id,
        claim_id=claim.claim_id,
        nonce=created.nonce,
        device_id="android-pixel-9",
    )
    device = repo.get_device("android-pixel-9")
    assert device is not None
    first_hash = device.token_hash

    with pytest.raises(MobilePairingError) as exc_info:
        manager.exchange_pairing(
            pairing_id=created.pairing_id,
            claim_id=claim.claim_id,
            nonce=created.nonce,
            device_id="android-pixel-9",
        )

    assert exc_info.value.code == "invalid_token"
    assert repo.get_device("android-pixel-9") is not None
    assert repo.get_device("android-pixel-9").token_hash == first_hash  # type: ignore[union-attr]
    assert exchanged.device_token.startswith("kgm_dt_")


def test_exchange_write_failure_keeps_pairing_retryable(tmp_path: Path) -> None:
    """验证真实 SQLite exchange 中途失败后 pairing 仍可重试。

    关键输入：handoff 主键冲突制造事务中途失败。
    关键输出：session/claim 和 device token 回滚，随后真实 Manager 可成功 exchange。
    """
    manager, repo = _manager(tmp_path)
    now = datetime(2026, 6, 13, tzinfo=UTC)
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
            user_id="android-pixel-9",
            expires_at=now + timedelta(seconds=60),
            consumed_at=None,
            created_at=now,
        )
    )
    created = manager.create_pairing_session(
        protocol_version="1",
        client="kongming-web",
        requested_scopes=["webview"],
        server_origin="https://kongming.local",
    )
    claim = _claim(manager, created.pairing_id, created.nonce)
    manager.approve_claim(pairing_id=created.pairing_id, claim_id=claim.claim_id, approved=True)

    with pytest.raises(sqlite3.IntegrityError):
        repo.complete_exchange(
            pairing_id=created.pairing_id,
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
                user_id="android-pixel-9",
                expires_at=now + timedelta(seconds=60),
                consumed_at=None,
                created_at=now,
            ),
        )

    session = repo.get_pairing_session(created.pairing_id)
    stored_claim = repo.get_claim(claim.claim_id)
    device = repo.get_device("android-pixel-9")
    assert session is not None
    assert stored_claim is not None
    assert device is not None
    assert session.status == PairingSessionStatus.APPROVED
    assert stored_claim.status == PairingClaimStatus.APPROVED
    assert device.token_hash == "existing-device-token-hash"

    exchanged = manager.exchange_pairing(
        pairing_id=created.pairing_id,
        claim_id=claim.claim_id,
        nonce=created.nonce,
        device_id="android-pixel-9",
    )
    assert exchanged.device_token.startswith("kgm_dt_")


def test_denied_exchange_after_ttl_keeps_denied_error(tmp_path: Path) -> None:
    """验证拒绝态超过 TTL 后仍返回 denied。

    关键输入：已拒绝且超过 TTL 的 pairing。
    关键输出：exchange 返回 ``approval_denied``。
    """
    manager, _repo = _manager(tmp_path)
    now = datetime.now(UTC)
    created = manager.create_pairing_session(
        protocol_version="1",
        client="kongming-web",
        requested_scopes=["webview"],
        server_origin="https://kongming.local",
        ttl_seconds=60,
        now=now,
    )
    claim = _claim(manager, created.pairing_id, created.nonce)
    manager.deny_claim(pairing_id=created.pairing_id, claim_id=claim.claim_id)

    with pytest.raises(MobilePairingError) as exc_info:
        manager.exchange_pairing(
            pairing_id=created.pairing_id,
            claim_id=claim.claim_id,
            nonce=created.nonce,
            device_id="android-pixel-9",
            now=now + timedelta(seconds=2),
        )

    assert exc_info.value.code == "approval_denied"


def test_wrong_claim_id_returns_claim_not_found(tmp_path: Path) -> None:
    """验证错误 claim ID 返回明确错误。

    关键输入：已批准 pairing 和错误 claim ID。
    关键输出：``claim_not_found`` 错误消息包含 claim ID。
    """
    manager, _repo = _manager(tmp_path)
    created = manager.create_pairing_session(
        protocol_version="1",
        client="kongming-web",
        requested_scopes=["webview"],
        server_origin="https://kongming.local",
    )
    claim = _claim(manager, created.pairing_id, created.nonce)
    manager.approve_claim(pairing_id=created.pairing_id, claim_id=claim.claim_id, approved=True)

    with pytest.raises(MobilePairingError) as exc_info:
        manager.exchange_pairing(
            pairing_id=created.pairing_id,
            claim_id="cl_wrong",
            nonce=created.nonce,
            device_id="android-pixel-9",
        )

    assert exc_info.value.code == "claim_not_found"
    assert "cl_wrong" in exc_info.value.message


def test_invalid_public_inputs_return_stable_errors(tmp_path: Path) -> None:
    """验证公开入口对空字符串类输入返回稳定错误。

    关键输入：None server_origin 和 None nonce。
    关键输出：抛 ``MobilePairingError``，无 AttributeError。
    """
    manager, _repo = _manager(tmp_path)

    with pytest.raises(MobilePairingError) as create_error:
        manager.create_pairing_session(
            protocol_version="1",
            client="kongming-web",
            requested_scopes=["webview"],
            server_origin=None,  # type: ignore[arg-type]
        )
    assert create_error.value.code == "invalid_request"

    with pytest.raises(MobilePairingError) as ttl_error:
        manager.create_pairing_session(
            protocol_version="1",
            client="kongming-web",
            requested_scopes=["webview"],
            server_origin="https://kongming.local",
            ttl_seconds=None,  # type: ignore[arg-type]
        )
    assert ttl_error.value.code == "invalid_request"

    created = manager.create_pairing_session(
        protocol_version="1",
        client="kongming-web",
        requested_scopes=["webview"],
        server_origin="https://kongming.local",
    )
    with pytest.raises(MobilePairingError) as claim_error:
        manager.claim_session(
            pairing_id=created.pairing_id,
            protocol_version="1",
            nonce=None,  # type: ignore[arg-type]
            device=_device(),
            capabilities={"webview": True},
        )
    assert claim_error.value.code == "invalid_request"

    with pytest.raises(MobilePairingError) as device_error:
        manager.claim_session(
            pairing_id=created.pairing_id,
            protocol_version="1",
            nonce=created.nonce,
            device=None,  # type: ignore[arg-type]
            capabilities={"webview": True},
        )
    assert device_error.value.code == "invalid_request"

    with pytest.raises(MobilePairingError) as empty_device_error:
        manager.claim_session(
            pairing_id=created.pairing_id,
            protocol_version="1",
            nonce=created.nonce,
            device=MobileDeviceDescriptor(
                device_id="",
                label="",
                platform="android",
                app_version="",
            ),
            capabilities={"webview": True},
        )
    assert empty_device_error.value.code == "invalid_request"


def test_invalid_approval_flag_does_not_change_state(tmp_path: Path) -> None:
    """验证非法 approved 参数不会推动状态机。

    关键输入：已 pending approval 的 pairing 和 None approved。
    关键输出：返回 ``invalid_request``，session/claim 仍是 pending approval。
    """
    manager, repo = _manager(tmp_path)
    created = manager.create_pairing_session(
        protocol_version="1",
        client="kongming-web",
        requested_scopes=["webview"],
        server_origin="https://kongming.local",
    )
    claim = _claim(manager, created.pairing_id, created.nonce)

    with pytest.raises(MobilePairingError) as exc_info:
        manager.approve_claim(
            pairing_id=created.pairing_id,
            claim_id=claim.claim_id,
            approved=None,  # type: ignore[arg-type]
        )

    session = repo.get_pairing_session(created.pairing_id)
    stored_claim = repo.get_claim(claim.claim_id)
    assert exc_info.value.code == "invalid_request"
    assert session is not None
    assert stored_claim is not None
    assert session.status == PairingSessionStatus.PENDING_APPROVAL
    assert stored_claim.status == PairingClaimStatus.PENDING_APPROVAL
