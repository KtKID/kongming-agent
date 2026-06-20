"""ThreadMetadata schema_version=11 + v9→v11 兼容懒升级单测。

claude-session-rename-archive-metadata-source 引入 ``is_archived: bool`` 字段，
把归档真源从 jsonl ``archived`` 事件迁到 thread metadata.json。

覆盖：

1. v9 文件懒升级到 v10，``is_archived`` 默认 False
2. v9 文件 + 已含 ``is_archived`` 字段（迁移脚本中间态）→ 保留值
3. ``is_archived=True`` 持久化往返
4. v10 默认值（构造未传时 False）
5. ``THREAD_METADATA_SCHEMA_VERSION == 11``
"""

from __future__ import annotations

import json
from pathlib import Path

from hosts.web.threads.metadata import (
    THREAD_METADATA_SCHEMA_VERSION,
    ThreadMetadata,
    read_thread_metadata,
    thread_metadata_path,
    write_thread_metadata,
)

_THREAD_ID = "thread-abcdef012345"

# v9 文件形态（usage-token-v2-bigbang 之后的产物）：无 is_archived 字段
_V9_DATA: dict = {
    "id": _THREAD_ID,
    "name": "v9 thread",
    "preset_id": "",
    "backend_kind": "claude_code",
    "claude_thread_id": "uuid-v9",
    "codex_thread_id": "",
    "cwd": "/some/path",
    "created_at": 1000.0,
    "updated_at": 2000.0,
    "message_count": 7,
    "is_pinned": False,
    "schema_version": 9,
}


def _write_v9_metadata(home: Path, data: dict | None = None) -> Path:
    """在 tmp_path 下写一份 v9 metadata.json，返回文件路径。"""
    payload = data if data is not None else _V9_DATA
    meta_dir = home / "web" / "threads" / payload["id"]
    meta_dir.mkdir(parents=True, exist_ok=True)
    path = meta_dir / "metadata.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ── 常量 ────────────────────────────────────────────────────


def test_schema_version_constant_is_11() -> None:
    assert THREAD_METADATA_SCHEMA_VERSION == 11


# ── v9 → v10 lazy upgrade ──────────────────────────────────


def test_v9_file_upgraded_to_v11_default_not_archived(tmp_path: Path) -> None:
    """v9 文件读出 → schema_version=11、is_archived=False（默认）。"""
    _write_v9_metadata(tmp_path)
    meta = read_thread_metadata(tmp_path, _THREAD_ID)
    assert meta is not None
    assert meta.schema_version == 11
    assert meta.is_archived is False
    # 其它字段保真
    assert meta.id == _THREAD_ID
    assert meta.name == "v9 thread"
    assert meta.backend_kind == "claude_code"
    assert meta.claude_thread_id == "uuid-v9"
    assert meta.cwd == "/some/path"
    assert meta.message_count == 7


def test_v9_file_with_explicit_is_archived_true_preserved(tmp_path: Path) -> None:
    """v9 文件已含 is_archived=True（迁移脚本写入的中间态）→ 保留值。"""
    data = dict(_V9_DATA)
    data["id"] = "thread-111122223333"
    data["is_archived"] = True
    _write_v9_metadata(tmp_path, data)
    meta = read_thread_metadata(tmp_path, "thread-111122223333")
    assert meta is not None
    assert meta.schema_version == 11
    assert meta.is_archived is True


# ── v10 默认 + round-trip ──────────────────────────────────


def test_default_construction_is_not_archived() -> None:
    """v11 默认构造未传 is_archived → False。"""
    meta = ThreadMetadata(
        id="thread-cafebabe0001",
        name="t",
        preset_id="",
        backend_kind="claude_code",
        created_at=1.0,
        updated_at=1.0,
    )
    assert meta.schema_version == 11
    assert meta.is_archived is False


def test_is_archived_true_round_trip(tmp_path: Path) -> None:
    """显式归档 → 写盘 → 读盘字段保真。"""
    meta = ThreadMetadata(
        id="thread-cafebabe0002",
        name="archived thread",
        preset_id="",
        backend_kind="claude_code",
        cwd="/x",
        claude_thread_id="uuid-x",
        created_at=1.0,
        updated_at=2.0,
        message_count=3,
        is_archived=True,
    )
    write_thread_metadata(tmp_path, meta)
    loaded = read_thread_metadata(tmp_path, meta.id)
    assert loaded is not None
    assert loaded.is_archived is True
    assert loaded.schema_version == 11


def test_write_v10_then_read_back_preserves_not_archived(tmp_path: Path) -> None:
    """默认未归档 → 写盘 → 读出仍未归档。"""
    meta = ThreadMetadata(
        id="thread-cafebabe0003",
        name="normal thread",
        preset_id="",
        backend_kind="claude_code",
        created_at=1.0,
        updated_at=2.0,
    )
    write_thread_metadata(tmp_path, meta)
    raw = (tmp_path / "web" / "threads" / meta.id / "metadata.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    # 落盘文件里 is_archived=False 字段必须存在（pydantic dump 默认导出）
    assert data["is_archived"] is False
    assert data["schema_version"] == 11
    loaded = read_thread_metadata(tmp_path, meta.id)
    assert loaded is not None
    assert loaded.is_archived is False


# ── 未知更高版本 ────────────────────────────────────────────


def test_read_v12_returns_none(tmp_path: Path) -> None:
    """前向兼容拒绝：v12 文件（未来版本）返回 None。"""
    path = thread_metadata_path(tmp_path, "thread-feedface0001")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(_V9_DATA)
    payload["id"] = "thread-feedface0001"
    payload["schema_version"] = 12
    payload["is_archived"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert read_thread_metadata(tmp_path, "thread-feedface0001") is None
