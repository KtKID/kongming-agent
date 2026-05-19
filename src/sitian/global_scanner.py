"""
司天全局扫描器——独立于 web scanner，纯 stdlib 实现。

Role:
    全局扫描 ~/.claude/projects 和 ~/.codex/sessions 目录结构，
    列出所有最近活跃的项目和 session 摘要。
    独立于 web scanner：web 是 worktree 注册视角（只看已登记项目），
    sitian 是全局观察视角（扫描所有项目）。两者语义不同，不共用。

Owns:
    - Claude / Codex 项目目录扫描逻辑
    - session 索引解析（title / mtime / cwd 提取）
    - 项目排序（按 mtime desc）

Does not own:
    - session 消息内容读取（session_reader.py）
    - 观察记录生成（scanners.py）
    - 配置（config.py）

Called by:
    - scanners.py（claude_workspace / claude_project / codex_project 扫描）

Key outputs:
    - list_claude_projects_global() → list[SiTianProjectInfo]
    - list_codex_projects_global() → list[SiTianCodexProjectInfo]
    - SiTianSessionInfo / SiTianCodexSessionInfo（session 摘要）

Change risks:
    - ~/.claude/projects 目录结构变化会破坏项目发现
    - 零外部依赖约束：不能引入 pydantic / httpx 等
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_TITLE_MAX_LEN = 60


@dataclass(frozen=True)
class SiTianSessionInfo:
    thread_id: str
    title: str
    cwd: str
    last_modified: float
    message_count: int


@dataclass(frozen=True)
class SiTianProjectInfo:
    name: str
    cwd: str
    display_name: str
    sessions: list[SiTianSessionInfo]


def list_claude_projects_global(claude_home: Path | None = None) -> list[SiTianProjectInfo]:
    home = claude_home if claude_home is not None else Path.home() / ".claude"
    projects_dir = home / "projects"
    if not projects_dir.is_dir():
        return []

    summaries: list[SiTianProjectInfo] = []
    try:
        entries = [e for e in os.scandir(projects_dir) if e.is_dir()]
    except OSError:
        return []

    for entry in entries:
        project = _build_claude_project(entry.name, Path(entry.path))
        if project is not None:
            summaries.append(project)

    summaries.sort(key=_project_sort_key, reverse=True)
    return summaries


@dataclass(frozen=True)
class SiTianCodexSessionInfo:
    session_id: str
    title: str
    cwd: str
    last_modified: float
    message_count: int
    rollout_path: str
    cli_version: str
    provider: str


@dataclass(frozen=True)
class SiTianCodexProjectInfo:
    cwd: str
    display_name: str
    sessions: list[SiTianCodexSessionInfo]


def list_codex_projects_global(codex_home: Path | None = None) -> list[SiTianCodexProjectInfo]:
    home = (
        codex_home
        if codex_home is not None
        else Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    )
    sessions_dir = home / "sessions"
    if not sessions_dir.is_dir():
        return []

    index_map = _load_session_index(home / "session_index.jsonl")

    by_cwd: dict[str, list[SiTianCodexSessionInfo]] = {}
    try:
        rollout_files = sorted(
            _find_rollout_files(sessions_dir),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []

    for rollout_path in rollout_files:
        session = _build_codex_session(rollout_path, index_map)
        if session is None:
            continue
        by_cwd.setdefault(session.cwd, []).append(session)

    projects: list[SiTianCodexProjectInfo] = []
    for cwd, sessions in by_cwd.items():
        sessions.sort(key=lambda s: s.last_modified, reverse=True)
        projects.append(
            SiTianCodexProjectInfo(
                cwd=cwd,
                display_name=cwd.rsplit("/", 1)[-1] if cwd else "unknown",
                sessions=sessions,
            )
        )
    projects.sort(
        key=lambda p: max((s.last_modified for s in p.sessions), default=0.0), reverse=True
    )
    return projects


def _project_sort_key(p: SiTianProjectInfo) -> float:
    if not p.sessions:
        return 0.0
    return max(s.last_modified for s in p.sessions)


def _build_claude_project(name: str, project_path: Path) -> SiTianProjectInfo | None:
    sessions: list[SiTianSessionInfo] = []
    real_cwds: list[str] = []
    try:
        with os.scandir(project_path) as it:
            for entry in it:
                if not entry.is_file():
                    continue
                fname = entry.name
                if not fname.endswith(".jsonl"):
                    continue
                if fname.startswith("agent-"):
                    continue
                result = _build_claude_session(Path(entry.path))
                if result is not None:
                    session, real_cwd = result
                    sessions.append(session)
                    if real_cwd:
                        real_cwds.append(real_cwd)
    except OSError:
        return None

    sessions.sort(key=lambda s: s.last_modified, reverse=True)
    cwd = real_cwds[0] if real_cwds else _decode_cwd(name)
    display_name = cwd.rsplit("/", 1)[-1] if cwd else name
    return SiTianProjectInfo(name=name, cwd=cwd, display_name=display_name, sessions=sessions)


def _build_claude_session(jsonl_path: Path) -> tuple[SiTianSessionInfo, str] | None:
    try:
        stat = jsonl_path.stat()
    except OSError:
        return None
    last_modified = stat.st_mtime
    thread_id = jsonl_path.stem

    message_count = 0
    title = "(空会话)"
    real_cwd = ""

    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                message_count += 1
                if title != "(空会话)" and real_cwd:
                    continue
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry: dict[str, Any] = json.loads(stripped)
                except (ValueError, json.JSONDecodeError):
                    continue
                if title == "(空会话)":
                    extracted = _extract_claude_title(entry)
                    if extracted:
                        title = extracted
                if not real_cwd:
                    candidate = entry.get("cwd")
                    if isinstance(candidate, str) and candidate.strip():
                        real_cwd = str(Path(candidate).expanduser())
    except OSError:
        return None

    if message_count == 0:
        return None
    return SiTianSessionInfo(
        thread_id=thread_id,
        title=title,
        cwd=real_cwd,
        last_modified=last_modified,
        message_count=message_count,
    ), real_cwd


def _extract_claude_title(entry: dict[str, Any]) -> str | None:
    if entry.get("type") != "user":
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    cleaned = " ".join(content.split())
    return cleaned[:_TITLE_MAX_LEN] if cleaned else None


def _build_codex_session(
    rollout_path: Path, index_map: dict[str, str] | None = None
) -> SiTianCodexSessionInfo | None:
    try:
        stat = rollout_path.stat()
    except OSError:
        return None

    session_id = _extract_session_id_from_filename(rollout_path.name)
    cwd = ""
    title = "(空会话)"
    cli_version = ""
    message_count = 0

    try:
        with rollout_path.open("r", encoding="utf-8") as f:
            for line in f:
                message_count += 1
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry: dict[str, Any] = json.loads(stripped)
                except (ValueError, json.JSONDecodeError):
                    continue
                entry_type = entry.get("type", "")
                if entry_type == "session_meta":
                    payload = entry.get("payload", {})
                    if isinstance(payload, dict):
                        cwd = (
                            str(Path(str(payload.get("cwd", ""))).expanduser())
                            if payload.get("cwd")
                            else ""
                        )
                        cli_version = str(payload.get("cli_version", ""))
                if title == "(空会话)" and entry_type == "user":
                    msg = entry.get("message", {})
                    if isinstance(msg, dict):
                        content = msg.get("content", "")
                        if isinstance(content, str) and content.strip():
                            title = " ".join(content.split())[:_TITLE_MAX_LEN]
    except OSError:
        return None

    if not cwd:
        return None
    if index_map and session_id in index_map:
        title = index_map[session_id]
    return SiTianCodexSessionInfo(
        session_id=session_id,
        title=title,
        cwd=os.path.realpath(cwd),
        last_modified=stat.st_mtime,
        message_count=message_count,
        rollout_path=str(rollout_path.resolve()),
        cli_version=cli_version,
        provider="codex",
    )


def _load_session_index(index_path: Path) -> dict[str, str]:
    """读 session_index.jsonl，返回 {session_id: thread_name}。"""
    result: dict[str, str] = {}
    if not index_path.is_file():
        return result
    try:
        with index_path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj: dict[str, Any] = json.loads(stripped)
                except (ValueError, json.JSONDecodeError):
                    continue
                sid = obj.get("id")
                thread_name = obj.get("thread_name")
                if isinstance(sid, str) and sid and isinstance(thread_name, str) and thread_name:
                    result[sid] = thread_name
    except OSError:
        pass
    return result


def _find_rollout_files(sessions_dir: Path) -> list[Path]:
    results: list[Path] = []
    try:
        for root, _dirs, files in os.walk(sessions_dir):
            for fname in files:
                if fname.startswith("rollout-") and fname.endswith(".jsonl"):
                    results.append(Path(root) / fname)
    except OSError:
        pass
    return results


_UUID_SEGMENT_COUNT = 5


def _extract_session_id_from_filename(filename: str) -> str:
    stem = filename.removesuffix(".jsonl")
    parts = stem.split("-")
    if len(parts) >= _UUID_SEGMENT_COUNT:
        return "-".join(parts[-_UUID_SEGMENT_COUNT:])
    return stem


def _decode_cwd(encoded_name: str) -> str:
    return encoded_name.replace("-", "/")


__all__ = [
    "SiTianCodexProjectInfo",
    "SiTianCodexSessionInfo",
    "SiTianProjectInfo",
    "SiTianSessionInfo",
    "list_claude_projects_global",
    "list_codex_projects_global",
]
