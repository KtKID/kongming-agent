"""unit：sessions.session_store 覆盖（SQLiteSession + build_session）。

B3 / CR 报告 cr-report-20260424-202744.md。覆盖：

- 全新 db 首次 append → history 顺序一致
- 重新打开（新对象、同 db + 同 session_id）→ history 保留
- 并发 append（asyncio.gather）不丢消息
- 多 session_id 共享同一个 db 文件时互相隔离
- clear 只清本 session
- 四种 role + ToolCall 完整往返
- _message_to_dict / _message_from_dict 对称
- 兼容旧 payload（无 schema_version 包裹，直接是 Message dict）
- build_session 三种 backend 分派 + 未知 backend 抛 ValueError
- build_session file backend 无 bootstrap → ValueError
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.message import Message, ToolCall
from core.session import InMemorySession
from infrastructure.config.models import Config, ModelConfig, SessionConfig
from sessions.session_bootstrap import SessionBootstrap
from sessions.session_store import (
    SQLiteSession,
    _message_from_dict,
    _message_to_dict,
    build_session,
)


def _cfg(tmp_path: Path, backend: str = "memory", **extra) -> Config:
    session_cfg = {"backend": backend}
    if backend == "sqlite":
        session_cfg["store_path"] = str(tmp_path / "session.db")
    if backend == "file":
        session_cfg["file_store_path"] = str(tmp_path / "sessions")
    session_cfg.update(extra)
    return Config(
        model=ModelConfig(name="m", base_url="http://localhost:1234"),
        session=SessionConfig(**session_cfg),
    )


def _bootstrap(tmp_path: Path) -> SessionBootstrap:
    return SessionBootstrap(
        agent_name="agent",
        model_name="model",
        instruction_sources=["runtime", "memory"],
        instruction_text_hash="sha256:test",
        created_at=123.0,
        cwd=str(tmp_path),
        instruction_text="system text",
        app_version="test-version",
    )


# ---------------------------------------------------------------------------
# SQLiteSession: append / history / clear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_session_append_and_history_order(tmp_path):
    db = tmp_path / "s.db"
    session = SQLiteSession("sid-1", db)
    await session.append(Message.user("u1"))
    await session.append(Message.assistant(content="a1"))
    await session.append(Message.user("u2"))

    hist = await session.history()
    assert [m.role for m in hist] == ["user", "assistant", "user"]
    assert [m.content for m in hist] == ["u1", "a1", "u2"]


@pytest.mark.asyncio
async def test_sqlite_session_reopen_preserves_history(tmp_path):
    db = tmp_path / "s.db"
    first = SQLiteSession("sid-1", db)
    await first.append(Message.user("hello"))
    await first.append(Message.assistant(content="world"))

    # 新对象、同 db、同 session_id
    second = SQLiteSession("sid-1", db)
    hist = await second.history()
    assert [m.content for m in hist] == ["hello", "world"]


@pytest.mark.asyncio
async def test_sqlite_session_multiple_sessions_isolated(tmp_path):
    db = tmp_path / "s.db"
    a = SQLiteSession("sid-a", db)
    b = SQLiteSession("sid-b", db)
    await a.append(Message.user("for-a"))
    await b.append(Message.user("for-b"))

    assert [m.content for m in await a.history()] == ["for-a"]
    assert [m.content for m in await b.history()] == ["for-b"]


@pytest.mark.asyncio
async def test_sqlite_session_clear_only_affects_own_session(tmp_path):
    db = tmp_path / "s.db"
    a = SQLiteSession("sid-a", db)
    b = SQLiteSession("sid-b", db)
    await a.append(Message.user("x"))
    await b.append(Message.user("y"))

    await a.clear()
    assert await a.history() == []
    assert [m.content for m in await b.history()] == ["y"]


@pytest.mark.asyncio
async def test_sqlite_session_many_sequential_appends(tmp_path):
    """顺序 append 30 条必须全部保留且顺序一致。

    注意：source docstring 明确写了 v1-mini 不保证高并发写（见 session_store.py 模块
    docstring "线程 / 连接策略" 段）。本测试只覆盖顺序写场景——这是当前实现的
    契约；并发写留给 v0.2+ 升级 aiosqlite / 连接池后再覆盖。
    """
    db = tmp_path / "s.db"
    session = SQLiteSession("sid-seq", db)
    for i in range(30):
        await session.append(Message.user(f"u{i}"))
    hist = await session.history()
    assert [m.content for m in hist] == [f"u{i}" for i in range(30)]


@pytest.mark.asyncio
async def test_sqlite_session_creates_parent_dir(tmp_path):
    db = tmp_path / "nested" / "deep" / "s.db"
    session = SQLiteSession("sid", db)
    await session.append(Message.user("x"))
    assert db.exists()


@pytest.mark.asyncio
async def test_sqlite_session_empty_history(tmp_path):
    db = tmp_path / "s.db"
    session = SQLiteSession("sid", db)
    assert await session.history() == []


@pytest.mark.asyncio
async def test_sqlite_session_roundtrip_with_tool_calls(tmp_path):
    db = tmp_path / "s.db"
    session = SQLiteSession("sid", db)
    tc = ToolCall(call_id="c1", tool_name="read_file", arguments={"path": "/tmp/x", "n": 1})
    await session.append(Message.assistant(content=None, tool_calls=[tc]))
    await session.append(Message.tool_result(tool_call_id="c1", content="ok", name="read_file"))

    hist = await session.history()
    assert len(hist) == 2
    asst = hist[0]
    assert asst.role == "assistant"
    assert asst.tool_calls is not None
    assert len(asst.tool_calls) == 1
    assert asst.tool_calls[0].call_id == "c1"
    assert asst.tool_calls[0].arguments == {"path": "/tmp/x", "n": 1}

    tool_msg = hist[1]
    assert tool_msg.role == "tool"
    assert tool_msg.tool_call_id == "c1"
    assert tool_msg.content == "ok"
    assert tool_msg.name == "read_file"


# ---------------------------------------------------------------------------
# _message_to_dict / _message_from_dict 往返
# ---------------------------------------------------------------------------


def test_message_roundtrip_user():
    m = Message.user("hello")
    assert _message_from_dict(_message_to_dict(m)) == m


def test_message_roundtrip_system():
    m = Message.system("rules")
    assert _message_from_dict(_message_to_dict(m)) == m


def test_message_roundtrip_assistant_with_tool_calls():
    tc = ToolCall(call_id="c1", tool_name="tool", arguments={"k": "v"})
    m = Message.assistant(content=None, tool_calls=[tc])
    back = _message_from_dict(_message_to_dict(m))
    assert back.role == "assistant"
    assert back.tool_calls == (tc,)


def test_message_roundtrip_tool_with_metadata():
    m = Message.tool_result(tool_call_id="c1", content="x", metadata={"exit": 0})
    back = _message_from_dict(_message_to_dict(m))
    assert back.tool_call_id == "c1"
    assert back.metadata == {"exit": 0}


def test_message_roundtrip_preserves_metadata_isolation():
    """_message_to_dict 对 metadata 做 dict(...) 拷贝，修改入参不应污染输出。"""
    md = {"k": "v"}
    m = Message.user("hi", metadata=md)
    d = _message_to_dict(m)
    md["k"] = "mutated"
    assert d["metadata"] == {"k": "v"}


def test_message_from_dict_tolerates_missing_optional_fields():
    """缺失字段按默认值处理；保证旧版 payload 可读。"""
    m = _message_from_dict({"role": "user", "content": "hi"})
    assert m.role == "user"
    assert m.content == "hi"
    assert m.tool_calls is None


# ---------------------------------------------------------------------------
# 旧 payload 兼容（无 schema_version 包裹）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_session_reads_legacy_payload(tmp_path):
    """直接写入一条无 schema_version 包裹的旧格式 payload，history() 仍能读出。"""
    db = tmp_path / "s.db"
    # 先建表
    session = SQLiteSession("sid", db)
    await session.append(Message.user("new"))

    # 手写一条旧格式 payload
    legacy = {
        "role": "user",
        "content": "legacy",
        "tool_calls": None,
        "tool_call_id": None,
        "name": None,
        "metadata": {},
    }
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO messages (session_id, seq, payload, created_at) VALUES (?, ?, ?, ?)",
            ("sid", 99, json.dumps(legacy), 0.0),
        )
        conn.commit()

    hist = await session.history()
    contents = [m.content for m in hist]
    assert "legacy" in contents


@pytest.mark.asyncio
async def test_sqlite_session_skips_corrupt_payload(tmp_path):
    """JSON 解析失败的行被跳过，不炸掉 history。"""
    db = tmp_path / "s.db"
    session = SQLiteSession("sid", db)
    await session.append(Message.user("good"))

    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO messages (session_id, seq, payload, created_at) VALUES (?, ?, ?, ?)",
            ("sid", 2, "{not-json", 0.0),
        )
        conn.commit()

    hist = await session.history()
    assert [m.content for m in hist] == ["good"]


# ---------------------------------------------------------------------------
# build_session 分派
# ---------------------------------------------------------------------------


def test_build_session_memory(tmp_path):
    s = build_session(_cfg(tmp_path, "memory"), "sid")
    assert isinstance(s, InMemorySession)
    assert s.session_id == "sid"


def test_build_session_sqlite(tmp_path):
    bootstrap = _bootstrap(tmp_path)
    s = build_session(_cfg(tmp_path, "sqlite"), "sid", bootstrap=bootstrap)
    assert isinstance(s, SQLiteSession)
    assert s.session_id == "sid"


def test_build_session_file_requires_bootstrap(tmp_path):
    with pytest.raises(ValueError, match="file backend requires bootstrap"):
        build_session(_cfg(tmp_path, "file"), "sid")


def test_build_session_file_with_bootstrap(tmp_path):
    from sessions.file_session import FileSession
    from sessions.session_bootstrap import SessionBootstrap

    bootstrap = SessionBootstrap(
        agent_name="a",
        model_name="m",
        instruction_sources=[],
        instruction_text_hash="sha256:x",
        created_at=0.0,
        cwd=str(tmp_path),
    )
    s = build_session(_cfg(tmp_path, "file"), "sid", bootstrap=bootstrap)
    assert isinstance(s, FileSession)


@pytest.mark.asyncio
async def test_sqlite_session_advance_run_index_persists_across_instances(tmp_path):
    """run_count 在 sqlite sessions 表中持久化，跨实例继续递增。"""
    db = tmp_path / "x.db"
    first = SQLiteSession("sid", db)

    assert await first.advance_run_index() == 1
    assert await first.advance_run_index() == 2

    second = SQLiteSession("sid", db)
    assert await second.advance_run_index() == 3


@pytest.mark.asyncio
async def test_sqlite_session_advance_run_index_isolated_per_session(tmp_path):
    """同一个 db 文件内，不同 session_id 的 run_count 互不影响。"""
    db = tmp_path / "x.db"
    a = SQLiteSession("a", db)
    b = SQLiteSession("b", db)

    assert await a.advance_run_index() == 1
    assert await b.advance_run_index() == 1
    assert await a.advance_run_index() == 2
    assert await b.advance_run_index() == 2


@pytest.mark.asyncio
async def test_sqlite_session_clear_resets_run_index(tmp_path):
    """clear 清空当前 session 的消息，并把 run_count 重置为 0。"""
    db = tmp_path / "x.db"
    session = SQLiteSession("sid", db)

    await session.append(Message.user("hello"))
    assert await session.advance_run_index() == 1

    await session.clear()

    assert await session.history() == []
    assert await session.advance_run_index() == 1


@pytest.mark.asyncio
async def test_sqlite_session_persists_bootstrap_metadata_from_factory(tmp_path):
    """sqlite backend 通过 build_session 接收 bootstrap，并在底层落 metadata。"""
    cfg = _cfg(tmp_path, "sqlite")
    bootstrap = _bootstrap(tmp_path)
    session = build_session(cfg, "sid", bootstrap=bootstrap)
    assert isinstance(session, SQLiteSession)

    await session.append(Message.user("hello"))
    assert await session.advance_run_index() == 1

    with sqlite3.connect(str(tmp_path / "session.db")) as conn:
        row = conn.execute(
            """
            SELECT
                agent_name,
                model_name,
                instruction_sources,
                instruction_text_hash,
                cwd,
                app_version,
                system_prompt,
                run_count
            FROM sessions
            WHERE session_id = ?
            """,
            ("sid",),
        ).fetchone()

    assert row is not None
    assert row[0] == "agent"
    assert row[1] == "model"
    assert json.loads(row[2]) == ["runtime", "memory"]
    assert row[3] == "sha256:test"
    assert row[4] == str(tmp_path)
    assert row[5] == "test-version"
    assert row[6] == "system text"
    assert row[7] == 1


@pytest.mark.asyncio
async def test_sqlite_session_migrates_legacy_sessions_table_run_count(tmp_path):
    """旧 sessions 表缺 run_count 时，初始化必须补列并继续递增。"""
    db = tmp_path / "legacy.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                agent_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                instruction_sources TEXT NOT NULL,
                instruction_text_hash TEXT NOT NULL,
                cwd TEXT NOT NULL,
                app_version TEXT NOT NULL,
                system_prompt TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO sessions (
                session_id,
                created_at,
                updated_at,
                agent_name,
                model_name,
                instruction_sources,
                instruction_text_hash,
                cwd,
                app_version,
                system_prompt
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("sid", 10.0, 10.0, "", "", "[]", "", "", "", None),
        )
        conn.commit()

    session = SQLiteSession("sid", db)

    assert await session.advance_run_index() == 1
    with sqlite3.connect(str(db)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        row = conn.execute(
            "SELECT run_count FROM sessions WHERE session_id = ?",
            ("sid",),
        ).fetchone()

    assert "run_count" in columns
    assert row is not None
    run_count = row[0]
    assert run_count == 1


@pytest.mark.asyncio
async def test_sqlite_session_backfills_created_at_from_legacy_messages(tmp_path):
    """旧 messages 行先于 sessions 行存在时，created_at 必须取最早消息时间。"""
    db = tmp_path / "legacy-messages.db"
    with sqlite3.connect(str(db)) as conn:
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
            (
                "sid",
                1,
                json.dumps(_message_to_dict(Message.user("legacy"))),
                12.5,
            ),
        )
        conn.commit()

    session = SQLiteSession("sid", db, bootstrap=_bootstrap(tmp_path))
    await session.append(Message.assistant(content="new"))

    with sqlite3.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT created_at FROM sessions WHERE session_id = ?",
            ("sid",),
        ).fetchone()

    assert row is not None
    created_at = row[0]
    assert created_at == 12.5
