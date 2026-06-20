"""启动前 session 发现能力。

用于 CLI 在 runtime 装配前做三类工作：

1. 列出可恢复的持久化 session
2. 选择最近活跃 session
3. 校验显式传入的 session_id 是否存在

当前支持两类持久化后端：

- file: ``<root>/<session_id>/<session_id>.jsonl``
- sqlite: ``messages`` 表，按 ``created_at`` 聚合
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_SUMMARY_MAX_CHARS = 10


@dataclass(frozen=True)
class SessionSummary:
    """启动前展示用的 session 摘要。"""

    session_id: str
    updated_at: float
    last_role: str | None
    preview: str
    message_count: int
    backend: Literal["file", "sqlite"]


def discover_file_sessions(root: str | Path) -> list[SessionSummary]:
    """扫描 file backend 的 session 根目录。"""
    root_path = Path(root)
    if not root_path.is_dir():
        return []

    summaries: list[SessionSummary] = []
    for session_dir in root_path.iterdir():
        if not session_dir.is_dir():
            continue
        summary = _read_file_session_summary(session_dir)
        if summary is not None:
            summaries.append(summary)
    return sorted(summaries, key=lambda item: item.updated_at, reverse=True)


def discover_sqlite_sessions(db_path: str | Path) -> list[SessionSummary]:
    """扫描 sqlite backend 的 session 列表。"""
    path = Path(db_path)
    if not path.is_file():
        return []

    with sqlite3.connect(str(path)) as conn:
        try:
            rows = conn.execute(
                """
                SELECT session_id, MAX(created_at) AS updated_at, COUNT(*) AS message_count
                FROM messages
                GROUP BY session_id
                ORDER BY updated_at DESC
                """
            ).fetchall()
        except sqlite3.Error:
            return []

        summaries: list[SessionSummary] = []
        for session_id, updated_at, message_count in rows:
            last_payload_row = conn.execute(
                """
                SELECT payload
                FROM messages
                WHERE session_id = ?
                ORDER BY seq DESC
                LIMIT 1
                """,
                (str(session_id),),
            ).fetchone()
            preview, last_role = _payload_preview(last_payload_row[0] if last_payload_row else None)
            summaries.append(
                SessionSummary(
                    session_id=str(session_id),
                    updated_at=float(updated_at or 0.0),
                    last_role=last_role,
                    preview=preview,
                    message_count=int(message_count or 0),
                    backend="sqlite",
                )
            )
        return summaries


def most_recent_session(summaries: list[SessionSummary]) -> SessionSummary | None:
    """返回最近活跃 session。"""
    if not summaries:
        return None
    return summaries[0]


def find_session_by_id(
    summaries: list[SessionSummary],
    session_id: str,
) -> SessionSummary | None:
    """按 session_id 查找已有 session。"""
    for summary in summaries:
        if summary.session_id == session_id:
            return summary
    return None


def _read_file_session_summary(session_dir: Path) -> SessionSummary | None:
    session_id = session_dir.name
    messages_path = session_dir / f"{session_id}.jsonl"
    manifest_path = session_dir / "manifest.json"
    if not messages_path.is_file():
        return None

    last_timestamp = 0.0
    last_preview = ""
    last_role: str | None = None
    message_count = 0

    with messages_path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            created_at = record.get("created_at")
            if isinstance(created_at, (int, float)):
                last_timestamp = float(created_at)

            if record.get("record_type", "message") != "message":
                continue

            message_count += 1
            preview, role = _record_preview(record)
            if role == "user" and preview:
                last_preview = preview
                last_role = role

    if last_timestamp <= 0 and manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
        created_at = manifest.get("created_at")
        if isinstance(created_at, (int, float)):
            last_timestamp = float(created_at)

    return SessionSummary(
        session_id=session_id,
        updated_at=last_timestamp,
        last_role=last_role,
        preview=last_preview,
        message_count=message_count,
        backend="file",
    )


def _payload_preview(raw_payload: str | None) -> tuple[str, str | None]:
    if not raw_payload:
        return "", None
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return "", None

    if isinstance(payload, dict) and "message" in payload:
        return _message_preview(payload.get("message"))
    if isinstance(payload, dict):
        return _message_preview(payload)
    return "", None


def _record_preview(record: dict[str, Any]) -> tuple[str, str | None]:
    message = record.get("message")
    if not isinstance(message, dict):
        return "", None
    return _message_preview(message)


def _message_preview(message: Any) -> tuple[str, str | None]:
    if not isinstance(message, dict):
        return "", None

    role_raw = message.get("role")
    role = str(role_raw) if role_raw else None
    if role in ("system", "assistant", "tool"):
        return "", role

    content = message.get("content")
    if isinstance(content, str):
        stripped = content.strip()
        if stripped:
            return _truncate_preview(stripped), role

    return "", role


def _truncate_preview(text: str, max_chars: int = _SUMMARY_MAX_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


__all__ = [
    "SessionSummary",
    "discover_file_sessions",
    "discover_sqlite_sessions",
    "find_session_by_id",
    "most_recent_session",
]
