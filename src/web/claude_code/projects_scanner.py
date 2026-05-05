"""扫描 ``~/.claude/projects/`` 目录，列出 Claude Code SDK 写入的会话摘要。

公共入口 :func:`list_projects` 返回 :class:`ProjectSummary` 列表，每个 project
含若干 :class:`SessionSummary`（按 ``last_modified`` 倒序）。projects 间按各
project 内最新 session 的 ``last_modified`` 倒序。

设计要点（钉死，跟 v0.2 plan.md 对齐）：

- 跳过 ``agent-*.jsonl``（subagent 工具历史，v0.2 不展示）
- 行数为 0 的空 jsonl 跳过（不输出 SessionSummary）
- 损坏 / 完全无 user message 的 jsonl 仍然计入：``title`` 取占位文案
- ``cwd`` **优先从 jsonl entry 的 ``cwd`` 字段直接读**（SDK 自己写的真实绝对
  路径）；目录名 ``-`` → ``/`` 解码降为 fallback。原因：SDK 编码时把 ``/`` /
  ``_`` / ``.`` 都换成 ``-``，无法纯文本逆转；从 entry 读才能拿到原始 cwd。
- 不依赖 SDK，只 import 标准库

模块严格无副作用：纯函数，仅读 ``claude_home``，从不写入。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ProjectSummary", "SessionSummary", "list_projects"]


_TITLE_MAX_LEN = 40
_TITLE_EMPTY_PLACEHOLDER = "(空会话)"
_TITLE_BROKEN_PLACEHOLDER = "(无法解析)"


@dataclass(frozen=True)
class SessionSummary:
    """单个 SDK session（jsonl 文件）的摘要。"""

    sdk_session_id: str
    """jsonl 文件名（去掉 ``.jsonl`` 后缀），通常为 UUID 字符串。"""

    title: str
    """第 1 条 ``type=user`` 且 ``message.content`` 是 string 的 entry，
    取前 :data:`_TITLE_MAX_LEN` 字符（先把换行替成空格再 strip）。
    找不到合格 entry → ``"(空会话)"``；jsonl 完全无法解析 → ``"(无法解析)"``。
    """

    last_modified: float
    """jsonl 文件 mtime（Unix 时间戳）。"""

    message_count: int
    """jsonl 总行数（含空行 / 任意 type，不过滤）。"""


@dataclass(frozen=True)
class ProjectSummary:
    """单个 project（``~/.claude/projects/<name>/`` 目录）的摘要。"""

    name: str
    """编码目录名，原样保留（如 ``-Volumes-machub-app-proj-kongming-agent``）。"""

    cwd: str
    """解码后的 cwd（所有 ``-`` 替换为 ``/``）。"""

    display_name: str
    """``cwd`` 最后一段（如 ``"kongming-agent"``）。"""

    sessions: list[SessionSummary]
    """按 ``last_modified`` 倒序的 session 摘要列表。"""


def list_projects(
    claude_home: Path | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> list[ProjectSummary]:
    """扫描 ``~/.claude/projects/``，列出所有 project + 每个 project 的 session 摘要。

    Args:
        claude_home: Claude 数据目录，默认 ``Path.home() / ".claude"``。
            单元测试可注入临时目录。

    Returns:
        ProjectSummary 列表。projects 之间按 ``max(session.last_modified)`` 倒序。
        没有任何 project / projects_dir 不存在时返回空列表。

    Notes:
        - 跳过 ``agent-*.jsonl``（subagent 历史）
        - 空 jsonl（0 行）不计入 sessions
        - 不递归子目录
    """
    home = claude_home if claude_home is not None else Path.home() / ".claude"
    projects_dir = home / "projects"

    if not projects_dir.is_dir():
        return []

    summaries: list[ProjectSummary] = []
    try:
        with os.scandir(projects_dir) as it:
            project_entries = [entry for entry in it if entry.is_dir()]
    except OSError:
        # projects_dir 在中途被删 / 无权限 → 直接返回已收集到的
        return summaries

    total_projects = len(project_entries)
    for index, entry in enumerate(project_entries, start=1):
        project = _build_project_summary(entry.name, Path(entry.path))
        if project is not None:
            summaries.append(project)
        if progress_callback is not None:
            progress_callback(index, total_projects, entry.name)

    summaries.sort(key=_project_sort_key, reverse=True)
    return summaries


def _project_sort_key(p: ProjectSummary) -> float:
    if not p.sessions:
        return 0.0
    return max(s.last_modified for s in p.sessions)


def _build_project_summary(name: str, project_path: Path) -> ProjectSummary | None:
    """收集单个 project 目录下的 session 摘要并打包。

    project_path 不存在 / 无权限 → 返回 None（让 list_projects 跳过）。
    没有任何合规 session（全空 / 全是 agent-*.jsonl）也保留 ProjectSummary，
    但 sessions 为空——这样左栏可显示空 project 让用户知道扫描到了。
    """
    sessions: list[SessionSummary] = []
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
                result = _build_session_summary(Path(entry.path))
                if result is not None:
                    summary, real_cwd = result
                    sessions.append(summary)
                    if real_cwd:
                        real_cwds.append(real_cwd)
    except OSError:
        return None

    sessions.sort(key=lambda s: s.last_modified, reverse=True)

    # cwd 优先用 jsonl entry 中 SDK 写的真值；都没有则降级目录名解码
    cwd = real_cwds[0] if real_cwds else _decode_cwd(name)
    display_name = cwd.rsplit("/", 1)[-1] if cwd else name
    return ProjectSummary(
        name=name,
        cwd=cwd,
        display_name=display_name,
        sessions=sessions,
    )


def _build_session_summary(jsonl_path: Path) -> tuple[SessionSummary, str] | None:
    """读取 jsonl 文件，统计行数 + 提取 title + 取 mtime + 真实 cwd。

    返回 ``(SessionSummary, real_cwd)``；``real_cwd`` 是从 jsonl entry 的
    ``cwd`` 字段读出来的绝对路径（SDK 自己写的真值），找不到则空字符串。

    空 jsonl（0 行）→ 返回 None。
    无法 stat / 打开 → 返回 None（损坏到完全没法访问就跳过）。
    打开但 json 全损坏 → message_count 仍按行数算，title 用 ``(无法解析)``。
    """
    try:
        stat = jsonl_path.stat()
    except OSError:
        return None
    last_modified = stat.st_mtime

    sdk_session_id = jsonl_path.stem

    message_count = 0
    title: str | None = None
    real_cwd = ""
    parse_failed = False
    saw_any_json = False

    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                message_count += 1
                # title + real_cwd 都拿到后剩余只数行数
                if title is not None and real_cwd:
                    continue
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except (ValueError, json.JSONDecodeError):
                    parse_failed = True
                    continue
                saw_any_json = True
                if title is None:
                    title = _extract_title(entry)
                if not real_cwd:
                    real_cwd = _extract_cwd(entry)
    except OSError:
        return None

    if message_count == 0:
        return None

    if title is None:
        title = (
            _TITLE_BROKEN_PLACEHOLDER
            if (parse_failed and not saw_any_json)
            else _TITLE_EMPTY_PLACEHOLDER
        )

    return (
        SessionSummary(
            sdk_session_id=sdk_session_id,
            title=title,
            last_modified=last_modified,
            message_count=message_count,
        ),
        real_cwd,
    )


def _extract_title(entry: object) -> str | None:
    """若 entry 是合格的 user-text entry，返回截断后的 title；否则 None。

    合格条件：``entry.type == "user"`` 且 ``entry.message.content`` 是非空 string。
    （ccui 还会过滤一批 ``<command-name>`` / ``<system-reminder>`` 等系统消息，
    本 v0.2 第一轮先不过滤——保持最简语义；v0.3 可视体验再加。）
    """
    if not isinstance(entry, dict):
        return None
    if entry.get("type") != "user":
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    cleaned = content.replace("\n", " ").strip()
    if not cleaned:
        return None
    if len(cleaned) > _TITLE_MAX_LEN:
        cleaned = cleaned[:_TITLE_MAX_LEN]
    return cleaned


def _extract_cwd(entry: object) -> str:
    """从 jsonl entry 读 SDK 写的真实 ``cwd`` 字段。

    SDK 在大多数 entry 里都会带 ``cwd`` 字段（绝对路径），用作真值源；
    只有少数 entry（如某些纯 system 状态帧）可能缺。读到第一个非空就行。
    """
    if not isinstance(entry, dict):
        return ""
    cwd = entry.get("cwd")
    if isinstance(cwd, str) and cwd.startswith("/"):
        return cwd
    return ""


def _decode_cwd(encoded_name: str) -> str:
    """把编码目录名解码回 cwd 路径。

    规则（跟 ccui projects.js 一致）：所有 ``-`` 替换为 ``/``。

    例：``-Volumes-machub-app-proj-kongming-agent`` →
    ``/Volumes/machub/app/proj/kongming/agent``。

    边缘 case：原 cwd 路径里含 ``-`` 字符（如 ``/foo/bar-baz``）时，会被错
    还原成 ``/foo/bar/baz``。v0.2 接受此误差；v0.3 计划改进。
    """
    return encoded_name.replace("-", "/")
