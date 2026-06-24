"""Session 协议的工程化实现。

这个文件提供 :class:`SQLiteSession`——基于 stdlib ``sqlite3`` 的
:class:`core.contracts.Session` 协议工程化实现。它是 :class:`core.session.InMemorySession`
的升级版实现，不是新的 Session 抽象。

严格约束（对齐 ``docs/kongming-agent-v1-minimal/10-contracts.md`` "Session 边界"）：

- :class:`Session` Protocol **只在** :mod:`core.contracts` 定义，这里**只 import**，
  不重新定义
- 上层只认 :class:`core.contracts.Session`，调用点用 :class:`InMemorySession` 还是
  :class:`SQLiteSession` 由 :func:`build_session` 工厂按 :class:`Config.session.backend`
  决定

线程 / 连接策略：

- stdlib ``sqlite3.Connection`` 默认约束 "对象只能被创建它的线程使用"（同线程约束）
- 这里采取的做法是：**所有写 / 读操作都走 asyncio.to_thread，在里面现场打开连接、
  执行完就关**，彻底绕开"跨线程复用 Connection"这条雷。代价是多打开一次 SQLite
  文件句柄，换来在 v1-mini 阶段极简、无锁、无状态的实现
- 库内不再把 Connection 缓存到 ``threading.local`` / 单例，也不保持长连接
- schema 初始化通过 ``asyncio.Lock`` + ``_init_done`` 布尔位保证只做一次；初始化同样
  是每次 ``sqlite3.connect(...)`` 现场执行，同线程约束自动满足
- 本期不引入 ``aiosqlite`` 等新依赖，也不开 WAL / PRAGMA 优化。v0.2+ 如果需要
  并发高频写，再升一层连接池或换 async 驱动

序列化策略：

- :class:`core.message.Message` 是 frozen dataclass，``tool_calls`` 内嵌
  :class:`core.message.ToolCall` 元组
- 使用 JSON 作为持久化格式，序列化走 :func:`_message_to_dict`，反序列化走
  :func:`_message_from_dict`。``ToolCall`` 嵌套也要完整还原，往返等价
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.contracts import Session
from core.message import Message, ToolCall
from infrastructure.config.paths import resolve_kongming_path

if TYPE_CHECKING:
    from infrastructure.config.models import Config
    from sessions.session_bootstrap import SessionBootstrap


# ---------------------------------------------------------------------------
# Message <-> dict 的 JSON 往返
# ---------------------------------------------------------------------------


def _tool_call_to_dict(call: ToolCall) -> dict[str, Any]:
    """把 :class:`ToolCall` 转换为 JSON 可序列化 dict。

    ``arguments`` 要求 JSON 可序列化；不做深度拷贝也不做二次 JSON encode，
    由调用方（模型 / tool registry）保证。
    """
    return {
        "call_id": call.call_id,
        "tool_name": call.tool_name,
        "arguments": dict(call.arguments),
    }


def _tool_call_from_dict(data: dict[str, Any]) -> ToolCall:
    """与 :func:`_tool_call_to_dict` 对称的反向构造。"""
    return ToolCall(
        call_id=str(data["call_id"]),
        tool_name=str(data["tool_name"]),
        arguments=dict(data.get("arguments") or {}),
    )


def _message_to_dict(msg: Message) -> dict[str, Any]:
    """把 :class:`Message` dataclass 转换为 JSON 可序列化 dict。

    保留全部字段；``tool_calls`` 为 ``None`` 时落地为 ``None``，非空时展开成 list。
    和 :func:`_message_from_dict` 组成完整往返映射。
    """
    tool_calls: list[dict[str, Any]] | None
    if msg.tool_calls is None:
        tool_calls = None
    else:
        tool_calls = [_tool_call_to_dict(c) for c in msg.tool_calls]
    return {
        "role": msg.role,
        "content": msg.content,
        "tool_calls": tool_calls,
        "tool_call_id": msg.tool_call_id,
        "name": msg.name,
        "metadata": dict(msg.metadata),
    }


def _message_from_dict(data: dict[str, Any]) -> Message:
    """与 :func:`_message_to_dict` 对称的反向构造。

    忽略未知字段，缺失字段按 :class:`Message` 默认值处理，保证版本升级时旧 payload
    仍可读。
    """
    raw_calls = data.get("tool_calls")
    tool_calls: tuple[ToolCall, ...] | None
    if raw_calls is None:
        tool_calls = None
    else:
        tool_calls = tuple(_tool_call_from_dict(c) for c in raw_calls)
    return Message(
        role=data["role"],
        content=data.get("content"),
        tool_calls=tool_calls,
        tool_call_id=data.get("tool_call_id"),
        name=data.get("name"),
        metadata=dict(data.get("metadata") or {}),
    )


# ---------------------------------------------------------------------------
# SQLiteSession
# ---------------------------------------------------------------------------


# schema 版本号，写入 payload 头部时用于未来兼容升级。
_SCHEMA_VERSION = 1

_SESSION_COLUMNS_SQL: dict[str, str] = {
    "session_id": "TEXT PRIMARY KEY",
    "created_at": "REAL NOT NULL DEFAULT 0",
    "updated_at": "REAL NOT NULL DEFAULT 0",
    "agent_name": "TEXT NOT NULL DEFAULT ''",
    "model_name": "TEXT NOT NULL DEFAULT ''",
    "instruction_sources": "TEXT NOT NULL DEFAULT '[]'",
    "instruction_text_hash": "TEXT NOT NULL DEFAULT ''",
    "cwd": "TEXT NOT NULL DEFAULT ''",
    "app_version": "TEXT NOT NULL DEFAULT ''",
    "system_prompt": "TEXT",
    "run_count": "INTEGER NOT NULL DEFAULT 0",
}


class SQLiteSession:
    """:class:`core.contracts.Session` 协议的 SQLite 工程化实现。

    结构性满足 :class:`core.contracts.Session`：同时提供 ``session_id`` 属性和
    ``append / history / clear / advance_run_index`` async 方法。类型注解上不显式继承 Protocol，
    靠 Python 的结构化协议校验。

    表结构::

        messages(
            session_id TEXT NOT NULL,
            seq        INTEGER NOT NULL,
            payload    TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY(session_id, seq)
        )

        sessions(
            session_id TEXT PRIMARY KEY,
            ...
            run_count INTEGER NOT NULL DEFAULT 0
        )

    ``payload`` 为 :func:`_message_to_dict` 输出后 ``json.dumps`` 的字符串；
    ``seq`` 从 1 起递增，唯一决定同一个 session 的消息顺序。
    """

    def __init__(
        self,
        session_id: str,
        db_path: str | Path,
        *,
        bootstrap: SessionBootstrap | None = None,
    ) -> None:
        """初始化。

        Args:
            session_id: 会话标识。同一个 db 文件可以存多个 session，通过此字段隔离。
            db_path: SQLite 文件路径。父目录不存在时会在初始化阶段尝试创建。
            bootstrap: 会话初始化元数据。未传入时使用空元数据兜底，兼容直接构造
                ``SQLiteSession`` 的测试和旧调用点。
        """
        self.session_id: str = session_id
        self._db_path: str = str(db_path)
        self._bootstrap: SessionBootstrap | None = bootstrap
        self._init_done: bool = False
        self._init_lock: asyncio.Lock = asyncio.Lock()

    # -- 初始化 --------------------------------------------------------------

    async def _ensure_init(self) -> None:
        """幂等初始化 schema。

        并发 ``append`` / ``history`` / ``clear`` 首次调用时，通过 ``asyncio.Lock``
        串行化初始化，避免重复建表；后续调用直接 fast-path 返回。
        """
        if self._init_done:
            return
        async with self._init_lock:
            if self._init_done:
                return
            await asyncio.to_thread(self._init_db_sync)
            self._init_done = True

    def _init_db_sync(self) -> None:
        """同步建表。每次调用现场开连接 / 执行 / 关闭，满足 sqlite3 同线程约束。"""
        path = Path(self._db_path)
        # 父目录可能还不存在（例如默认的 .kongming/sessions.db）。
        if path.parent and str(path.parent) not in ("", "."):
            path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    agent_name TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    instruction_sources TEXT NOT NULL,
                    instruction_text_hash TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    app_version TEXT NOT NULL,
                    system_prompt TEXT,
                    run_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(session_id, seq)
                )
                """
            )
            self._migrate_sessions_table_sync(conn)
            conn.commit()

    def _migrate_sessions_table_sync(self, conn: sqlite3.Connection) -> None:
        """补齐旧 sessions 表缺失列，输入 sqlite 连接，无返回值。"""

        rows = conn.execute("PRAGMA table_info(sessions)").fetchall()
        existing = {str(row[1]) for row in rows}
        for name, definition in _SESSION_COLUMNS_SQL.items():
            if name in existing or name == "session_id":
                continue
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")

    def _legacy_created_at_sync(self, conn: sqlite3.Connection, *, now: float) -> float:
        """从旧 messages 行推断 session 创建时间，输入连接和当前时间，输出时间戳。"""

        row = conn.execute(
            "SELECT MIN(created_at) FROM messages WHERE session_id = ?",
            (self.session_id,),
        ).fetchone()
        if row and row[0] is not None:
            return float(row[0])
        if self._bootstrap is not None:
            return float(self._bootstrap.created_at)
        return now

    def _ensure_session_row_sync(self, conn: sqlite3.Connection, *, now: float) -> None:
        """确保当前 session 的元数据行存在。"""
        bootstrap = self._bootstrap
        if bootstrap is None:
            created_at = self._legacy_created_at_sync(conn, now=now)
            agent_name = ""
            model_name = ""
            instruction_sources = "[]"
            instruction_text_hash = ""
            cwd = ""
            app_version = ""
            system_prompt = None
        else:
            created_at = self._legacy_created_at_sync(conn, now=now)
            agent_name = bootstrap.agent_name
            model_name = bootstrap.model_name
            instruction_sources = json.dumps(bootstrap.instruction_sources, ensure_ascii=False)
            instruction_text_hash = bootstrap.instruction_text_hash
            cwd = bootstrap.cwd
            app_version = bootstrap.app_version or ""
            system_prompt = bootstrap.instruction_text
        conn.execute(
            """
            INSERT OR IGNORE INTO sessions (
                session_id,
                created_at,
                updated_at,
                agent_name,
                model_name,
                instruction_sources,
                instruction_text_hash,
                cwd,
                app_version,
                system_prompt,
                run_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(session_id) DO UPDATE SET
                updated_at = excluded.updated_at
            """,
            (
                self.session_id,
                created_at,
                now,
                agent_name,
                model_name,
                instruction_sources,
                instruction_text_hash,
                cwd,
                app_version,
                system_prompt,
            ),
        )

    # -- Session 协议实现 ----------------------------------------------------

    async def append(self, message: Message, *, usage: dict[str, Any] | None = None) -> None:
        """追加一条消息到末尾。"""
        await self._ensure_init()
        await asyncio.to_thread(self._append_sync, message, usage)

    def _append_sync(self, message: Message, usage: dict[str, Any] | None = None) -> None:
        """同步 append：确保 session 行存在，查最大 seq + 1，再 insert。"""
        payload_obj = {
            "schema_version": _SCHEMA_VERSION,
            "message": _message_to_dict(message),
        }
        if usage is not None:
            payload_obj["usage"] = dict(usage)
        payload = json.dumps(payload_obj, ensure_ascii=False)
        now = time.time()
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_session_row_sync(conn, now=now)
            cur = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE session_id = ?",
                (self.session_id,),
            )
            row = cur.fetchone()
            next_seq = int(row[0]) + 1 if row else 1
            conn.execute(
                "INSERT INTO messages (session_id, seq, payload, created_at) VALUES (?, ?, ?, ?)",
                (self.session_id, next_seq, payload, now),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (now, self.session_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def history(self) -> list[Message]:
        """返回按 append 顺序排列的历史副本。"""
        await self._ensure_init()
        payloads = await asyncio.to_thread(self._history_sync)
        messages: list[Message] = []
        for raw in payloads:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # 损坏的 payload 直接跳过，避免一条坏记录炸掉整个 history 读取。
                continue
            # 兼容历史 payload（没有 schema_version 包裹，直接是 Message dict）。
            if isinstance(data, dict) and "message" in data and "schema_version" in data:
                message_dict = data["message"]
            else:
                message_dict = data
            try:
                messages.append(_message_from_dict(message_dict))
            except (KeyError, TypeError):
                continue
        return messages

    def _history_sync(self) -> list[str]:
        """同步读取所有 payload 字符串。"""
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "SELECT payload FROM messages WHERE session_id = ? ORDER BY seq ASC",
                (self.session_id,),
            )
            return [str(row[0]) for row in cur.fetchall()]

    async def clear(self) -> None:
        """清空当前 session 的全部历史（不影响同 db 其他 session）。"""
        await self._ensure_init()
        await asyncio.to_thread(self._clear_sync)

    def _clear_sync(self) -> None:
        """同步清空，并把当前 session 的 run_count 重置为 0。"""
        now = time.time()
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_session_row_sync(conn, now=now)
            conn.execute(
                "DELETE FROM messages WHERE session_id = ?",
                (self.session_id,),
            )
            conn.execute(
                "UPDATE sessions SET run_count = 0, updated_at = ? WHERE session_id = ?",
                (now, self.session_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def advance_run_index(self) -> int:
        """递增并返回当前 session 的 run 编号。"""
        await self._ensure_init()
        return await asyncio.to_thread(self._advance_run_index_sync)

    def _advance_run_index_sync(self) -> int:
        """同步递增 run_count；事务保证同一 sqlite db 内原子递增。"""
        now = time.time()
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_session_row_sync(conn, now=now)
            conn.execute(
                """
                UPDATE sessions
                SET run_count = run_count + 1,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (now, self.session_id),
            )
            row = conn.execute(
                "SELECT run_count FROM sessions WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if row is None:
            raise RuntimeError(f"session row missing after upsert: {self.session_id}")
        return int(row[0])

    # -- 调试辅助 ------------------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - 调试友好
        return f"SQLiteSession(session_id={self.session_id!r}, db_path={self._db_path!r})"


# ---------------------------------------------------------------------------
# 工厂：按 config.session.backend 建具体 Session 实现
# ---------------------------------------------------------------------------


def build_session(
    config: Config,
    session_id: str,
    *,
    bootstrap: SessionBootstrap | None = None,
) -> Session:
    """按 ``config.session.backend`` 建合适的 :class:`core.contracts.Session` 实现。

    - ``memory`` → :class:`core.session.InMemorySession`
    - ``sqlite`` → :class:`SQLiteSession`，写入 ``config.session.store_path``
    - ``file`` → :class:`FileSession`，写入 ``config.session.file_store_path`` 子目录

    返回值结构性满足 :class:`core.contracts.Session` Protocol（``runtime_checkable``）。
    上层调用点只认 Protocol 签名，不关心具体实现。

    Args:
        config: 统一配置对象，至少含 ``session.backend`` / ``session.store_path``。
        session_id: 此次运行绑定的 session 标识。
        bootstrap: CLI 阶段产出的稳定元数据，file / sqlite backend 会持久化使用；
            memory backend 忽略此参数。

    Raises:
        ValueError: 当 backend 取了协议未声明的值时抛出。
    """
    backend = config.session.backend
    if backend == "memory":
        # 局部 import 避免在模块加载时就把 core.session 拉进来（其实无所谓，core 也允许
        # 被 import；写成 lazy 只为显式表达"只有 memory 分支才需要它"）。
        from core.session import InMemorySession

        return InMemorySession(session_id)
    if backend == "sqlite":
        return SQLiteSession(
            session_id,
            resolve_kongming_path(config.session.store_path),
            bootstrap=bootstrap,
        )
    if backend == "file":
        if bootstrap is None:
            raise ValueError("file backend requires bootstrap")
        from sessions.file_session import FileSession

        return FileSession(
            session_id,
            bootstrap,
            str(resolve_kongming_path(config.session.file_store_path)),
        )
    raise ValueError(f"unknown session backend: {backend!r}")


__all__ = [
    "SQLiteSession",
    "build_session",
]
