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
- ``display_name`` 直接取 ``os.path.basename(cwd)``——cwd 由 registry 提供，
  本身就是绝对路径真值，不再需要从 jsonl entry 反推。
- 不依赖 SDK，只 import 标准库

模块严格无副作用：纯函数，仅读 ``claude_home``，从不写入。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from web.claude_code.jsonl_history import encode_cwd

__all__ = [
    "ProjectSummary",
    "SessionSummary",
    "list_projects",
    "read_archived",
    "read_custom_title",
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
) -> list[ProjectSummary]:
    """按 registry 登记的 cwd 列表扫 ``~/.claude/projects/<encoded(cwd)>/``。

    Args:
        registry_cwds: 来自 ``claude_code.projects_registry.load_registry()`` 的
            cwd 列表（绝对路径）。每个 cwd 输出一个 ``ProjectSummary`` 节点。
            重复的 cwd 会去重（保留首次出现位置）。
        claude_home: Claude 数据目录，默认 ``Path.home() / ".claude"``。
            单元测试可注入临时目录。
        progress_callback: 流式刷新场景使用，签名 ``(current, total, name)``。

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
        project = _build_project_summary(cwd, encoded, project_path)
        summaries.append(project)
        if progress_callback is not None:
            progress_callback(index, total, encoded)

    summaries.sort(key=_project_sort_key, reverse=True)
    return summaries


def _project_sort_key(p: ProjectSummary) -> float:
    if not p.sessions:
        return 0.0
    return max(s.last_modified for s in p.sessions)


def _build_project_summary(cwd: str, encoded_name: str, project_path: Path) -> ProjectSummary:
    """收集单个 project 目录下的 session 摘要并打包。

    project_path 不存在 / 无权限 → 仍返回 ProjectSummary（``sessions=[]``）。
    这样 registry 登记后但还没产生过 jsonl 的项目也能正常展示，让用户从空白
    开始使用合法。
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
                    summary = _build_session_summary(Path(entry.path))
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


def read_custom_title(jsonl_path: Path) -> str | None:
    """从 jsonl 文件尾部读取最后一条 ``custom-title`` 条目的标题。

    CCUI 或 rename API 会追加 ``{"type":"custom-title","customTitle":"..."}``
    到文件末尾。本函数从尾部倒读最后 4KB，逐行倒序查找最后一条匹配记录。

    Args:
        jsonl_path: jsonl 文件路径。

    Returns:
        找到 → ``customTitle`` 字符串；找不到或文件打不开 → ``None``。
    """
    TAIL_BYTES = 4096
    try:
        with jsonl_path.open("rb") as f:
            f.seek(0, 2)  # seek to end
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    for line in reversed(tail.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except (ValueError, json.JSONDecodeError):
            continue
        if (
            isinstance(entry, dict)
            and entry.get("type") == "custom-title"
            and isinstance(entry.get("customTitle"), str)
        ):
            return str(entry["customTitle"])
    return None


def read_archived(jsonl_path: Path) -> bool:
    """从 jsonl 文件尾部读取最后一条 ``archived`` 条目的归档状态。

    archive API 会追加 ``{"type":"archived","archived":true}`` 到文件末尾。
    本函数从尾部倒读最后 4KB，逐行倒序查找最后一条匹配记录。

    Args:
        jsonl_path: jsonl 文件路径。

    Returns:
        找到 → ``archived`` 字段布尔值；找不到或文件打不开 → ``False``。
    """
    TAIL_BYTES = 4096
    try:
        with jsonl_path.open("rb") as f:
            f.seek(0, 2)  # seek to end
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return False

    for line in reversed(tail.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(entry, dict) and entry.get("type") == "archived":
            return bool(entry.get("archived", False))
    return False


def _build_session_summary(jsonl_path: Path) -> SessionSummary | None:
    """读取 jsonl 文件，统计行数 + 提取 title + 取 mtime。

    空 jsonl（0 行）→ 返回 None。
    无法 stat / 打开 → 返回 None（损坏到完全没法访问就跳过）。
    打开但 json 全损坏 → message_count 仍按行数算，title 用 ``(无法解析)``。

    title 优先级：custom-title（用户重命名） > 第 1 条 user message > 占位符。

    cwd 不再从 entry 反推——由 registry 提供绝对路径真值。
    """
    try:
        stat = jsonl_path.stat()
    except OSError:
        return None
    last_modified = stat.st_mtime

    claude_thread_id = jsonl_path.stem

    # 优先从尾部取 custom-title
    custom_title = read_custom_title(jsonl_path)

    # 已归档的 session 不展示
    if read_archived(jsonl_path):
        return None

    message_count = 0
    title: str | None = custom_title
    parse_failed = False
    saw_any_json = False

    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                message_count += 1
                # title 拿到后剩余只数行数
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
