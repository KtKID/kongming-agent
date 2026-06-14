"""XSpace Mobile TokenService 单元测试。

本脚本验证 ``MobileDeviceTokenService`` 的 device token hash、handoff 单次消费、
TTL 和吊销语义。作用是确保服务端只保存 hash，handoff token 只能使用一次，设备
吊销后无法继续换取 handoff。关键测试函数职责：签发测试覆盖 hash 边界，consume
测试覆盖一次性与过期，revoke 测试覆盖 device token 失效。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hosts.web.xspace_mobile import (
    MobileDeviceDescriptor,
    MobileDeviceTokenService,
    MobilePairingError,
    MobilePairingRepository,
)
from hosts.web.xspace_mobile.token_service import hash_token


def _service(tmp_path: Path) -> tuple[MobileDeviceTokenService, MobilePairingRepository]:
    """创建测试 TokenService。

    关键输入：pytest 临时目录。
    关键输出：共享 SQLite repository 的 TokenService 和 Repository。
    """
    repo = MobilePairingRepository(tmp_path / "mobile_pairing.db")
    return MobileDeviceTokenService(repo), repo


def _device() -> MobileDeviceDescriptor:
    """创建测试设备。

    关键输入：无。
    关键输出：Android 设备 DTO。
    """
    return MobileDeviceDescriptor(
        device_id="android-pixel-9",
        label="Pixel 9",
        platform="android",
        app_version="1.0.0",
    )


def test_device_token_plaintext_is_not_persisted(tmp_path: Path) -> None:
    """验证 device token 明文只返回一次且数据库保存 hash。

    关键输入：设备描述和 scope。
    关键输出：存储 token_hash 与明文不同。
    """
    service, repo = _service(tmp_path)
    issued = service.issue_device_token(device=_device(), scopes=["webview"])

    stored = repo.get_device("android-pixel-9")

    assert issued.device_token.startswith("kgm_dt_")
    assert stored is not None
    assert stored.token_hash != issued.device_token
    assert service.validate_device_token(issued.device_token).device_id == "android-pixel-9"


def test_handoff_token_can_be_consumed_once(tmp_path: Path) -> None:
    """验证 handoff token 单次消费。

    关键输入：已签发 device token 和 handoff token。
    关键输出：首次消费成功，第二次消费返回 ``handoff_consumed``。
    """
    service, _repo = _service(tmp_path)
    issued = service.issue_device_token(device=_device(), scopes=["webview"])
    handoff = service.issue_handoff_for_device_token(issued.device_token)
    stored_handoff = _repo.get_handoff_by_token_hash(hash_token(handoff.handoff_token))

    assert stored_handoff is not None
    assert stored_handoff.token_hash != handoff.handoff_token
    with sqlite3.connect(_repo.db_path) as conn:
        rows = conn.execute("SELECT token_hash FROM handoff_tokens").fetchall()
    assert handoff.handoff_token not in {row[0] for row in rows}
    context = service.consume_handoff_token(handoff.handoff_token)

    assert context.device_id == "android-pixel-9"
    assert context.scopes == ["webview"]

    with pytest.raises(MobilePairingError) as exc_info:
        service.consume_handoff_token(handoff.handoff_token)

    assert exc_info.value.code == "handoff_consumed"


def test_handoff_token_expiry_is_enforced(tmp_path: Path) -> None:
    """验证 handoff token TTL。

    关键输入：固定当前时间和 1 秒 TTL。
    关键输出：过期后消费返回 ``handoff_expired``。
    """
    service, _repo = _service(tmp_path)
    now = datetime(2026, 6, 13, tzinfo=UTC)
    issued = service.issue_device_token(device=_device(), scopes=["webview"], now=now)
    handoff = service.issue_handoff_for_device_token(
        issued.device_token,
        ttl_seconds=1,
        now=now,
    )

    with pytest.raises(MobilePairingError) as exc_info:
        service.consume_handoff_token(
            handoff.handoff_token,
            now=now + timedelta(seconds=2),
        )

    assert exc_info.value.code == "handoff_expired"


def test_revoked_device_token_is_rejected(tmp_path: Path) -> None:
    """验证吊销设备后 device token 失效。

    关键输入：已签发 device token 和吊销操作。
    关键输出：校验和 handoff 签发均返回 ``device_revoked``。
    """
    service, repo = _service(tmp_path)
    issued = service.issue_device_token(device=_device(), scopes=["webview"])
    repo.revoke_device("android-pixel-9")

    with pytest.raises(MobilePairingError) as validate_error:
        service.validate_device_token(issued.device_token)
    assert validate_error.value.code == "device_revoked"

    with pytest.raises(MobilePairingError) as handoff_error:
        service.issue_handoff_for_device_token(issued.device_token)
    assert handoff_error.value.code == "device_revoked"


def test_invalid_token_inputs_return_stable_errors(tmp_path: Path) -> None:
    """验证空 token 输入返回稳定错误。

    关键输入：None device token 和 None handoff token。
    关键输出：``invalid_token`` 错误，无 AttributeError。
    """
    service, _repo = _service(tmp_path)

    with pytest.raises(MobilePairingError) as device_error:
        service.validate_device_token(None)  # type: ignore[arg-type]
    assert device_error.value.code == "invalid_token"

    with pytest.raises(MobilePairingError) as handoff_error:
        service.consume_handoff_token(None)  # type: ignore[arg-type]
    assert handoff_error.value.code == "invalid_token"

    with pytest.raises(MobilePairingError) as device_error:
        service.issue_device_token(device=None, scopes=["webview"])  # type: ignore[arg-type]
    assert device_error.value.code == "invalid_request"

    with pytest.raises(MobilePairingError) as exchange_device_error:
        service.issue_exchange_tokens(
            pairing_id="pr_missing",
            claim_id="cl_missing",
            device=None,  # type: ignore[arg-type]
            scopes=["webview"],
            user_id="android-pixel-9",
        )
    assert exchange_device_error.value.code == "invalid_request"

    with pytest.raises(MobilePairingError) as empty_device_error:
        service.issue_device_token(
            device=MobileDeviceDescriptor(
                device_id="",
                label="",
                platform="android",
                app_version="",
            ),
            scopes=["webview"],
        )
    assert empty_device_error.value.code == "invalid_request"


def test_invalid_handoff_ttl_returns_stable_error(tmp_path: Path) -> None:
    """验证非法 TTL 返回稳定错误。

    关键输入：0 秒 handoff TTL。
    关键输出：``invalid_request`` 错误。
    """
    service, _repo = _service(tmp_path)
    issued = service.issue_device_token(device=_device(), scopes=["webview"])

    with pytest.raises(MobilePairingError) as exc_info:
        service.issue_handoff_for_device_token(issued.device_token, ttl_seconds=0)

    assert exc_info.value.code == "invalid_request"

    with pytest.raises(MobilePairingError) as none_error:
        service.issue_handoff_for_device_token(
            issued.device_token,
            ttl_seconds=None,  # type: ignore[arg-type]
        )
    assert none_error.value.code == "invalid_request"
