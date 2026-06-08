"""``src/web/workspace.py`` 纯 helper 单元测试。

覆盖 ``resolve_workspace_cwd`` 与 ``require_workspace_root`` 的
``fallback_to`` 行为；与 endpoint 集成测试分离，确保 helper 在不起 FastAPI
client 的情况下也能独立回归。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from web.threads.metadata import ThreadMetadata
from web.workspace import (
    WorkspaceError,
    require_workspace_root,
    resolve_workspace_cwd,
)


def _make_meta(cwd: str) -> ThreadMetadata:
    """构造测试用 ThreadMetadata，仅关心 ``cwd`` 字段。"""
    return ThreadMetadata(
        id="thread-000000000001",
        name="t",
        preset_id="",
        backend_kind="generic_chat",
        cwd=cwd,
        created_at=1.0,
        updated_at=2.0,
        message_count=0,
    )


# ---- resolve_workspace_cwd ----------------------------------------------


def test_resolve_workspace_cwd_with_meta_cwd(tmp_path: Path) -> None:
    """meta.cwd 非空时直接返回 meta.cwd，不走 fallback。"""
    meta = _make_meta("/foo/bar")
    server_root = tmp_path / "srv"
    server_root.mkdir()
    assert resolve_workspace_cwd(meta, server_root) == "/foo/bar"


def test_resolve_workspace_cwd_empty_falls_back(tmp_path: Path) -> None:
    """meta.cwd 空字符串 → fallback 到 server_workspace_root。"""
    meta = _make_meta("")
    server_root = tmp_path / "srv"
    server_root.mkdir()
    assert resolve_workspace_cwd(meta, server_root) == str(server_root)


def test_resolve_workspace_cwd_whitespace_falls_back(tmp_path: Path) -> None:
    """meta.cwd 全空白 → strip 后空 → fallback。"""
    meta = _make_meta("   ")
    server_root = tmp_path / "srv"
    server_root.mkdir()
    assert resolve_workspace_cwd(meta, server_root) == str(server_root)


# ---- require_workspace_root ---------------------------------------------


def test_require_workspace_root_default_raises() -> None:
    """meta.cwd 空 + 不传 fallback_to → 抛 WorkspaceError（向后兼容）。"""
    meta = _make_meta("")
    with pytest.raises(WorkspaceError, match="thread has no workspace cwd"):
        require_workspace_root(meta)


def test_require_workspace_root_fallback_used(tmp_path: Path) -> None:
    """meta.cwd 空 + 传 fallback_to=tmp_path → 返回 fallback 路径。"""
    meta = _make_meta("")
    fallback = tmp_path / "srv"
    fallback.mkdir()
    root = require_workspace_root(meta, fallback_to=fallback)
    assert root == fallback.resolve()


def test_require_workspace_root_meta_cwd_priority(tmp_path: Path) -> None:
    """meta.cwd 有效 + 同时传 fallback_to → 优先 meta.cwd，不走 fallback。"""
    real = tmp_path / "thread_cwd"
    real.mkdir()
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    meta = _make_meta(str(real))
    root = require_workspace_root(meta, fallback_to=fallback)
    assert root == real.resolve()


def test_require_workspace_root_fallback_invalid_raises(tmp_path: Path) -> None:
    """meta.cwd 空 + fallback 也不存在 → 抛 WorkspaceError（不 silently 失败）。"""
    meta = _make_meta("")
    missing = tmp_path / "does_not_exist"
    with pytest.raises(WorkspaceError, match="workspace root not found"):
        require_workspace_root(meta, fallback_to=missing)


def test_require_workspace_root_fallback_is_file_raises(tmp_path: Path) -> None:
    """meta.cwd 空 + fallback 是文件而非目录 → 抛 WorkspaceError。"""
    meta = _make_meta("")
    fake = tmp_path / "afile.txt"
    fake.write_text("x", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="not a directory"):
        require_workspace_root(meta, fallback_to=fake)
