"""Workspace 文件与 shell 共用辅助。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from web.thread_metadata import ThreadMetadata

WORKSPACE_MAX_TEXT_FILE_BYTES = 256 * 1024
WORKSPACE_TREE_DEFAULT_LIMIT = 200


class WorkspaceError(ValueError):
    """workspace 相关输入或状态错误。"""


def get_thread_meta(thread_id: str, metas: list[ThreadMetadata]) -> ThreadMetadata | None:
    """从 thread metadata 列表中定位单条记录。"""
    return next((meta for meta in metas if meta.id == thread_id), None)


def require_workspace_root(meta: ThreadMetadata) -> Path:
    """要求 thread 已绑定有效 workspace root。"""
    cwd = meta.cwd.strip()
    if not cwd:
        raise WorkspaceError("thread has no workspace cwd")
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
