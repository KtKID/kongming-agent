"""扫描 ``~/.codex/sessions/`` 目录，列出 Codex CLI 写入的会话摘要。

公共入口 :func:`list_codex_projects` 返回 :class:`CodexProjectSummary` 列表，
每个 project 含若干 :class:`CodexSessionSummary`（按 ``last_modified`` 倒序）。
projects 间按各 project 内最新 session 的 ``last_modified`` 倒序。

数据源策略（双源合并）：

1. **快通道**：读 ``~/.codex/session_index.jsonl``（每行含 id / thread_name /
   updated_at），拿到 title 真值。
2. **兜底**：递归扫 ``~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl``，每文件读
   第一行 ``session_meta`` 拿到 cwd / cli_version / originator。
3. 按 ``session_id`` 去重（索引 + 扫盘 Union），索引的 ``thread_name`` 优先
   作为 title。
4. cwd 只能从 rollout 第一行 ``session_meta.payload.cwd`` 获取（索引不含 cwd）。

设计要点：

- 流式读第一行（``readline``），rollout 可能 16 MB+，绝不全量读
- ``os.path.realpath()`` 归一 cwd（macOS ``/private/tmp`` ↔ ``/tmp`` symlink）
- 纯只读，从不写入任何文件
- 不依赖 SDK / claude_code，只 import 标准库
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "CodexProjectSummary",
    "CodexSessionSummary",
    "list_codex_projects",
]

_TITLE_MAX_LEN = 40
_TITLE_PLACEHOLDER = "(无标题)"

# rollout 文件名格式: rollout-<ISO>-<UUIDv7>.jsonl
# UUIDv7 含 5 段 hex，用 ``-`` 分隔；从文件名末尾取倒数 5 段拼回。
_UUID_SEGMENT_COUNT = 5


@dataclass(frozen=True)
class CodexSessionSummary:
    """单个 Codex session（rollout jsonl 文件）的摘要。"""

    session_id: str
    """codex thread_id（UUIDv7），从文件名或 session_meta 提取。"""

    title: str
    """session_index 的 thread_name 优先；无则从 session_meta 推断或占位。"""

    cwd: str
    """session_meta.payload.cwd 真值（经 realpath 归一）。"""

    last_modified: float
    """rollout 文件 mtime（Unix 时间戳）。"""

    message_count: int
    """rollout 总行数。"""

    cli_version: str
    """session_meta.payload.cli_version。"""

    rollout_path: str
    """rollout jsonl 绝对路径。"""

    provider: str = "codex"
    """固定 ``"codex"``。"""


@dataclass(frozen=True)
class CodexProjectSummary:
    """按 cwd 聚合的 Codex 项目摘要。"""

    cwd: str
    """项目工作目录（session_meta.cwd 真值，经 realpath 归一）。"""

    display_name: str
    """``cwd`` 最后一段 basename。"""

    sessions: list[CodexSessionSummary]
    """按 ``last_modified`` 倒序的 session 摘要列表。"""

    last_modified: float
    """``max(session.last_modified)``。"""


# ---------------------------------------------------------------------------
# 内部：索引条目
# ---------------------------------------------------------------------------


@dataclass
class _IndexEntry:
    """session_index.jsonl 单行解析结果。"""

    session_id: str
    thread_name: str
    updated_at: float  # unix timestamp


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


def list_codex_projects(
    codex_home: Path | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> list[CodexProjectSummary]:
    """扫描 ``~/.codex/sessions/`` 列出所有 codex 项目 + 会话。

    Args:
        codex_home: Codex 数据目录，默认 ``Path.home() / ".codex"``。
            单元测试可注入临时目录。
        progress_callback: ``(current, total, session_id)`` 进度回调。

    Returns:
        CodexProjectSummary 列表，按 ``last_modified`` 倒序。
        ``codex_home`` 不存在时返回空列表。
    """
    home = codex_home if codex_home is not None else Path.home() / ".codex"

    # --- 1. 快通道：读索引拿 thread_name ---
    index_map = _load_session_index(home / "session_index.jsonl")

    # --- 2. 兜底：递归扫 rollout 文件 ---
    sessions_dir = home / "sessions"
    if not sessions_dir.is_dir():
        return []

    rollout_files = _find_rollout_files(sessions_dir)
    total = len(rollout_files)

    # session_id → CodexSessionSummary
    session_by_id: dict[str, CodexSessionSummary] = {}

    for idx, rollout_path in enumerate(rollout_files, start=1):
        summary = _build_session_from_rollout(rollout_path, index_map)
        if summary is not None:
            # 去重：同一 session_id 保留 mtime 更晚的
            existing = session_by_id.get(summary.session_id)
            if existing is None or summary.last_modified > existing.last_modified:
                session_by_id[summary.session_id] = summary
        if progress_callback is not None:
            sid = summary.session_id if summary else rollout_path.name
            progress_callback(idx, total, sid)

    # --- 3. 按 cwd 分组 ---
    groups: dict[str, list[CodexSessionSummary]] = defaultdict(list)
    for session in session_by_id.values():
        groups[session.cwd].append(session)

    # --- 4. 组装 ProjectSummary ---
    projects: list[CodexProjectSummary] = []
    for cwd, sessions in groups.items():
        sessions.sort(key=lambda s: s.last_modified, reverse=True)
        display_name = os.path.basename(cwd) or cwd
        projects.append(
            CodexProjectSummary(
                cwd=cwd,
                display_name=display_name,
                sessions=sessions,
                last_modified=sessions[0].last_modified,
            )
        )

    projects.sort(key=lambda p: p.last_modified, reverse=True)
    return projects


# ---------------------------------------------------------------------------
# 内部：索引读取
# ---------------------------------------------------------------------------


def _load_session_index(index_path: Path) -> dict[str, _IndexEntry]:
    """读 session_index.jsonl，返回 ``{session_id: _IndexEntry}``。

    文件不存在 / 损坏 → 返回空 dict（降级到纯扫盘）。
    """
    result: dict[str, _IndexEntry] = {}
    if not index_path.is_file():
        return result
    try:
        with index_path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except (ValueError, json.JSONDecodeError):
                    continue
                sid = obj.get("id")
                if not isinstance(sid, str) or not sid:
                    continue
                thread_name = obj.get("thread_name", "")
                updated_at = _parse_iso_timestamp(obj.get("updated_at", ""))
                result[sid] = _IndexEntry(
                    session_id=sid,
                    thread_name=thread_name if isinstance(thread_name, str) else "",
                    updated_at=updated_at,
                )
    except OSError:
        pass
    return result


# ---------------------------------------------------------------------------
# 内部：扫描 rollout 文件
# ---------------------------------------------------------------------------


def _find_rollout_files(sessions_dir: Path) -> list[Path]:
    """递归查找 ``sessions/`` 下所有 ``rollout-*.jsonl`` 文件。"""
    results: list[Path] = []
    try:
        for root, _dirs, files in os.walk(sessions_dir):
            for fname in files:
                if fname.startswith("rollout-") and fname.endswith(".jsonl"):
                    results.append(Path(root) / fname)
    except OSError:
        pass
    return results


def _extract_session_id_from_filename(filename: str) -> str:
    """从 ``rollout-<ISO>-<UUIDv7>.jsonl`` 提取 UUIDv7。

    文件名示例: ``rollout-2026-03-14T14:55:46-019c429a-b3e2-7f00-a1d2-e4f5g6h7i8j9.jsonl``

    策略：去掉 ``.jsonl`` 后缀，用 ``-`` 分割，取倒数 5 段拼回。
    """
    stem = filename.removesuffix(".jsonl")
    parts = stem.split("-")
    if len(parts) >= _UUID_SEGMENT_COUNT:
        return "-".join(parts[-_UUID_SEGMENT_COUNT:])
    return stem


# ---------------------------------------------------------------------------
# 内部：构建单条 session
# ---------------------------------------------------------------------------


def _build_session_from_rollout(
    rollout_path: Path,
    index_map: dict[str, _IndexEntry],
) -> CodexSessionSummary | None:
    """读 rollout 第一行 session_meta + 统计行数，组装 CodexSessionSummary。

    返回 None：文件无法读取 / 为空 / 第一行不含 session_meta。
    """
    try:
        stat = rollout_path.stat()
    except OSError:
        return None
    last_modified = stat.st_mtime

    # 从文件名提取 session_id（作为 fallback）
    filename_sid = _extract_session_id_from_filename(rollout_path.name)

    # 流式读第一行 + 统计总行数
    first_line: str | None = None
    message_count = 0
    try:
        with rollout_path.open("r", encoding="utf-8") as f:
            for line in f:
                message_count += 1
                if first_line is None:
                    first_line = line
    except OSError:
        return None

    if message_count == 0 or first_line is None:
        return None

    # 解析第一行 session_meta
    meta = _parse_session_meta(first_line)
    if meta is None:
        return None

    session_id = meta.get("id", filename_sid) or filename_sid
    raw_cwd = meta.get("cwd", "")
    cli_version = meta.get("cli_version", "")

    # cwd 归一：realpath 处理 macOS symlink（/tmp → /private/tmp）
    cwd = os.path.realpath(raw_cwd) if raw_cwd else ""
    if not cwd:
        # 没有 cwd 的 session 无法归类到项目，跳过
        return None

    # title：索引 thread_name 优先，无则占位
    index_entry = index_map.get(session_id)
    title = _TITLE_PLACEHOLDER
    if index_entry and index_entry.thread_name:
        title = index_entry.thread_name
        if len(title) > _TITLE_MAX_LEN:
            title = title[:_TITLE_MAX_LEN]

    # 索引的 updated_at 可能比文件 mtime 更准确
    if index_entry and index_entry.updated_at > 0:
        last_modified = max(last_modified, index_entry.updated_at)

    return CodexSessionSummary(
        session_id=session_id,
        title=title,
        cwd=cwd,
        last_modified=last_modified,
        message_count=message_count,
        cli_version=cli_version if isinstance(cli_version, str) else "",
        rollout_path=str(rollout_path),
    )


# ---------------------------------------------------------------------------
# 内部：解析 session_meta
# ---------------------------------------------------------------------------


def _parse_session_meta(line: str) -> dict[str, str] | None:
    """解析 rollout 第一行，提取 session_meta payload 字段。

    期望格式::

        {"timestamp":"...","type":"session_meta","payload":{
            "id":"019c429a-...","cwd":"/...","originator":"codex_exec",
            "cli_version":"0.128.0",...}}

    返回 ``{"id":..., "cwd":..., "cli_version":..., "originator":...}``
    或 None（不合规 / 解析失败）。
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("type") != "session_meta":
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return None
    result: dict[str, str] = {}
    for key in ("id", "cwd", "cli_version", "originator"):
        val = payload.get(key)
        if isinstance(val, str):
            result[key] = val
    return result


# ---------------------------------------------------------------------------
# 内部：时间戳解析
# ---------------------------------------------------------------------------


def _parse_iso_timestamp(ts: str) -> float:
    """将 ISO 8601 时间字符串转为 Unix 时间戳。解析失败返回 0.0。"""
    if not isinstance(ts, str) or not ts:
        return 0.0
    try:
        # 处理 "2026-03-14T14:55:46.572238Z" 格式
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except (ValueError, OverflowError):
        return 0.0
