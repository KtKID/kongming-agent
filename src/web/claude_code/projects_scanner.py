"""按 registry 登记的 cwd 列表扫描 ``~/.claude/projects/``，列出会话摘要。

公共入口 :func:`list_projects` **以 registry 登记的 cwd 列表为权威输入**，逐个
扫 ``~/.claude/projects/<encoded(cwd)>/`` 拿 sessions。返回 :class:`ProjectSummary`
列表，每个 project 含若干 :class:`SessionSummary`（按 ``last_modified`` 倒序）。

设计要点（钉死，跟 v0.1 web-projects-registry 任务包 D1 决议对齐）：

- **registry-driven**：不再 ``iterdir`` 全局扫 ``~/.claude/projects/``；项目
  必须先在 registry 注册才会出现。这样 worktree A / worktree B 可以独立维护
  各自的项目列表，避免互相污染。
- **目录不存在不丢节点**：cwd 对应的 ``~/.claude/projects/<encoded>/`` 不存在
  时仍返回该 project 节点（``sessions=[]``），让用户从空白开始用合法。
- 跳过 ``agent-*.jsonl``（subagent 工具历史，v0.2 不展示）
- 行数为 0 的空 jsonl 跳过（不输出 SessionSummary）
- 损坏 / 完全无 user message 的 jsonl 仍然计入：``title`` 取占位文案
- ``display_name`` 直接取 ``os.path.basename(cwd)``——cwd 由 registry 提供,
  本身就是绝对路径真值，不再需要从 jsonl entry 反推。
- 不依赖 SDK，只 import 标准库
- **title / archived 真源 = thread metadata**：调用方按 ``claude_thread_id``
  组装 ``{claude_thread_id → ThreadMetadata}`` 索引并传入；scanner 命中即用
  ``meta.name`` / ``meta.is_archived``。未命中（未绑定 thread 的孤儿 jsonl）
  走原 fallback：扫首条 user message。这一改动修掉旧 "尾部 4KB 窗口" bug——
  Claude jsonl 单行 P90=7KB / P99=285KB，原 ``read_custom_title`` /
  ``read_archived`` 一旦 rename 后又写了大消息就读不到真值。

模块严格无副作用：纯函数，仅读 ``claude_home``，从不写入。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from web.claude_code.jsonl_history import encode_cwd
from web.thread_metadata import ThreadMetadata

__all__ = [
    "ProjectSummary",
    "SessionSummary",
    "list_projects",
]


_TITLE_MAX_LEN = 40
_TITLE_EMPTY_PLACEHOLDER = "(空会话)"
_TITLE_BROKEN_PLACEHOLDER = "(无法解析)"


@dataclass(frozen=True)
class SessionSummary:
    """单个 SDK session（jsonl 文件）的摘要。"""

    claude_thread_id: str
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
    """单个 project 的摘要。"""

    name: str
    """编码目录名（如 ``-Volumes-machub-app-proj-kongming-agent``），由 cwd 通过
    SDK 编码规则计算得出。"""

    cwd: str
    """registry 登记的 cwd 原文，绝对路径真值。"""

    display_name: str
    """``os.path.basename(cwd)``（如 ``"kongming-agent"``）。"""

    sessions: list[SessionSummary]
    """按 ``last_modified`` 倒序的 session 摘要列表；目录不存在时为空列表。"""


def list_projects(
    registry_cwds: list[str],
    *,
    claude_home: Path | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
    thread_metadata_index: dict[str, ThreadMetadata] | None = None,
) -> list[ProjectSummary]:
    """按 registry 登记的 cwd 列表扫 ``~/.claude/projects/<encoded(cwd)>/``。

    Args:
        registry_cwds: 来自 ``claude_code.projects_registry.load_registry()`` 的
            cwd 列表（绝对路径）。每个 cwd 输出一个 ``ProjectSummary`` 节点。
            重复的 cwd 会去重（保留首次出现位置)。
        claude_home: Claude 数据目录，默认 ``Path.home() / ".claude"``。
            单元测试可注入临时目录。
        progress_callback: 流式刷新场景使用，签名 ``(current, total, name)``。
        thread_metadata_index: ``{claude_thread_id → ThreadMetadata}`` 索引，
            由 router 层用 :func:`web.thread_metadata.list_thread_metadata`
            构建后传入。命中的 jsonl 直接用 ``meta.name`` 当 title，并按
            ``meta.is_archived`` 过滤归档项。未命中走原 fallback（首条 user
            message → 占位符）。``None`` 表示调用方未提供索引（保持后兼容），
            等同空 dict。

    Returns:
        ProjectSummary 列表，**每个 cwd 一个节点**：

        - cwd 对应的 ``~/.claude/projects/<encoded>/`` 目录存在 → 扫 jsonl 拿 sessions
        - 目录不存在 / 无权限 → ``sessions=[]``，节点仍保留（用户从空开始用合法）

        排序：按各 project 内最新 session 的 ``last_modified`` 倒序；空列表的
        project 用 ``0.0`` 自然垫底。
    """
    home = claude_home if claude_home is not None else Path.home() / ".claude"
    projects_dir = home / "projects"

    # registry_cwds 去重（保留首次出现顺序）
    seen: set[str] = set()
    deduped_cwds: list[str] = []
    for cwd in registry_cwds:
        if cwd in seen:
            continue
        seen.add(cwd)
        deduped_cwds.append(cwd)

    summaries: list[ProjectSummary] = []
    total = len(deduped_cwds)
    for index, cwd in enumerate(deduped_cwds, start=1):
        encoded = encode_cwd(cwd)
        project_path = projects_dir / encoded
        project = _build_project_summary(
            cwd,
            encoded,
            project_path,
            thread_metadata_index=thread_metadata_index,
        )
        summaries.append(project)
        if progress_callback is not None:
            progress_callback(index, total, encoded)

    summaries.sort(key=_project_sort_key, reverse=True)
    return summaries


def _project_sort_key(p: ProjectSummary) -> float:
    if not p.sessions:
        return 0.0
    return max(s.last_modified for s in p.sessions)


def _build_project_summary(
    cwd: str,
    encoded_name: str,
    project_path: Path,
    *,
    thread_metadata_index: dict[str, ThreadMetadata] | None = None,
) -> ProjectSummary:
    """收集单个 project 目录下的 session 摘要并打包。

    project_path 不存在 / 无权限 → 仍返回 ProjectSummary（``sessions=[]``）。
    这样 registry 登记后但还没产生过 jsonl 的项目也能正常展示，让用户从空白
    开始使用合法。

    ``thread_metadata_index`` 透传给 ``_build_session_summary``：scanner 不
    在层级 1 解释索引，统一在 session 级别按 ``claude_thread_id`` 查询。
    """
    sessions: list[SessionSummary] = []
    if project_path.is_dir():
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
                    summary = _build_session_summary(
                        Path(entry.path),
                        thread_metadata_index=thread_metadata_index,
                    )
                    if summary is not None:
                        sessions.append(summary)
        except OSError:
            # 中途被删 / 权限丢失 → 当作空列表，节点仍保留
            sessions = []

    sessions.sort(key=lambda s: s.last_modified, reverse=True)

    display_name = os.path.basename(cwd) or cwd
    return ProjectSummary(
        name=encoded_name,
        cwd=cwd,
        display_name=display_name,
        sessions=sessions,
    )


def _build_session_summary(
    jsonl_path: Path,
    *,
    thread_metadata_index: dict[str, ThreadMetadata] | None = None,
) -> SessionSummary | None:
    """读取 jsonl 文件，统计行数 + 提取 title + 取 mtime。

    空 jsonl（0 行）→ 返回 None。
    无法 stat / 打开 → 返回 None（损坏到完全没法访问就跳过）。
    打开但 json 全损坏 → message_count 仍按行数算，title 用 ``(无法解析)``。

    title 优先级：
    1. ``thread_metadata_index[claude_thread_id].name`` —— 用户绑定 thread
       后元数据落在 ``.kongming/web/threads/<tid>/metadata.json``，是 rename
       的真源。命中即用，跳过 jsonl 扫描（只用来数行数）。
    2. fallback：未绑定 thread 的孤儿 jsonl，扫第 1 条 user message。
    3. 双双没有 → 占位符 ``(空会话)`` / ``(无法解析)``。

    archived 真源同样在 metadata 里：``meta.is_archived=True`` → 返回 None
    （列表不显示）。未命中 thread metadata 默认视为未归档。

    cwd 不再从 entry 反推——由 registry 提供绝对路径真值。
    """
    try:
        stat = jsonl_path.stat()
    except OSError:
        return None
    last_modified = stat.st_mtime

    claude_thread_id = jsonl_path.stem

    # thread metadata 索引：title / archived 真源
    meta = (
        thread_metadata_index.get(claude_thread_id) if thread_metadata_index is not None else None
    )

    # 已归档（按 metadata）的 session 不展示
    if meta is not None and meta.is_archived:
        return None

    message_count = 0
    # 命中 metadata：直接用 meta.name 当 title，scan 阶段只数行数不解析
    title: str | None = meta.name if meta is not None else None
    parse_failed = False
    saw_any_json = False

    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                message_count += 1
                # title 已确定（meta 命中 / 已找到首条 user msg）→ 剩余只数行数
                if title is not None:
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
                title = _extract_title(entry)
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

    return SessionSummary(
        claude_thread_id=claude_thread_id,
        title=title,
        last_modified=last_modified,
        message_count=message_count,
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
