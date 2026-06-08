"""Workspace 文件与 shell 共用辅助。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from web.threads.metadata import ThreadMetadata

WORKSPACE_MAX_TEXT_FILE_BYTES = 256 * 1024
WORKSPACE_TREE_DEFAULT_LIMIT = 200


class WorkspaceError(ValueError):
    """workspace 相关输入或状态错误。"""


def get_thread_meta(thread_id: str, metas: list[ThreadMetadata]) -> ThreadMetadata | None:
    """从 thread metadata 列表中定位单条记录。"""
    return next((meta for meta in metas if meta.id == thread_id), None)


def resolve_workspace_cwd(meta: ThreadMetadata, server_workspace_root: Path) -> str:
    """thread.cwd 空时 fallback 到 server 启动目录。

    用户心智："纯聊天 thread 没填 cwd，默认走 server 启动路径"。
    所有需要 cwd 的链路（approval / Zap UI / files / shell）统一走这个 helper，
    保证全链路一致。

    Args:
        meta: thread metadata（含 ``cwd`` 字段）。
        server_workspace_root: server 启动目录（通常是
            ``app.state.workspace_root``）。

    Returns:
        ``meta.cwd`` 非空（strip 后）直接返回；否则返回
        ``server_workspace_root`` 的正斜杠字符串。
    """
    cwd = meta.cwd.strip()
    if cwd:
        return cwd
    return server_workspace_root.as_posix()


def require_workspace_root(
    meta: ThreadMetadata,
    *,
    fallback_to: Path | None = None,
) -> Path:
    """要求 thread 已绑定有效 workspace root，可选 fallback 到 server 启动目录。

    默认行为（``fallback_to=None``）与历史一致：``meta.cwd`` 空字符串直接抛
    :class:`WorkspaceError`。

    调用方按需传 ``fallback_to=app.state.workspace_root`` 启用 fallback：
    ``meta.cwd`` 空时改用 ``fallback_to``；若 ``fallback_to`` 也不存在 / 不是目录
    同样抛 :class:`WorkspaceError`，不会再 silently 继续。

    Args:
        meta: thread metadata。
        fallback_to: 可选 fallback 路径，必须以关键字方式传入（kw-only）；
            ``None`` 表示不启用 fallback，保持向后兼容。

    Returns:
        已解析（``expanduser`` + ``resolve``）的 workspace 根目录绝对路径。

    Raises:
        WorkspaceError: thread 无 cwd 且未提供（或 fallback 路径无效）的情况。
    """
    cwd = meta.cwd.strip()
    if not cwd:
        if fallback_to is None:
            raise WorkspaceError("thread has no workspace cwd")
        cwd = str(fallback_to)
    root = Path(cwd).expanduser().resolve()
    if not root.exists():
        raise WorkspaceError(f"workspace root not found: {root}")
    if not root.is_dir():
        raise WorkspaceError(f"workspace root is not a directory: {root}")
    return root


def normalize_relative_path(path: str) -> str:
    """规范化相对路径。"""
    cleaned = path.strip().replace("\\", "/").lstrip("/")
    if cleaned in {"", "."}:
        return ""
    parts = [part for part in cleaned.split("/") if part and part != "."]
    normalized = "/".join(parts)
    if normalized.startswith("..") or "/../" in f"/{normalized}/":
        raise WorkspaceError("workspace path escapes root")
    return normalized


def resolve_workspace_path(root: Path, relative_path: str) -> Path:
    """将相对路径解析为 workspace 内绝对路径。"""
    rel = normalize_relative_path(relative_path)
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise WorkspaceError("workspace path escapes root") from exc
    return target


def detect_entry_kind(path: Path) -> Literal["file", "dir"]:
    """把路径归类成 file / dir。"""
    return "dir" if path.is_dir() else "file"


def list_workspace_entries(
    root: Path,
    relative_path: str,
    *,
    limit: int = WORKSPACE_TREE_DEFAULT_LIMIT,
) -> list[dict[str, object]]:
    """列出目录直接子项。"""
    target = resolve_workspace_path(root, relative_path)
    if not target.exists():
        raise FileNotFoundError(f"workspace path not found: {target}")
    if not target.is_dir():
        raise NotADirectoryError(f"workspace path is not a directory: {target}")

    entries: list[dict[str, object]] = []
    children = sorted(
        target.iterdir(),
        key=lambda child: (0 if child.is_dir() else 1, child.name.lower()),
    )
    for index, child in enumerate(children):
        if index >= limit:
            break
        rel_child = child.relative_to(root).as_posix()
        has_children = False
        if child.is_dir():
            try:
                next(child.iterdir())
                has_children = True
            except StopIteration:
                has_children = False
        entries.append(
            {
                "path": rel_child,
                "name": child.name,
                "kind": detect_entry_kind(child),
                "has_children": has_children,
            }
        )
    return entries


def read_workspace_text_file(root: Path, relative_path: str) -> dict[str, object]:
    """读取 workspace 文本文件。"""
    target = resolve_workspace_path(root, relative_path)
    if not target.exists():
        raise FileNotFoundError(f"workspace file not found: {target}")
    if not target.is_file():
        raise IsADirectoryError(f"workspace path is not a file: {target}")

    raw = target.read_bytes()
    size_bytes = len(raw)
    if size_bytes > WORKSPACE_MAX_TEXT_FILE_BYTES:
        return {
            "path": normalize_relative_path(relative_path),
            "name": target.name,
            "content": "",
            "size_bytes": size_bytes,
            "is_text": False,
            "too_large": True,
            "encoding": "utf-8",
        }

    if b"\x00" in raw:
        return {
            "path": normalize_relative_path(relative_path),
            "name": target.name,
            "content": "",
            "size_bytes": size_bytes,
            "is_text": False,
            "too_large": False,
            "encoding": "utf-8",
        }

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "path": normalize_relative_path(relative_path),
            "name": target.name,
            "content": "",
            "size_bytes": size_bytes,
            "is_text": False,
            "too_large": False,
            "encoding": "utf-8",
        }

    return {
        "path": normalize_relative_path(relative_path),
        "name": target.name,
        "content": content,
        "size_bytes": size_bytes,
        "is_text": True,
        "too_large": False,
        "encoding": "utf-8",
    }


def write_workspace_text_file(root: Path, relative_path: str, content: str) -> dict[str, object]:
    """写回 workspace 文本文件。"""
    target = resolve_workspace_path(root, relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8")
    if len(encoded) > WORKSPACE_MAX_TEXT_FILE_BYTES:
        raise WorkspaceError("workspace file exceeds 256 KiB limit")
    target.write_bytes(encoded)
    return {
        "path": normalize_relative_path(relative_path),
        "name": target.name,
        "content": content,
        "size_bytes": len(encoded),
        "is_text": True,
        "too_large": False,
        "encoding": "utf-8",
    }
