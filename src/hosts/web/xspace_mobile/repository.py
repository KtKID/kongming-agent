"""XSpace Mobile 配对 SQLite 持久化门户。

本脚本实现 ``MobilePairingRepository``，作用是封装移动扫码配对相关 SQLite
schema、记录读写和原子状态更新。关键流程是初始化四张表，使用短连接和
``BEGIN IMMEDIATE`` 执行 claim-if-open、approve/deny、exchange、handoff consume
等事务。关键类/函数职责：Repository 是持久化唯一入口，序列化函数负责
Record DTO 与 SQLite 行互转，事务方法负责维护跨表状态一致性。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hosts.web.xspace_mobile import errors
from hosts.web.xspace_mobile.models import (
    HandoffLoginContext,
    HandoffTokenRecord,
    MobileDeviceRecord,
    PairingClaimRecord,
    PairingClaimStatus,
    PairingSessionRecord,
    PairingSessionStatus,
)

_SCHEMA_VERSION = 1


def _utc_now() -> datetime:
    """生成当前 UTC 时间。

    关键输入：系统时钟。
    关键输出：带 UTC 时区的 ``datetime``。
    """
    return datetime.now(UTC)


def _to_iso(value: datetime | None) -> str | None:
    """把时间转换为 SQLite 存储字符串。

    关键输入：UTC ``datetime`` 或空值。
    关键输出：ISO 8601 字符串或 ``None``。
    """
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _from_iso(value: str | None) -> datetime | None:
    """把 SQLite 时间字符串转换为 UTC 时间。

    关键输入：ISO 8601 字符串或空值。
    关键输出：UTC ``datetime`` 或 ``None``。
    """
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _json_dump(value: Any) -> str:
    """把结构化字段转换为 JSON 字符串。

    关键输入：list/dict 等 JSON 可序列化值。
    关键输出：紧凑 JSON 字符串。
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_load(raw: str | None, fallback: Any) -> Any:
    """把 SQLite JSON 字符串转换为 Python 对象。

    关键输入：JSON 字符串和解析失败时的 fallback。
    关键输出：解析出的 Python 对象。
    """
    if raw is None:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


def _session_from_row(row: sqlite3.Row) -> PairingSessionRecord:
    """把 pairing_sessions 行转换为 DTO。

    关键输入：SQLite row。
    关键输出：``PairingSessionRecord``。
    """
    return PairingSessionRecord(
        pairing_id=str(row["pairing_id"]),
        protocol_version=str(row["protocol_version"]),
        client=str(row["client"]),
        nonce_hash=str(row["nonce_hash"]),
        server_origin=str(row["server_origin"]),
        requested_scopes=list(_json_load(row["requested_scopes"], [])),
        status=PairingSessionStatus(str(row["status"])),
        expires_at=_from_iso(row["expires_at"]) or _utc_now(),
        created_at=_from_iso(row["created_at"]) or _utc_now(),
        approved_at=_from_iso(row["approved_at"]),
        denied_at=_from_iso(row["denied_at"]),
    )


def _claim_from_row(row: sqlite3.Row) -> PairingClaimRecord:
    """把 pairing_claims 行转换为 DTO。

    关键输入：SQLite row。
    关键输出：``PairingClaimRecord``。
    """
    return PairingClaimRecord(
        claim_id=str(row["claim_id"]),
        pairing_id=str(row["pairing_id"]),
        device_id=str(row["device_id"]),
        label=str(row["label"]),
        platform="android",
        app_version=str(row["app_version"]),
        capabilities=dict(_json_load(row["capabilities"], {})),
        status=PairingClaimStatus(str(row["status"])),
        created_at=_from_iso(row["created_at"]) or _utc_now(),
    )


def _device_from_row(row: sqlite3.Row) -> MobileDeviceRecord:
    """把 mobile_devices 行转换为 DTO。

    关键输入：SQLite row。
    关键输出：``MobileDeviceRecord``。
    """
    return MobileDeviceRecord(
        device_id=str(row["device_id"]),
        label=str(row["label"]),
        platform="android",
        app_version=str(row["app_version"]),
        scopes=list(_json_load(row["scopes"], [])),
        token_hash=str(row["token_hash"]),
        created_at=_from_iso(row["created_at"]) or _utc_now(),
        last_seen_at=_from_iso(row["last_seen_at"]),
        revoked_at=_from_iso(row["revoked_at"]),
    )


def _handoff_from_row(row: sqlite3.Row) -> HandoffTokenRecord:
    """把 handoff_tokens 行转换为 DTO。

    关键输入：SQLite row。
    关键输出：``HandoffTokenRecord``。
    """
    return HandoffTokenRecord(
        handoff_id=str(row["handoff_id"]),
        token_hash=str(row["token_hash"]),
        device_id=str(row["device_id"]),
        scopes=list(_json_load(row["scopes"], [])),
        user_id=str(row["user_id"]),
        expires_at=_from_iso(row["expires_at"]) or _utc_now(),
        consumed_at=_from_iso(row["consumed_at"]),
        created_at=_from_iso(row["created_at"]) or _utc_now(),
    )


def _upsert_device(conn: sqlite3.Connection, record: MobileDeviceRecord) -> None:
    """写入或更新移动设备记录。

    关键输入：SQLite 连接和设备 record。
    关键输出：mobile_devices 表完成 upsert。
    """
    conn.execute(
        """
        INSERT INTO mobile_devices (
            device_id, label, platform, app_version, scopes, token_hash,
            created_at, last_seen_at, revoked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            label = excluded.label,
            platform = excluded.platform,
            app_version = excluded.app_version,
            scopes = excluded.scopes,
            token_hash = excluded.token_hash,
            last_seen_at = excluded.last_seen_at,
            revoked_at = NULL
        """,
        (
            record.device_id,
            record.label,
            record.platform,
            record.app_version,
            _json_dump(record.scopes),
            record.token_hash,
            _to_iso(record.created_at),
            _to_iso(record.last_seen_at),
            _to_iso(record.revoked_at),
        ),
    )


def _insert_handoff_token(conn: sqlite3.Connection, record: HandoffTokenRecord) -> None:
    """写入 handoff token 记录。

    关键输入：SQLite 连接和 handoff record。
    关键输出：handoff_tokens 表新增可消费记录。
    """
    conn.execute(
        """
        INSERT INTO handoff_tokens (
            handoff_id, token_hash, device_id, scopes, user_id,
            expires_at, consumed_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.handoff_id,
            record.token_hash,
            record.device_id,
            _json_dump(record.scopes),
            record.user_id,
            _to_iso(record.expires_at),
            _to_iso(record.consumed_at),
            _to_iso(record.created_at),
        ),
    )


class MobilePairingRepository:
    """移动配对 SQLite 持久化门户。

    职责：初始化 schema、隐藏 SQL 细节、提供原子状态更新方法。
    关键输入：SQLite 文件路径。
    关键输出：Record DTO 和稳定错误。
    """

    def __init__(self, db_path: str | Path) -> None:
        """初始化 Repository。

        关键输入：SQLite 数据库路径，父目录可缺失。
        关键输出：可直接使用的 repository 实例。
        """
        self._db_path = Path(db_path)
        self._init_schema()

    @property
    def db_path(self) -> Path:
        """返回 SQLite 路径。

        关键输入：repository 实例。
        关键输出：数据库文件路径。
        """
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        """打开短生命周期 SQLite 连接。

        关键输入：repository 的数据库路径。
        关键输出：启用 Row factory 的连接。
        """
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        """初始化 SQLite schema。

        关键输入：数据库路径。
        关键输出：四张表、索引和 user_version。
        """
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pairing_sessions (
                    pairing_id TEXT PRIMARY KEY,
                    protocol_version TEXT NOT NULL,
                    client TEXT NOT NULL,
                    nonce_hash TEXT NOT NULL,
                    server_origin TEXT NOT NULL,
                    requested_scopes TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    approved_at TEXT,
                    denied_at TEXT
                );

                CREATE TABLE IF NOT EXISTS pairing_claims (
                    claim_id TEXT PRIMARY KEY,
                    pairing_id TEXT NOT NULL UNIQUE,
                    device_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    app_version TEXT NOT NULL,
                    capabilities TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(pairing_id) REFERENCES pairing_sessions(pairing_id)
                );

                CREATE TABLE IF NOT EXISTS mobile_devices (
                    device_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    app_version TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT,
                    revoked_at TEXT
                );

                CREATE TABLE IF NOT EXISTS handoff_tokens (
                    handoff_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    device_id TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(device_id) REFERENCES mobile_devices(device_id)
                );

                CREATE INDEX IF NOT EXISTS idx_mobile_devices_token_hash
                    ON mobile_devices(token_hash);
                CREATE INDEX IF NOT EXISTS idx_handoff_tokens_token_hash
                    ON handoff_tokens(token_hash);
                """
            )
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def schema_version(self) -> int:
        """读取当前 schema 版本。

        关键输入：数据库连接。
        关键输出：``PRAGMA user_version`` 整数。
        """
        with self._connect() as conn:
            row = conn.execute("PRAGMA user_version").fetchone()
            return int(row[0])

    def create_pairing_session(self, record: PairingSessionRecord) -> PairingSessionRecord:
        """插入配对会话。

        关键输入：完整 ``PairingSessionRecord``。
        关键输出：同一 record；数据库新增 pending session。
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pairing_sessions (
                    pairing_id, protocol_version, client, nonce_hash, server_origin,
                    requested_scopes, status, expires_at, created_at, approved_at, denied_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.pairing_id,
                    record.protocol_version,
                    record.client,
                    record.nonce_hash,
                    record.server_origin,
                    _json_dump(record.requested_scopes),
                    record.status.value,
                    _to_iso(record.expires_at),
                    _to_iso(record.created_at),
                    _to_iso(record.approved_at),
                    _to_iso(record.denied_at),
                ),
            )
        return record

    def get_pairing_session(self, pairing_id: str) -> PairingSessionRecord | None:
        """读取配对会话。

        关键输入：``pairing_id``。
        关键输出：会话记录或 ``None``。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pairing_sessions WHERE pairing_id = ?",
                (pairing_id,),
            ).fetchone()
        return _session_from_row(row) if row else None

    def get_claim(self, claim_id: str) -> PairingClaimRecord | None:
        """读取 claim。

        关键输入：``claim_id``。
        关键输出：claim 记录或 ``None``。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pairing_claims WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
        return _claim_from_row(row) if row else None

    def get_claim_for_pairing(self, pairing_id: str) -> PairingClaimRecord | None:
        """按 pairing 读取 claim。

        关键输入：``pairing_id``。
        关键输出：claim 记录或 ``None``。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pairing_claims WHERE pairing_id = ?",
                (pairing_id,),
            ).fetchone()
        return _claim_from_row(row) if row else None

    def claim_if_open(
        self,
        claim: PairingClaimRecord,
        *,
        now: datetime | None = None,
    ) -> PairingClaimRecord:
        """原子 claim 一个 pending_scan 会话。

        关键输入：待插入 claim 和当前时间。
        关键输出：成功插入的 claim；失败时抛稳定错误。
        """
        now = now or _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM pairing_sessions WHERE pairing_id = ?",
                (claim.pairing_id,),
            ).fetchone()
            if row is None:
                raise errors.pairing_not_found(claim.pairing_id)
            session = _session_from_row(row)
            if session.expires_at <= now:
                conn.execute(
                    "UPDATE pairing_sessions SET status = ? WHERE pairing_id = ?",
                    (PairingSessionStatus.EXPIRED.value, claim.pairing_id),
                )
                conn.commit()
                raise errors.pairing_expired(claim.pairing_id)
            if session.status != PairingSessionStatus.PENDING_SCAN:
                raise errors.pairing_already_claimed(claim.pairing_id)

            conn.execute(
                """
                INSERT INTO pairing_claims (
                    claim_id, pairing_id, device_id, label, platform, app_version,
                    capabilities, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim.claim_id,
                    claim.pairing_id,
                    claim.device_id,
                    claim.label,
                    claim.platform,
                    claim.app_version,
                    _json_dump(claim.capabilities),
                    claim.status.value,
                    _to_iso(claim.created_at),
                ),
            )
            conn.execute(
                "UPDATE pairing_sessions SET status = ? WHERE pairing_id = ?",
                (PairingSessionStatus.PENDING_APPROVAL.value, claim.pairing_id),
            )
            conn.commit()
        return claim

    def approve_claim(
        self,
        pairing_id: str,
        claim_id: str,
        *,
        approved: bool,
        now: datetime | None = None,
    ) -> tuple[PairingSessionRecord, PairingClaimRecord]:
        """原子批准或拒绝 claim。

        关键输入：pairing ID、claim ID、批准布尔值和当前时间。
        关键输出：更新后的 session 与 claim。
        """
        now = now or _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session_row = conn.execute(
                "SELECT * FROM pairing_sessions WHERE pairing_id = ?",
                (pairing_id,),
            ).fetchone()
            if session_row is None:
                raise errors.pairing_not_found(pairing_id)
            session = _session_from_row(session_row)
            if session.expires_at <= now:
                conn.execute(
                    "UPDATE pairing_sessions SET status = ? WHERE pairing_id = ?",
                    (PairingSessionStatus.EXPIRED.value, pairing_id),
                )
                conn.commit()
                raise errors.pairing_expired(pairing_id)

            claim_row = conn.execute(
                "SELECT * FROM pairing_claims WHERE pairing_id = ? AND claim_id = ?",
                (pairing_id, claim_id),
            ).fetchone()
            if claim_row is None:
                raise errors.claim_not_found(pairing_id, claim_id)
            claim = _claim_from_row(claim_row)
            if claim.status != PairingClaimStatus.PENDING_APPROVAL:
                if claim.status == PairingClaimStatus.DENIED:
                    raise errors.approval_denied(pairing_id)
                raise errors.pairing_already_claimed(pairing_id)

            if approved:
                session_status = PairingSessionStatus.APPROVED
                claim_status = PairingClaimStatus.APPROVED
                approved_at = _to_iso(now)
                denied_at = None
            else:
                session_status = PairingSessionStatus.DENIED
                claim_status = PairingClaimStatus.DENIED
                approved_at = None
                denied_at = _to_iso(now)

            conn.execute(
                """
                UPDATE pairing_sessions
                   SET status = ?, approved_at = ?, denied_at = ?
                 WHERE pairing_id = ?
                """,
                (session_status.value, approved_at, denied_at, pairing_id),
            )
            conn.execute(
                "UPDATE pairing_claims SET status = ? WHERE claim_id = ?",
                (claim_status.value, claim_id),
            )
            updated_session = _session_from_row(
                conn.execute(
                    "SELECT * FROM pairing_sessions WHERE pairing_id = ?",
                    (pairing_id,),
                ).fetchone()
            )
            updated_claim = _claim_from_row(
                conn.execute(
                    "SELECT * FROM pairing_claims WHERE claim_id = ?",
                    (claim_id,),
                ).fetchone()
            )
            conn.commit()
        return updated_session, updated_claim

    def mark_pairing_expired(self, pairing_id: str) -> PairingSessionRecord:
        """把非终态 pairing 持久化为 expired。

        关键输入：pairing ID。
        关键输出：更新后的 session；终态 session 保持原状态。
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM pairing_sessions WHERE pairing_id = ?",
                (pairing_id,),
            ).fetchone()
            if row is None:
                raise errors.pairing_not_found(pairing_id)
            session = _session_from_row(row)
            if session.status not in {
                PairingSessionStatus.DENIED,
                PairingSessionStatus.EXCHANGED,
                PairingSessionStatus.EXPIRED,
            }:
                conn.execute(
                    "UPDATE pairing_sessions SET status = ? WHERE pairing_id = ?",
                    (PairingSessionStatus.EXPIRED.value, pairing_id),
                )
                row = conn.execute(
                    "SELECT * FROM pairing_sessions WHERE pairing_id = ?",
                    (pairing_id,),
                ).fetchone()
                session = _session_from_row(row)
            conn.commit()
        return session

    def mark_exchange_complete(self, pairing_id: str, claim_id: str) -> None:
        """原子标记 exchange 完成。

        关键输入：pairing ID 和 claim ID。
        关键输出：session 与 claim 均推进为 exchanged。
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session_row = conn.execute(
                "SELECT status FROM pairing_sessions WHERE pairing_id = ?",
                (pairing_id,),
            ).fetchone()
            if session_row is None:
                raise errors.pairing_not_found(pairing_id)
            if str(session_row["status"]) != PairingSessionStatus.APPROVED.value:
                raise errors.approval_pending(pairing_id)
            claim_row = conn.execute(
                "SELECT status FROM pairing_claims WHERE pairing_id = ? AND claim_id = ?",
                (pairing_id, claim_id),
            ).fetchone()
            if claim_row is None:
                raise errors.claim_not_found(pairing_id, claim_id)
            if str(claim_row["status"]) != PairingClaimStatus.APPROVED.value:
                raise errors.approval_pending(pairing_id)
            conn.execute(
                "UPDATE pairing_sessions SET status = ? WHERE pairing_id = ?",
                (PairingSessionStatus.EXCHANGED.value, pairing_id),
            )
            cursor = conn.execute(
                """
                UPDATE pairing_claims
                   SET status = ?
                 WHERE pairing_id = ? AND claim_id = ? AND status = ?
                """,
                (
                    PairingClaimStatus.EXCHANGED.value,
                    pairing_id,
                    claim_id,
                    PairingClaimStatus.APPROVED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise errors.approval_pending(pairing_id)
            conn.commit()

    def complete_exchange(
        self,
        *,
        pairing_id: str,
        claim_id: str,
        device: MobileDeviceRecord,
        handoff: HandoffTokenRecord,
    ) -> tuple[MobileDeviceRecord, HandoffTokenRecord]:
        """原子完成 exchange 并写入 token 相关记录。

        关键输入：pairing ID、claim ID、设备 record 和 handoff record。
        关键输出：同一事务内写入后的 device/handoff 记录。
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session_row = conn.execute(
                "SELECT status FROM pairing_sessions WHERE pairing_id = ?",
                (pairing_id,),
            ).fetchone()
            if session_row is None:
                raise errors.pairing_not_found(pairing_id)
            if str(session_row["status"]) != PairingSessionStatus.APPROVED.value:
                raise errors.approval_pending(pairing_id)

            claim_row = conn.execute(
                """
                SELECT status, device_id
                  FROM pairing_claims
                 WHERE pairing_id = ? AND claim_id = ?
                """,
                (pairing_id, claim_id),
            ).fetchone()
            if claim_row is None:
                raise errors.claim_not_found(pairing_id, claim_id)
            if str(claim_row["device_id"]) != device.device_id:
                raise errors.claim_not_found(pairing_id, claim_id)
            if str(claim_row["status"]) != PairingClaimStatus.APPROVED.value:
                raise errors.approval_pending(pairing_id)

            _upsert_device(conn, device)
            _insert_handoff_token(conn, handoff)
            conn.execute(
                "UPDATE pairing_sessions SET status = ? WHERE pairing_id = ?",
                (PairingSessionStatus.EXCHANGED.value, pairing_id),
            )
            cursor = conn.execute(
                """
                UPDATE pairing_claims
                   SET status = ?
                 WHERE pairing_id = ? AND claim_id = ? AND status = ?
                """,
                (
                    PairingClaimStatus.EXCHANGED.value,
                    pairing_id,
                    claim_id,
                    PairingClaimStatus.APPROVED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise errors.approval_pending(pairing_id)

            stored_device = _device_from_row(
                conn.execute(
                    "SELECT * FROM mobile_devices WHERE device_id = ?",
                    (device.device_id,),
                ).fetchone()
            )
            stored_handoff = _handoff_from_row(
                conn.execute(
                    "SELECT * FROM handoff_tokens WHERE handoff_id = ?",
                    (handoff.handoff_id,),
                ).fetchone()
            )
            conn.commit()
        return stored_device, stored_handoff

    def upsert_device(self, record: MobileDeviceRecord) -> MobileDeviceRecord:
        """插入或更新移动设备。

        关键输入：包含 token hash 的设备记录。
        关键输出：持久化后的设备记录。
        """
        with self._connect() as conn:
            _upsert_device(conn, record)
        return self.get_device(record.device_id) or record

    def get_device(self, device_id: str) -> MobileDeviceRecord | None:
        """按设备 ID 读取设备。

        关键输入：``device_id``。
        关键输出：设备记录或 ``None``。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM mobile_devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        return _device_from_row(row) if row else None

    def get_device_by_token_hash(self, token_hash: str) -> MobileDeviceRecord | None:
        """按 device token hash 读取设备。

        关键输入：SHA-256 token hash。
        关键输出：设备记录或 ``None``。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM mobile_devices WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        return _device_from_row(row) if row else None

    def list_devices(self, *, include_revoked: bool = False) -> list[MobileDeviceRecord]:
        """列出移动设备。

        关键输入：是否包含已吊销设备。
        关键输出：按创建时间倒序排列的设备记录列表。
        """
        sql = "SELECT * FROM mobile_devices"
        params: tuple[()] = ()
        if not include_revoked:
            sql += " WHERE revoked_at IS NULL"
        sql += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_device_from_row(row) for row in rows]

    def touch_device(self, device_id: str, *, now: datetime | None = None) -> None:
        """更新设备最近使用时间。

        关键输入：设备 ID 和当前时间。
        关键输出：``last_seen_at`` 更新。
        """
        now = now or _utc_now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE mobile_devices SET last_seen_at = ? WHERE device_id = ?",
                (_to_iso(now), device_id),
            )

    def revoke_device(self, device_id: str, *, now: datetime | None = None) -> MobileDeviceRecord:
        """吊销移动设备。

        关键输入：设备 ID 和当前时间。
        关键输出：吊销后的设备记录。
        """
        now = now or _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM mobile_devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if row is None:
                raise errors.invalid_token(f"device not found: {device_id}")
            conn.execute(
                "UPDATE mobile_devices SET revoked_at = ? WHERE device_id = ?",
                (_to_iso(now), device_id),
            )
            updated = _device_from_row(
                conn.execute(
                    "SELECT * FROM mobile_devices WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
            )
            conn.commit()
        return updated

    def create_handoff_token(self, record: HandoffTokenRecord) -> HandoffTokenRecord:
        """插入 handoff token 记录。

        关键输入：只包含 token hash 的 handoff record。
        关键输出：同一 record；数据库新增可消费 handoff。
        """
        with self._connect() as conn:
            _insert_handoff_token(conn, record)
        return record

    def get_handoff_by_token_hash(self, token_hash: str) -> HandoffTokenRecord | None:
        """按 handoff token hash 读取记录。

        关键输入：SHA-256 token hash。
        关键输出：handoff 记录或 ``None``。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM handoff_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        return _handoff_from_row(row) if row else None

    def consume_handoff_by_hash(
        self,
        token_hash: str,
        *,
        now: datetime | None = None,
    ) -> HandoffLoginContext:
        """原子消费 handoff token。

        关键输入：handoff token hash 和当前时间。
        关键输出：登录上下文；失败时抛 handoff/token 错误。
        """
        now = now or _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            handoff_row = conn.execute(
                "SELECT * FROM handoff_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if handoff_row is None:
                raise errors.invalid_token("invalid handoff token")
            handoff = _handoff_from_row(handoff_row)
            if handoff.consumed_at is not None:
                raise errors.handoff_consumed(handoff.handoff_id)
            if handoff.expires_at <= now:
                raise errors.handoff_expired(handoff.handoff_id)

            device_row = conn.execute(
                "SELECT * FROM mobile_devices WHERE device_id = ?",
                (handoff.device_id,),
            ).fetchone()
            if device_row is None:
                raise errors.invalid_token("handoff device not found")
            device = _device_from_row(device_row)
            if device.revoked_at is not None:
                raise errors.device_revoked(device.device_id)

            cursor = conn.execute(
                """
                UPDATE handoff_tokens
                   SET consumed_at = ?
                 WHERE handoff_id = ? AND consumed_at IS NULL
                """,
                (_to_iso(now), handoff.handoff_id),
            )
            if cursor.rowcount != 1:
                raise errors.handoff_consumed(handoff.handoff_id)
            conn.execute(
                "UPDATE mobile_devices SET last_seen_at = ? WHERE device_id = ?",
                (_to_iso(now), device.device_id),
            )
            conn.commit()
        return HandoffLoginContext(
            device_id=handoff.device_id,
            scopes=handoff.scopes,
            user_id=handoff.user_id,
        )


__all__ = ["MobilePairingRepository"]
