"""Avatar 消息 SQLite 持久化门户。

本脚本实现 AvatarMessageRepository，作用是封装 avatar_messages SQLite
schema、消息 upsert、cursor 查询、TTL 过期和 ack 幂等。关键流程是
Manager 调用 register_message/list_messages/ack_message，Repository 通过
短连接和 BEGIN IMMEDIATE 维护 sequence、revision、dedupe 和状态一致性。
关键函数职责：序列化函数负责 DTO 与 SQLite 行互转，事务方法负责状态推进。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import errors
from .models import (
    AvatarAckRequest,
    AvatarMessageInput,
    AvatarMessageListQuery,
    AvatarMessageListResult,
    AvatarMessageSnapshot,
    AvatarMessageStatus,
)

_SCHEMA_VERSION = 1
_MESSAGE_ID_PREFIX = "am_"
_MAX_FILTER_VALUES = 50
_MAX_SOURCE_FILTER_LENGTH = 80
_MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807


def _utc_now() -> datetime:
    """生成当前 UTC 时间。"""
    return datetime.now(UTC)


def _to_iso(value: datetime | None) -> str | None:
    """把 datetime 转换为 SQLite ISO 字符串。"""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _from_iso(value: str | None) -> datetime | None:
    """把 SQLite ISO 字符串转换为 UTC datetime。"""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _json_dump(value: Any) -> str:
    """把 JSON 可序列化对象转换为紧凑 JSON 字符串。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_load(raw: str | None) -> Any:
    """把 SQLite JSON 字符串转换为 Python 对象。"""
    if raw is None:
        return {}
    return json.loads(raw)


def _new_message_id() -> str:
    """生成 Avatar 消息 ID。"""
    return f"{_MESSAGE_ID_PREFIX}{secrets.token_urlsafe(18)}"


def _parse_cursor(cursor: str | None) -> int:
    """解析 REST cursor。

    关键输入：cursor 字符串或空值。
    关键输出：上一页最后 sequence；非法时抛稳定错误。
    """
    if cursor is None or cursor == "":
        return 0
    try:
        value = int(cursor)
    except ValueError as exc:
        raise errors.invalid_cursor(cursor) from exc
    if value < 0:
        raise errors.invalid_cursor(cursor)
    if value > _MAX_SQLITE_INTEGER:
        raise errors.invalid_cursor(cursor)
    return value


def _input_to_payload(message: AvatarMessageInput) -> dict[str, Any]:
    """把 AvatarMessageInput 转成 JSON payload dict。"""
    return message.model_dump(mode="json")


def _validate_filter_values(query: AvatarMessageListQuery) -> None:
    """校验列表过滤条件，避免超大 IN 查询撞 SQLite 参数上限。

    关键输入：AvatarMessageListQuery。
    关键输出：合法过滤通过；非法时抛 avatar_invalid_filter。
    """
    for name, values in (
        ("level", query.level),
        ("source", query.source),
        ("status", query.status),
    ):
        if values is not None and len(values) > _MAX_FILTER_VALUES:
            raise errors.invalid_filter(
                f"avatar filter {name} accepts at most {_MAX_FILTER_VALUES} values"
            )
    if query.source:
        for source in query.source:
            if not source or len(source) > _MAX_SOURCE_FILTER_LENGTH:
                raise errors.invalid_filter(
                    f"avatar filter source values must be 1-{_MAX_SOURCE_FILTER_LENGTH} chars"
                )


def _snapshot_from_row(row: sqlite3.Row) -> AvatarMessageSnapshot:
    """把 avatar_messages 行转换为 AvatarMessageSnapshot。"""
    message_id = str(row["message_id"])
    try:
        payload = _json_load(row["payload_json"])
        message_input = AvatarMessageInput.model_validate(payload)
    except json.JSONDecodeError as exc:
        raise errors.invalid_request(
            f"invalid avatar message JSON: message_id={message_id} field=payload_json"
        ) from exc
    except ValidationError as exc:
        raise errors.invalid_request(
            f"invalid avatar message payload: message_id={message_id} field=payload_json"
        ) from exc
    return AvatarMessageSnapshot(
        message_id=message_id,
        sequence=int(row["sequence"]),
        revision=int(row["revision"]),
        status=AvatarMessageStatus(str(row["status"])),
        input=message_input,
        created_at=_from_iso(row["created_at"]) or _utc_now(),
        updated_at=_from_iso(row["updated_at"]) or _utc_now(),
        acked_at=_from_iso(row["acked_at"]),
        consumed_at=_from_iso(row["consumed_at"]),
    )


class AvatarMessageRepository:
    """Avatar 消息 SQLite Repository。

    职责：初始化 schema、隐藏 SQL 细节、提供消息注册、查询和 ack 事务。
    关键输入：SQLite 数据库路径。
    关键输出：AvatarMessageSnapshot 和分页结果。
    """

    def __init__(self, db_path: str | Path) -> None:
        """初始化 Repository。

        关键输入：SQLite 数据库路径。
        关键输出：可直接使用的 Repository 实例。
        """
        self._db_path = Path(db_path)
        self._init_schema()

    @property
    def db_path(self) -> Path:
        """返回 SQLite 数据库路径。"""
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        """打开短生命周期 SQLite 连接。"""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        """初始化 SQLite schema。"""
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS avatar_messages (
                    message_id TEXT PRIMARY KEY,
                    sequence INTEGER NOT NULL UNIQUE,
                    revision INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    level TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    thread_id TEXT,
                    run_id TEXT,
                    request_id TEXT,
                    action_json TEXT,
                    dedupe_key TEXT,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    expires_at TEXT,
                    acked_at TEXT,
                    consumed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_avatar_messages_source_dedupe
                    ON avatar_messages(source, dedupe_key);
                CREATE INDEX IF NOT EXISTS idx_avatar_messages_sequence
                    ON avatar_messages(sequence);
                CREATE INDEX IF NOT EXISTS idx_avatar_messages_status
                    ON avatar_messages(status);
                CREATE INDEX IF NOT EXISTS idx_avatar_messages_source
                    ON avatar_messages(source);
                CREATE INDEX IF NOT EXISTS idx_avatar_messages_thread_id
                    ON avatar_messages(thread_id);
                CREATE INDEX IF NOT EXISTS idx_avatar_messages_updated_at
                    ON avatar_messages(updated_at);
                CREATE INDEX IF NOT EXISTS idx_avatar_messages_expires_at
                    ON avatar_messages(expires_at);
                """
            )
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def schema_version(self) -> int:
        """读取 schema 版本。"""
        with self._connect() as conn:
            row = conn.execute("PRAGMA user_version").fetchone()
            return int(row[0])

    def register_message(
        self,
        message: AvatarMessageInput,
        *,
        now: datetime | None = None,
    ) -> AvatarMessageSnapshot:
        """注册或更新 Avatar 消息。

        关键输入：AvatarMessageInput 和可选当前时间。
        关键输出：新建或 dedupe 更新后的消息快照。
        """
        now = now or _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._mark_expired_locked(conn, now=now)
            if message.dedupe_key:
                existing = conn.execute(
                    """
                    SELECT * FROM avatar_messages
                     WHERE source = ? AND dedupe_key = ?
                    """,
                    (message.source, message.dedupe_key),
                ).fetchone()
                if existing is not None:
                    snapshot = self._update_existing_locked(
                        conn,
                        existing=existing,
                        message=message,
                        now=now,
                    )
                    conn.commit()
                    return snapshot

            snapshot = self._insert_new_locked(conn, message=message, now=now)
            conn.commit()
            return snapshot

    def list_messages(
        self,
        query: AvatarMessageListQuery,
        *,
        now: datetime | None = None,
    ) -> AvatarMessageListResult:
        """按查询条件列出 Avatar 消息。

        关键输入：AvatarMessageListQuery 和可选当前时间。
        关键输出：分页列表结果。
        """
        now = now or _utc_now()
        _validate_filter_values(query)
        cursor_value = _parse_cursor(query.cursor)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._mark_expired_locked(conn, now=now)
            rows = conn.execute(
                *self._build_list_sql(query, cursor_value=cursor_value),
            ).fetchall()
            conn.commit()

        snapshots = [_snapshot_from_row(row) for row in rows]
        next_cursor = str(snapshots[-1].sequence) if len(snapshots) == query.limit else None
        return AvatarMessageListResult(
            items=snapshots,
            next_cursor=next_cursor,
            server_time=now,
        )

    def ack_message(
        self,
        message_id: str,
        request: AvatarAckRequest,
        *,
        now: datetime | None = None,
    ) -> AvatarMessageSnapshot:
        """幂等确认或消费单条 Avatar 消息。

        关键输入：消息 ID、ack 请求和可选当前时间。
        关键输出：更新后的消息快照。
        """
        now = now or _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._mark_expired_locked(conn, now=now)
            row = conn.execute(
                "SELECT * FROM avatar_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if row is None:
                raise errors.message_not_found(message_id)
            _snapshot_from_row(row)

            acked_at = row["acked_at"]
            consumed_at = row["consumed_at"]
            status = str(row["status"])
            if request.status.value == AvatarMessageStatus.CONSUMED.value:
                acked_at = acked_at or _to_iso(now)
                consumed_at = consumed_at or _to_iso(now)
                status = AvatarMessageStatus.CONSUMED.value
            elif status != AvatarMessageStatus.CONSUMED.value:
                acked_at = acked_at or _to_iso(now)
                status = AvatarMessageStatus.ACKED.value

            conn.execute(
                """
                UPDATE avatar_messages
                   SET status = ?, acked_at = ?, consumed_at = ?, updated_at = ?
                 WHERE message_id = ?
                """,
                (status, acked_at, consumed_at, _to_iso(now), message_id),
            )
            updated = conn.execute(
                "SELECT * FROM avatar_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            snapshot = _snapshot_from_row(updated)
            conn.commit()
        return snapshot

    def _next_sequence_locked(self, conn: sqlite3.Connection) -> int:
        """在事务内生成下一条 sequence。"""
        row = conn.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM avatar_messages").fetchone()
        return int(row[0])

    def _insert_new_locked(
        self,
        conn: sqlite3.Connection,
        *,
        message: AvatarMessageInput,
        now: datetime,
    ) -> AvatarMessageSnapshot:
        """在事务内插入新消息。"""
        message_id = _new_message_id()
        sequence = self._next_sequence_locked(conn)
        payload = _input_to_payload(message)
        conn.execute(
            """
            INSERT INTO avatar_messages (
                message_id, sequence, revision, source, level, priority,
                thread_id, run_id, request_id, action_json, dedupe_key, status,
                payload_json, expires_at, acked_at, consumed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                sequence,
                1,
                message.source,
                message.level.value,
                message.priority,
                message.thread_id,
                message.run_id,
                message.request_id,
                _json_dump(message.action.model_dump(mode="json")) if message.action else None,
                message.dedupe_key,
                AvatarMessageStatus.ACTIVE.value,
                _json_dump(payload),
                _to_iso(message.expires_at),
                None,
                None,
                _to_iso(now),
                _to_iso(now),
            ),
        )
        row = conn.execute(
            "SELECT * FROM avatar_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        return _snapshot_from_row(row)

    def _update_existing_locked(
        self,
        conn: sqlite3.Connection,
        *,
        existing: sqlite3.Row,
        message: AvatarMessageInput,
        now: datetime,
    ) -> AvatarMessageSnapshot:
        """在事务内更新 dedupe 命中的消息。"""
        sequence = self._next_sequence_locked(conn)
        status = str(existing["status"])
        if status == AvatarMessageStatus.EXPIRED.value:
            status = AvatarMessageStatus.ACTIVE.value
        payload = _input_to_payload(message)
        conn.execute(
            """
            UPDATE avatar_messages
               SET sequence = ?,
                   revision = revision + 1,
                   level = ?,
                   priority = ?,
                   thread_id = ?,
                   run_id = ?,
                   request_id = ?,
                   action_json = ?,
                   status = ?,
                   payload_json = ?,
                   expires_at = ?,
                   updated_at = ?
             WHERE message_id = ?
            """,
            (
                sequence,
                message.level.value,
                message.priority,
                message.thread_id,
                message.run_id,
                message.request_id,
                _json_dump(message.action.model_dump(mode="json")) if message.action else None,
                status,
                _json_dump(payload),
                _to_iso(message.expires_at),
                _to_iso(now),
                str(existing["message_id"]),
            ),
        )
        row = conn.execute(
            "SELECT * FROM avatar_messages WHERE message_id = ?",
            (str(existing["message_id"]),),
        ).fetchone()
        return _snapshot_from_row(row)

    def _mark_expired_locked(self, conn: sqlite3.Connection, *, now: datetime) -> None:
        """在事务内把已过期消息标记为 expired。"""
        conn.execute(
            """
            UPDATE avatar_messages
               SET status = ?, updated_at = ?
             WHERE expires_at IS NOT NULL
               AND expires_at <= ?
               AND status IN (?, ?)
            """,
            (
                AvatarMessageStatus.EXPIRED.value,
                _to_iso(now),
                _to_iso(now),
                AvatarMessageStatus.ACTIVE.value,
                AvatarMessageStatus.ACKED.value,
            ),
        )

    def _build_list_sql(
        self,
        query: AvatarMessageListQuery,
        *,
        cursor_value: int,
    ) -> tuple[str, tuple[Any, ...]]:
        """构造消息列表查询 SQL 和参数。"""
        clauses = ["sequence > ?"]
        values: list[Any] = [cursor_value]
        statuses = query.status or [AvatarMessageStatus.ACTIVE]
        clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
        values.extend(status.value for status in statuses)
        if query.since is not None:
            clauses.append("updated_at >= ?")
            values.append(_to_iso(query.since))
        if query.level:
            clauses.append(f"level IN ({','.join('?' for _ in query.level)})")
            values.extend(level.value for level in query.level)
        if query.source:
            clauses.append(f"source IN ({','.join('?' for _ in query.source)})")
            values.extend(query.source)
        if query.thread_id:
            clauses.append("thread_id = ?")
            values.append(query.thread_id)

        sql = f"""
            SELECT *
              FROM avatar_messages
             WHERE {" AND ".join(clauses)}
             ORDER BY sequence ASC
             LIMIT ?
        """
        values.append(query.limit)
        return sql, tuple(values)


__all__ = ["AvatarMessageRepository"]
