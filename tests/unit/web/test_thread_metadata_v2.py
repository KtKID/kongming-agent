"""ThreadMetadata schema_version=2/3/4/5 + v1/v2/v3 兼容懒升级单测。

历史：v0.1.6 引入 backend_kind（v1→v2）；v0.2 引入 sdk_session_id+cwd（v2→v3）。
v0.2.1 引入累计 usage 字段（v3→v4）。v0.2.2 引入 codex_thread_id（v4→v5）。
v1 文件读盘时一次性升级到 v5（v1→v2→v3→v4→v5 连续懒升级）。

覆盖：

1. v5 默认值：schema_version=5、backend_kind="generic_chat"、sdk_session_id=""、cwd=""
2. v4 显式 backend_kind="claude_code"：preset_id 允许空字符串
3. 读盘 v1 文件（无 backend_kind） → 自动补 backend_kind+sdk_session_id+cwd+累计 usage+codex_thread_id 升 v5
4. 读盘 v4 文件 → 字段保真
5. 读盘 v4 + claude_code → 字段保真
6. 读盘未知 schema_version=99 → 返回 None
7. 写盘后再读盘字段保真
"""

from __future__ import annotations

from pathlib import Path

import pytest

from web.thread_metadata import (
    THREAD_METADATA_SCHEMA_VERSION,
    ThreadMetadata,
    read_thread_metadata,
    thread_metadata_path,
    write_thread_metadata,
)


def test_schema_version_constant_is_5() -> None:
    """v0.2.2 起常量为 5（v0.2.1 是 4）。"""
    assert THREAD_METADATA_SCHEMA_VERSION == 5


def test_default_construction_uses_v5_and_generic_chat() -> None:
    meta = ThreadMetadata(
        id="thread-aaaaaaaaaaaa",
        name="t",
        preset_id="p1",
        created_at=1.0,
        updated_at=1.0,
    )
    assert meta.schema_version == 5
    assert meta.backend_kind == "generic_chat"
    assert meta.preset_id == "p1"
    # v0.2/v0.2.1 新字段默认空 / 0
    assert meta.sdk_session_id == ""
    assert meta.cwd == ""
    assert meta.cumulative_prompt_tokens == 0
    assert meta.cumulative_completion_tokens == 0
    assert meta.cumulative_total_tokens == 0


def test_claude_code_allows_empty_preset_id() -> None:
    meta = ThreadMetadata(
        id="thread-aaaaaaaaaaaa",
        name="claude thread",
        preset_id="",
        backend_kind="claude_code",
        created_at=1.0,
        updated_at=1.0,
    )
    assert meta.backend_kind == "claude_code"
    assert meta.preset_id == ""


def test_read_v1_file_auto_upgrades_to_v4(tmp_path: Path) -> None:
    """v1 文件落盘后用 read_thread_metadata 读 → 连续懒升级 v1→v2→v3→v4。"""
    path = thread_metadata_path(tmp_path, "thread-aaaaaaaaaaaa")
    path.parent.mkdir(parents=True, exist_ok=True)
    # 模拟旧 v1 文件：缺 backend_kind，schema_version=1
    path.write_text(
        '{"id":"thread-aaaaaaaaaaaa","name":"old","preset_id":"p1",'
        '"created_at":1.0,"updated_at":2.0,"message_count":3,"schema_version":1}',
        encoding="utf-8",
    )
    loaded = read_thread_metadata(tmp_path, "thread-aaaaaaaaaaaa")
    assert loaded is not None
    assert loaded.backend_kind == "generic_chat"
    assert loaded.schema_version == 5
    assert loaded.sdk_session_id == ""
    assert loaded.cwd == ""
    assert loaded.cumulative_prompt_tokens == 0
    assert loaded.cumulative_completion_tokens == 0
    assert loaded.cumulative_total_tokens == 0
    assert loaded.preset_id == "p1"
    assert loaded.name == "old"
    assert loaded.message_count == 3


def test_read_v2_file_round_trip(tmp_path: Path) -> None:
    meta = ThreadMetadata(
        id="thread-bbbbbbbbbbbb",
        name="generic",
        preset_id="p1",
        backend_kind="generic_chat",
        created_at=1.0,
        updated_at=2.0,
        message_count=0,
    )
    write_thread_metadata(tmp_path, meta)
    loaded = read_thread_metadata(tmp_path, meta.id)
    assert loaded == meta


def test_read_v2_claude_code_round_trip(tmp_path: Path) -> None:
    meta = ThreadMetadata(
        id="thread-cccccccccccc",
        name="claude",
        preset_id="",
        backend_kind="claude_code",
        created_at=1.0,
        updated_at=2.0,
        message_count=0,
    )
    write_thread_metadata(tmp_path, meta)
    loaded = read_thread_metadata(tmp_path, meta.id)
    assert loaded is not None
    assert loaded.backend_kind == "claude_code"
    assert loaded.preset_id == ""


def test_read_unknown_schema_version_returns_none(tmp_path: Path) -> None:
    path = thread_metadata_path(tmp_path, "thread-dddddddddddd")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"id":"thread-dddddddddddd","name":"x","preset_id":"p",'
        '"created_at":1.0,"updated_at":1.0,"message_count":0,"schema_version":99}',
        encoding="utf-8",
    )
    assert read_thread_metadata(tmp_path, "thread-dddddddddddd") is None


def test_invalid_backend_kind_raises() -> None:
    with pytest.raises(Exception):  # pydantic ValidationError
        ThreadMetadata.model_validate(
            {
                "id": "thread-eeeeeeeeeeee",
                "name": "x",
                "preset_id": "p",
                "backend_kind": "unknown",
                "created_at": 1.0,
                "updated_at": 1.0,
                "message_count": 0,
            }
        )


def test_v1_file_with_backend_kind_already_present(tmp_path: Path) -> None:
    """边界：万一 schema_version=1 但已经有 backend_kind 字段（不该出现，但兜底接受）。"""
    path = thread_metadata_path(tmp_path, "thread-ffffffffffff")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"id":"thread-ffffffffffff","name":"old","preset_id":"p1",'
        '"backend_kind":"claude_code","created_at":1.0,"updated_at":2.0,'
        '"message_count":0,"schema_version":1}',
        encoding="utf-8",
    )
    loaded = read_thread_metadata(tmp_path, "thread-ffffffffffff")
    # 该文件包含 backend_kind 字段，懒升级逻辑只补缺字段，不改已有值；
    # schema_version 仍是 1（只在缺 backend_kind 时升 v2），随后模型接受并补齐默认字段。
    assert loaded is not None
    assert loaded.backend_kind == "claude_code"
