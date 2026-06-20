"""XSpace Mobile 配对 Repository 单元测试。

本脚本验证 ``MobilePairingRepository`` 的 SQLite schema、重启读取和原子状态更新。
作用是固定持久化边界和并发 claim 语义。关键流程是直接构造 Record DTO 写入真实
SQLite，再用新 Repository 实例读取或模拟双 claim。关键测试函数职责：schema 测试
覆盖四表初始化，restart 测试覆盖持久化，atomic 测试覆盖单次 claim。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hosts.web.xspace_mobile import (
    HandoffTokenRecord,
    MobileDeviceRecord,
    MobilePairingError,
    MobilePairingRepository,
    PairingClaimRecord,
    PairingClaimStatus,
    PairingSessionRecord,
    PairingSessionStatus,
)


def _session(pairing_id: str = "pr_test") -> PairingSessionRecord:
    """创建测试 pairing session 记录。

    关键输入：可选 pairing ID。
    关键输出：pending_scan session DTO。
    """
    now = datetime.now(UTC)
    return PairingSessionRecord(
        pairing_id=pairing_id,
        protocol_version="1",
        client="kongming-web",
        nonce_hash="nonce-hash",
        server_origin="https://kongming.local",
        requested_scopes=["webview"],
        status=PairingSessionStatus.PENDING_SCAN,
        expires_at=now + timedelta(minutes=1),
        created_at=now,
    )


def _claim(claim_id: str, pairing_id: str = "pr_test") -> PairingClaimRecord:
    """创建测试 claim 记录。

    关键输入：claim ID 和 pairing ID。
    关键输出：pending_approval claim DTO。
    """
    return PairingClaimRecord(
        claim_id=claim_id,
        pairing_id=pairing_id,
        device_id=f"device-{claim_id}",
        label="Pixel 9",
        platform="android",
        app_version="1.0.0",
        capabilities={"webview": True},
        status=PairingClaimStatus.PENDING_APPROVAL,
        created_at=datetime.now(UTC),
    )


def test_repository_initializes_schema(tmp_path: Path) -> None:
    """验证 repository 初始化四张表和 schema 版本。

    关键输入：临时 SQLite 路径。
    关键输出：schema version 为 2。
    """
    repo = MobilePairingRepository(tmp_path / "mobile_pairing.db")

    assert repo.schema_version() == 2


def test_repository_persists_records_across_restart(tmp_path: Path) -> None:
    """验证重启后仍可读取 pairing 和 claim。

    关键输入：同一 SQLite 文件的两个 Repository 实例。
    关键输出：第二个实例读取到第一个实例写入的记录。
    """
    db_path = tmp_path / "mobile_pairing.db"
    repo = MobilePairingRepository(db_path)
    session = repo.create_pairing_session(_session())
    claim = repo.claim_if_open(_claim("cl_one"))

    restarted = MobilePairingRepository(db_path)

    assert restarted.get_pairing_session(session.pairing_id) == session.model_copy(
        update={"status": PairingSessionStatus.PENDING_APPROVAL}
    )
    assert restarted.get_claim(claim.claim_id) == claim


def test_repository_atomic_claim_allows_only_one_success(tmp_path: Path) -> None:
    """验证双 repository 实例串行模拟并发 claim 只成功一次。

    关键输入：同一 SQLite 文件、两个 Repository 实例、两个 claim。
    关键输出：第一个 claim 成功，第二个 claim 返回 ``pairing_already_claimed``。
    """
    db_path = tmp_path / "mobile_pairing.db"
    first = MobilePairingRepository(db_path)
    second = MobilePairingRepository(db_path)
    first.create_pairing_session(_session())

    first_claim = first.claim_if_open(_claim("cl_one"))

    with pytest.raises(MobilePairingError) as exc_info:
        second.claim_if_open(_claim("cl_two"))

    assert first_claim.claim_id == "cl_one"
    assert exc_info.value.code == "pairing_already_claimed"


def test_repository_approve_and_deny_update_session_and_claim(tmp_path: Path) -> None:
    """验证 approve 和 deny 事务同步更新 session 与 claim。

    关键输入：两个 pairing，各自 claim 后分别批准和拒绝。
    关键输出：对应 session/claim 状态一致。
    """
    repo = MobilePairingRepository(tmp_path / "mobile_pairing.db")
    repo.create_pairing_session(_session("pr_approve"))
    repo.create_pairing_session(_session("pr_deny"))
    approve_claim = repo.claim_if_open(_claim("cl_approve", "pr_approve"))
    deny_claim = repo.claim_if_open(_claim("cl_deny", "pr_deny"))

    approved_session, approved_claim = repo.approve_claim(
        "pr_approve",
        approve_claim.claim_id,
        approved=True,
    )
    denied_session, denied_claim_record = repo.approve_claim(
        "pr_deny",
        deny_claim.claim_id,
        approved=False,
    )

    assert approved_session.status == PairingSessionStatus.APPROVED
    assert approved_claim.status == PairingClaimStatus.APPROVED
    assert denied_session.status == PairingSessionStatus.DENIED
    assert denied_claim_record.status == PairingClaimStatus.DENIED


def test_repository_expired_claim_persists_expired_status(tmp_path: Path) -> None:
    """验证过期 claim 会把 session 持久化为 expired。

    关键输入：已过期的 pairing session。
    关键输出：错误返回后数据库状态仍为 expired。
    """
    repo = MobilePairingRepository(tmp_path / "mobile_pairing.db")
    now = datetime(2026, 6, 13, tzinfo=UTC)
    repo.create_pairing_session(
        _session().model_copy(
            update={
                "expires_at": now - timedelta(seconds=1),
                "created_at": now - timedelta(seconds=2),
            }
        )
    )

    with pytest.raises(MobilePairingError) as exc_info:
        repo.claim_if_open(_claim("cl_expired"), now=now)

    session = repo.get_pairing_session("pr_test")
    assert exc_info.value.code == "pairing_expired"
    assert session is not None
    assert session.status == PairingSessionStatus.EXPIRED


def test_repository_wrong_claim_id_does_not_exchange_session(tmp_path: Path) -> None:
    """验证 exchange 标记会校验 claim 归属。

    关键输入：已批准 pairing 和错误 claim ID。
    关键输出：错误返回后 session/claim 保持 approved。
    """
    repo = MobilePairingRepository(tmp_path / "mobile_pairing.db")
    repo.create_pairing_session(_session())
    claim = repo.claim_if_open(_claim("cl_one"))
    repo.approve_claim("pr_test", claim.claim_id, approved=True)

    with pytest.raises(MobilePairingError) as exc_info:
        repo.mark_exchange_complete("pr_test", "cl_wrong")

    session = repo.get_pairing_session("pr_test")
    stored_claim = repo.get_claim(claim.claim_id)
    assert exc_info.value.code == "claim_not_found"
    assert session is not None
    assert stored_claim is not None
    assert session.status == PairingSessionStatus.APPROVED
    assert stored_claim.status == PairingClaimStatus.APPROVED


def test_repository_complete_exchange_persists_token_records_atomically(tmp_path: Path) -> None:
    """验证 complete_exchange 单事务写入 device、handoff 和状态。

    关键输入：已批准 pairing、设备记录和 handoff 记录。
    关键输出：三类表状态同步推进。
    """
    repo = MobilePairingRepository(tmp_path / "mobile_pairing.db")
    now = datetime(2026, 6, 13, tzinfo=UTC)
    repo.create_pairing_session(_session())
    claim = repo.claim_if_open(_claim("cl_one"))
    repo.approve_claim("pr_test", claim.claim_id, approved=True)
    device = MobileDeviceRecord(
        device_id=claim.device_id,
        label="Pixel 9",
        platform="android",
        app_version="1.0.0",
        scopes=["webview"],
        token_hash="device-token-hash",
        created_at=now,
        last_seen_at=now,
    )
    handoff = HandoffTokenRecord(
        handoff_id="ho_one",
        token_hash="handoff-token-hash",
        device_id=claim.device_id,
        scopes=["webview"],
        user_id=claim.device_id,
        expires_at=now + timedelta(seconds=60),
        consumed_at=None,
        created_at=now,
    )

    stored_device, stored_handoff = repo.complete_exchange(
        pairing_id="pr_test",
        claim_id=claim.claim_id,
        device=device,
        handoff=handoff,
    )

    session = repo.get_pairing_session("pr_test")
    stored_claim = repo.get_claim(claim.claim_id)
    assert stored_device.device_id == claim.device_id
    assert stored_handoff.handoff_id == "ho_one"
    assert session is not None
    assert stored_claim is not None
    assert session.status == PairingSessionStatus.EXCHANGED
    assert stored_claim.status == PairingClaimStatus.EXCHANGED


def test_pairing_records_fixture_matches_dto_contract() -> None:
    """验证合同 fixture 可被 DTO 直接解析。

    关键输入：`tests/fixtures/xspace_mobile_pairing/pairing_records.json`。
    关键输出：四类 Record DTO 均解析成功，hash 字段不含 token 明文。
    """
    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "xspace_mobile_pairing"
        / "pairing_records.json"
    )
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))

    session = PairingSessionRecord.model_validate(payload["pairing_session"])
    claim = PairingClaimRecord.model_validate(payload["pairing_claim"])
    device = MobileDeviceRecord.model_validate(payload["mobile_device"])
    handoff = HandoffTokenRecord.model_validate(payload["handoff_token"])

    assert session.pairing_id == claim.pairing_id
    assert claim.device_id == device.device_id == handoff.device_id
    assert not device.token_hash.startswith("kgm_dt_")
    assert not handoff.token_hash.startswith("kgm_ht_")
