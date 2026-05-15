"""用例 25: ThreadMetadata schema v4 → v6 懒升级。"""

from __future__ import annotations

import json
from pathlib import Path

from web.thread_metadata import (
    THREAD_METADATA_SCHEMA_VERSION,
    ThreadMetadata,
    read_thread_metadata,
)

_THREAD_ID = "thread-aabbccddeeff"

_V4_DATA: dict = {
    "id": _THREAD_ID,
    "name": "test thread",
    "preset_id": "",
    "backend_kind": "claude_code",
    "sdk_session_id": "abc-123",
    "cwd": "/test/path",
    "created_at": 1000.0,
    "updated_at": 2000.0,
    "message_count": 5,
    "cumulative_prompt_tokens": 100,
    "cumulative_completion_tokens": 50,
    "cumulative_total_tokens": 150,
    "schema_version": 4,
}


def _write_v4_metadata(home: Path) -> Path:
    """在 tmp_path 下写一份 v4 metadata.json，返回文件路径。"""
    meta_dir = home / "web" / "threads" / _THREAD_ID
    meta_dir.mkdir(parents=True, exist_ok=True)
    path = meta_dir / "metadata.json"
    path.write_text(json.dumps(_V4_DATA), encoding="utf-8")
    return path


# ── v4 → v6 懒升级 ──────────────────────────────────────────


class TestSchemaV4ToV6Upgrade:
    """用例 25: v4 → v6 懒升级。"""

    def test_v4_file_upgraded_to_v9(self, tmp_path: Path) -> None:
        _write_v4_metadata(tmp_path)
        meta = read_thread_metadata(tmp_path, _THREAD_ID)
        assert meta is not None
        # v4 → v5 → v6 → v7 → v8 → v9（usage-token-v2-bigbang 加链末）
        assert meta.schema_version == 9
        assert meta.codex_thread_id == ""

    def test_v4_fields_preserved(self, tmp_path: Path) -> None:
        _write_v4_metadata(tmp_path)
        meta = read_thread_metadata(tmp_path, _THREAD_ID)
        assert meta is not None
        assert meta.id == _THREAD_ID
        assert meta.name == "test thread"
        assert meta.backend_kind == "claude_code"
        assert meta.claude_thread_id == "abc-123"
        assert meta.cwd == "/test/path"
        assert meta.created_at == 1000.0
        assert meta.updated_at == 2000.0
        assert meta.message_count == 5
        # v9 (usage-token-v2-bigbang): token 字段已物理删除（cumulative_usage 等
        # 都从 ThreadMetadata 移除；token 真源回归 SDK jsonl）
        assert not hasattr(meta, "cumulative_usage")


# ── codex backend_kind + codex_thread_id 字段 ───────────────


class TestCodexBackendKind:
    def test_codex_backend_kind_valid(self) -> None:
        meta = ThreadMetadata(
            id=_THREAD_ID,
            name="codex thread",
            backend_kind="codex",
            created_at=1000.0,
            updated_at=2000.0,
        )
        assert meta.backend_kind == "codex"

    def test_codex_thread_id_field(self) -> None:
        meta = ThreadMetadata(
            id=_THREAD_ID,
            name="codex thread",
            codex_thread_id="019dee38-c689",
            created_at=1000.0,
            updated_at=2000.0,
        )
        assert meta.codex_thread_id == "019dee38-c689"


# ── schema_version 常量 ─────────────────────────────────────


class TestSchemaVersion:
    def test_current_version_is_9(self) -> None:
        assert THREAD_METADATA_SCHEMA_VERSION == 9
