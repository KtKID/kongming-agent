from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sessions.session_discovery import (
    discover_file_sessions,
    discover_sqlite_sessions,
    find_session_by_id,
    most_recent_session,
)


def test_discover_file_sessions_returns_sorted_summaries(tmp_path: Path) -> None:
    _write_file_session(
        tmp_path,
        "older",
        [
            _record("system", "sys", created_at=10.0),
            _record("user", "first question", created_at=20.0),
        ],
    )
    _write_file_session(
        tmp_path,
        "newer",
        [
            _record("system", "sys", created_at=30.0),
            _record("assistant", "latest answer", created_at=35.0),
            _record("user", "latest question", created_at=40.0),
        ],
    )

    summaries = discover_file_sessions(tmp_path)

    assert [item.session_id for item in summaries] == ["newer", "older"]
    assert summaries[0].preview == "latest qu…"
    assert summaries[0].last_role == "user"
    assert summaries[0].message_count == 3


def test_discover_file_sessions_uses_last_user_preview_and_skips_broken_line(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "tooly"
    session_dir.mkdir(parents=True)
    messages_path = session_dir / "tooly.jsonl"
    messages_path.write_text(
        "\n".join(
            [
                json.dumps(_record("system", "sys", created_at=10.0)),
                "{broken json",
                json.dumps(_record("assistant", "ignored answer", created_at=20.0)),
                json.dumps(_record("user", "valid question", created_at=30.0)),
            ]
        ),
        encoding="utf-8",
    )

    summaries = discover_file_sessions(tmp_path)

    assert len(summaries) == 1
    assert summaries[0].preview == "valid que…"
    assert summaries[0].last_role == "user"
    assert summaries[0].message_count == 3


def test_discover_sqlite_sessions_returns_sorted_summaries(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE messages (
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY(session_id, seq)
            )
            """
        )
        conn.execute(
            "INSERT INTO messages (session_id, seq, payload, created_at) VALUES (?, ?, ?, ?)",
            ("older", 1, _sqlite_payload("user", "older question"), 100.0),
        )
        conn.execute(
            "INSERT INTO messages (session_id, seq, payload, created_at) VALUES (?, ?, ?, ?)",
            ("newer", 1, _sqlite_payload("assistant", "ignored answer"), 150.0),
        )
        conn.execute(
            "INSERT INTO messages (session_id, seq, payload, created_at) VALUES (?, ?, ?, ?)",
            ("newer", 2, _sqlite_payload("user", "new question"), 200.0),
        )
        conn.commit()

    summaries = discover_sqlite_sessions(db_path)

    assert [item.session_id for item in summaries] == ["newer", "older"]
    assert summaries[0].preview == "new quest…"
    assert summaries[0].backend == "sqlite"


def test_find_session_by_id_and_most_recent(tmp_path: Path) -> None:
    _write_file_session(
        tmp_path,
        "alpha",
        [_record("user", "A", created_at=10.0)],
    )
    _write_file_session(
        tmp_path,
        "beta",
        [_record("user", "B", created_at=20.0)],
    )

    summaries = discover_file_sessions(tmp_path)

    assert most_recent_session(summaries).session_id == "beta"
    assert find_session_by_id(summaries, "alpha").preview == "A"
    assert find_session_by_id(summaries, "missing") is None


def _write_file_session(root: Path, session_id: str, records: list[dict]) -> None:
    session_dir = root / session_id
    session_dir.mkdir(parents=True)
    (session_dir / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records),
        encoding="utf-8",
    )


def _record(role: str, content: str | None, *, created_at: float) -> dict:
    return {
        "created_at": created_at,
        "message": {
            "role": role,
            "content": content,
        },
    }


def _sqlite_payload(role: str, content: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "message": {
                "role": role,
                "content": content,
            },
        }
    )
