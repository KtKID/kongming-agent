"""ThreadMetadata 持久化层单测（Phase 2 #18）。

覆盖：
- write_thread_metadata 原子写 + 父目录自动创建
- read_thread_metadata 读盘 + 返回 None 路径（不存在 / 损坏 JSON / schema 不匹配）
- list_thread_metadata 排序 + 缺目录返回 [] + 损坏目录跳过
- delete_thread_metadata_dir 幂等 + 删空目录
- ThreadMetadata 字段校验（thread_id 正则 / name 长度上限）

用 ``tmp_path`` fixture 隔离写盘，不依赖真实 ``.kongming/``。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from hosts.web.threads.metadata import (
    THREAD_METADATA_SCHEMA_VERSION,
    ThreadMetadata,
    delete_thread_metadata_dir,
    list_thread_metadata,
    read_thread_metadata,
    thread_metadata_dir,
    thread_metadata_path,
    write_thread_metadata,
)


def _make_meta(thread_id: str = "thread-aaaaaaaaaaaa", **overrides: object) -> ThreadMetadata:
    base: dict[str, object] = {
        "id": thread_id,
        "name": "demo",
        "preset_id": "claude-sonnet-4",
        "created_at": 1700000000.0,
        "updated_at": 1700000100.0,
        "message_count": 3,
    }
    base.update(overrides)
    return ThreadMetadata.model_validate(base)


# ---------------------------------------------------------------------------
# 模型字段校验
# ---------------------------------------------------------------------------


def test_thread_metadata_id_must_match_pattern() -> None:
    with pytest.raises(ValidationError):
        ThreadMetadata.model_validate(
            {
                "id": "thread-INVALID",  # 非小写 hex
                "name": "x",
                "preset_id": "p",
                "created_at": 1.0,
                "updated_at": 1.0,
                "message_count": 0,
            }
        )


def test_thread_metadata_id_length_must_be_12() -> None:
    with pytest.raises(ValidationError):
        ThreadMetadata.model_validate(
            {
                "id": "thread-abc",  # 仅 3 位
                "name": "x",
                "preset_id": "p",
                "created_at": 1.0,
                "updated_at": 1.0,
                "message_count": 0,
            }
        )


def test_thread_metadata_name_max_200() -> None:
    with pytest.raises(ValidationError):
        ThreadMetadata.model_validate(
            {
                "id": "thread-aaaaaaaaaaaa",
                "name": "x" * 201,
                "preset_id": "p",
                "created_at": 1.0,
                "updated_at": 1.0,
                "message_count": 0,
            }
        )


def test_thread_metadata_default_schema_version() -> None:
    meta = _make_meta()
    assert meta.schema_version == THREAD_METADATA_SCHEMA_VERSION == 10


def test_thread_metadata_message_count_non_negative() -> None:
    with pytest.raises(ValidationError):
        ThreadMetadata.model_validate(
            {
                "id": "thread-aaaaaaaaaaaa",
                "name": "x",
                "preset_id": "p",
                "created_at": 1.0,
                "updated_at": 1.0,
                "message_count": -1,
            }
        )


# ---------------------------------------------------------------------------
# write/read round-trip
# ---------------------------------------------------------------------------


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    assert thread_metadata_path(tmp_path, meta.id).is_file()
    loaded = read_thread_metadata(tmp_path, meta.id)
    assert loaded == meta


def test_write_creates_parent_dirs(tmp_path: Path) -> None:
    meta = _make_meta()
    # parents 不存在
    assert not (tmp_path / "web").exists()
    write_thread_metadata(tmp_path, meta)
    assert thread_metadata_path(tmp_path, meta.id).is_file()


def test_write_is_atomic_via_temp_file(tmp_path: Path) -> None:
    """写完后不应留下 .tmp 中间文件。"""
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    final = thread_metadata_path(tmp_path, meta.id)
    tmp = final.with_suffix(".json.tmp")
    assert final.is_file()
    assert not tmp.exists()


def test_write_overwrites_existing(tmp_path: Path) -> None:
    meta1 = _make_meta(name="old")
    write_thread_metadata(tmp_path, meta1)
    meta2 = _make_meta(name="new", updated_at=1700000200.0)
    write_thread_metadata(tmp_path, meta2)
    loaded = read_thread_metadata(tmp_path, meta1.id)
    assert loaded is not None
    assert loaded.name == "new"
    assert loaded.updated_at == 1700000200.0


# ---------------------------------------------------------------------------
# read None 路径
# ---------------------------------------------------------------------------


def test_read_missing_returns_none(tmp_path: Path) -> None:
    assert read_thread_metadata(tmp_path, "thread-aaaaaaaaaaaa") is None


def test_read_corrupted_json_returns_none(tmp_path: Path) -> None:
    path = thread_metadata_path(tmp_path, "thread-aaaaaaaaaaaa")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert read_thread_metadata(tmp_path, "thread-aaaaaaaaaaaa") is None


def test_read_invalid_schema_returns_none(tmp_path: Path) -> None:
    path = thread_metadata_path(tmp_path, "thread-aaaaaaaaaaaa")
    path.parent.mkdir(parents=True, exist_ok=True)
    # 缺 name（仍是必填，min_length=1）；v0.1.6 后 preset_id 改为可选默认 ""，
    # 不能再用它做 invalid 用例。
    path.write_text(
        '{"id":"thread-aaaaaaaaaaaa","preset_id":"p","created_at":1.0,"updated_at":1.0,'
        '"message_count":0,"schema_version":1}',
        encoding="utf-8",
    )
    assert read_thread_metadata(tmp_path, "thread-aaaaaaaaaaaa") is None


def test_read_wrong_schema_version_returns_none(tmp_path: Path) -> None:
    path = thread_metadata_path(tmp_path, "thread-aaaaaaaaaaaa")
    path.parent.mkdir(parents=True, exist_ok=True)
    # schema_version=99 是未知；Literal[1] 应拒绝
    path.write_text(
        '{"id":"thread-aaaaaaaaaaaa","name":"x","preset_id":"p",'
        '"created_at":1.0,"updated_at":1.0,"message_count":0,"schema_version":99}',
        encoding="utf-8",
    )
    assert read_thread_metadata(tmp_path, "thread-aaaaaaaaaaaa") is None


def test_read_v3_lazy_upgrades_through_v9(tmp_path: Path) -> None:
    """v3 → v4 → ... → v8 → v9 全链路懒升级；v9 drop 3 个 token 字段。"""
    path = thread_metadata_path(tmp_path, "thread-aaaaaaaaaaaa")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"id":"thread-aaaaaaaaaaaa","name":"x","preset_id":"p",'
        '"backend_kind":"generic_chat","sdk_session_id":"","cwd":"",'
        '"created_at":1.0,"updated_at":2.0,"message_count":3,"schema_version":3}',
        encoding="utf-8",
    )
    loaded = read_thread_metadata(tmp_path, "thread-aaaaaaaaaaaa")
    assert loaded is not None
    # v9 schema：3 个 token 字段已物理删除；v10 再补 is_archived 默认 False
    assert loaded.schema_version == 10
    assert loaded.is_archived is False
    assert not hasattr(loaded, "cumulative_usage")
    assert not hasattr(loaded, "last_run_snapshot")
    assert not hasattr(loaded, "last_model_name")
    assert loaded.is_pinned is False


def test_read_directory_instead_of_file_returns_none(tmp_path: Path) -> None:
    """metadata.json 是目录而非文件 → 返回 None（不抛）。"""
    path = thread_metadata_path(tmp_path, "thread-aaaaaaaaaaaa")
    path.parent.mkdir(parents=True, exist_ok=True)
    # 把 metadata.json 创建成目录
    path.mkdir()
    assert read_thread_metadata(tmp_path, "thread-aaaaaaaaaaaa") is None


# ---------------------------------------------------------------------------
# list_thread_metadata
# ---------------------------------------------------------------------------


def test_list_empty_when_root_missing(tmp_path: Path) -> None:
    assert list_thread_metadata(tmp_path) == []


def test_list_sorted_by_updated_at_desc(tmp_path: Path) -> None:
    a = _make_meta("thread-aaaaaaaaaaaa", updated_at=100.0)
    b = _make_meta("thread-bbbbbbbbbbbb", updated_at=300.0)
    c = _make_meta("thread-cccccccccccc", updated_at=200.0)
    for m in (a, b, c):
        write_thread_metadata(tmp_path, m)
    out = list_thread_metadata(tmp_path)
    assert [m.id for m in out] == [
        "thread-bbbbbbbbbbbb",  # updated_at=300
        "thread-cccccccccccc",  # 200
        "thread-aaaaaaaaaaaa",  # 100
    ]


def test_list_uses_thread_id_as_stable_tiebreaker(tmp_path: Path) -> None:
    a = _make_meta("thread-aaaaaaaaaaaa", updated_at=100.0)
    c = _make_meta("thread-cccccccccccc", updated_at=100.0)
    b = _make_meta("thread-bbbbbbbbbbbb", updated_at=100.0)
    for m in (a, c, b):
        write_thread_metadata(tmp_path, m)
    out = list_thread_metadata(tmp_path)
    assert [m.id for m in out] == [
        "thread-cccccccccccc",
        "thread-bbbbbbbbbbbb",
        "thread-aaaaaaaaaaaa",
    ]


def test_list_pinned_threads_sorted_first(tmp_path: Path) -> None:
    a = _make_meta("thread-aaaaaaaaaaaa", updated_at=100.0, is_pinned=False)
    b = _make_meta("thread-bbbbbbbbbbbb", updated_at=300.0, is_pinned=False)
    c = _make_meta("thread-cccccccccccc", updated_at=50.0, is_pinned=True)
    for m in (a, b, c):
        write_thread_metadata(tmp_path, m)
    out = list_thread_metadata(tmp_path)
    assert [m.id for m in out] == [
        "thread-cccccccccccc",  # pinned, updated_at=50
        "thread-bbbbbbbbbbbb",  # not pinned, updated_at=300
        "thread-aaaaaaaaaaaa",  # not pinned, updated_at=100
    ]


def test_list_skips_corrupted(tmp_path: Path) -> None:
    a = _make_meta("thread-aaaaaaaaaaaa")
    write_thread_metadata(tmp_path, a)
    # 损坏一个目录
    bad = thread_metadata_path(tmp_path, "thread-bbbbbbbbbbbb")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("garbage", encoding="utf-8")
    out = list_thread_metadata(tmp_path)
    assert [m.id for m in out] == ["thread-aaaaaaaaaaaa"]


def test_list_ignores_non_directory_entries(tmp_path: Path) -> None:
    """root 下出现孤立文件时不应崩溃。"""
    root = tmp_path / "web" / "threads"
    root.mkdir(parents=True, exist_ok=True)
    (root / "stray.txt").write_text("noise", encoding="utf-8")
    a = _make_meta("thread-aaaaaaaaaaaa")
    write_thread_metadata(tmp_path, a)
    out = list_thread_metadata(tmp_path)
    assert [m.id for m in out] == ["thread-aaaaaaaaaaaa"]


# ---------------------------------------------------------------------------
# delete_thread_metadata_dir
# ---------------------------------------------------------------------------


def test_delete_idempotent_when_missing(tmp_path: Path) -> None:
    # 不存在时不抛
    delete_thread_metadata_dir(tmp_path, "thread-aaaaaaaaaaaa")


def test_delete_removes_metadata_dir(tmp_path: Path) -> None:
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    target_dir = thread_metadata_dir(tmp_path, meta.id)
    assert target_dir.is_dir()
    delete_thread_metadata_dir(tmp_path, meta.id)
    assert not target_dir.exists()


def test_delete_removes_extra_files_in_thread_dir(tmp_path: Path) -> None:
    """metadata 目录下若有额外文件（v0.1.6+ 可能新增），也应一并删除。"""
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    target_dir = thread_metadata_dir(tmp_path, meta.id)
    extra = target_dir / "extra.bin"
    extra.write_text("blob", encoding="utf-8")
    delete_thread_metadata_dir(tmp_path, meta.id)
    assert not target_dir.exists()


# ---------------------------------------------------------------------------
# is_pinned 相关
# ---------------------------------------------------------------------------


def test_is_pinned_default_false() -> None:
    meta = _make_meta()
    assert meta.is_pinned is False


def test_is_pinned_round_trip(tmp_path: Path) -> None:
    meta = _make_meta(is_pinned=True)
    write_thread_metadata(tmp_path, meta)
    loaded = read_thread_metadata(tmp_path, meta.id)
    assert loaded is not None
    assert loaded.is_pinned is True


def test_read_v6_lazy_upgrades_through_v9(tmp_path: Path) -> None:
    """v6 文件 → 懒升级到 v9：穿透多版本链，最终 drop token 字段。"""
    path = thread_metadata_path(tmp_path, "thread-aaaaaaaaaaaa")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"id":"thread-aaaaaaaaaaaa","name":"x","preset_id":"p",'
        '"backend_kind":"generic_chat","claude_thread_id":"","codex_thread_id":"","cwd":"",'
        '"created_at":1.0,"updated_at":2.0,"message_count":3,'
        '"cumulative_prompt_tokens":0,"cumulative_completion_tokens":0,'
        '"cumulative_total_tokens":0,"cumulative_cache_read_tokens":0,'
        '"cumulative_cache_creation_tokens":0,"schema_version":6}',
        encoding="utf-8",
    )
    loaded = read_thread_metadata(tmp_path, "thread-aaaaaaaaaaaa")
    assert loaded is not None
    assert loaded.schema_version == 10
    assert loaded.is_pinned is False
    assert loaded.is_archived is False
    # v9：token 字段已物理删除
    assert not hasattr(loaded, "cumulative_usage")


# ---------------------------------------------------------------------------
# v9 专属测试（usage-token-v2-bigbang）
# ---------------------------------------------------------------------------


def test_v8_to_v9_drops_token_fields(tmp_path: Path) -> None:
    """v8 文件含 3 个 token 字段 → 读出后被 drop，schema 升到 v9。"""
    path = thread_metadata_path(tmp_path, "thread-aaaaaaaaaaaa")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"id":"thread-aaaaaaaaaaaa","name":"x","preset_id":"p",'
        '"backend_kind":"claude_code","claude_thread_id":"sid-1","codex_thread_id":"",'
        '"cwd":"/tmp","created_at":1.0,"updated_at":2.0,"message_count":3,'
        '"cumulative_usage":{"channel":"anthropic","input_tokens":100,'
        '"cache_read_input_tokens":50000,"cache_creation_input_tokens":200,'
        '"output_tokens":300},'
        '"last_run_snapshot":{"channel":"anthropic","input_tokens":5,"output_tokens":10,'
        '"extras":{},"context_usage":50205,"turn":0,"run_id":""},'
        '"last_model_name":"claude-opus-4",'
        '"is_pinned":true,"schema_version":8}',
        encoding="utf-8",
    )
    loaded = read_thread_metadata(tmp_path, "thread-aaaaaaaaaaaa")
    assert loaded is not None
    assert loaded.schema_version == 10
    # v9：token 字段已物理删除（ThreadMetadata 类不再含这些字段）
    assert not hasattr(loaded, "cumulative_usage")
    assert not hasattr(loaded, "last_run_snapshot")
    assert not hasattr(loaded, "last_model_name")
    # 其他字段保留
    assert loaded.claude_thread_id == "sid-1"
    assert loaded.cwd == "/tmp"
    assert loaded.is_pinned is True


def test_v9_idempotent_no_change(tmp_path: Path) -> None:
    """v9 文件读入再 lazy upgrade 应该无变化（idempotent）。"""
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    loaded1 = read_thread_metadata(tmp_path, meta.id)
    # 再读一次（模拟 idempotent 检查）
    loaded2 = read_thread_metadata(tmp_path, meta.id)
    assert loaded1 == loaded2 == meta
    assert loaded1 is not None and loaded1.schema_version == 10


def test_v11_unknown_schema_returns_none(tmp_path: Path) -> None:
    """v11 是未来版本，本进程不认识 → 返回 None（Literal[1..10] 拒绝）。"""
    path = thread_metadata_path(tmp_path, "thread-aaaaaaaaaaaa")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"id":"thread-aaaaaaaaaaaa","name":"x","preset_id":"p",'
        '"created_at":1.0,"updated_at":1.0,"message_count":0,"schema_version":11}',
        encoding="utf-8",
    )
    assert read_thread_metadata(tmp_path, "thread-aaaaaaaaaaaa") is None


def test_v10_extra_forbid_rejects_unknown_field(tmp_path: Path) -> None:
    """v10 schema 仍 ``extra="forbid"``：未知字段拒绝。"""
    path = thread_metadata_path(tmp_path, "thread-aaaaaaaaaaaa")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"id":"thread-aaaaaaaaaaaa","name":"x","preset_id":"p",'
        '"created_at":1.0,"updated_at":1.0,"message_count":0,'
        '"unknown_v11_field":"hello","schema_version":10}',
        encoding="utf-8",
    )
    # extra="forbid" 让 model_validate 拒绝；read_thread_metadata 兜底返 None
    assert read_thread_metadata(tmp_path, "thread-aaaaaaaaaaaa") is None


def test_v10_write_does_not_include_token_fields(tmp_path: Path) -> None:
    """v10 schema 写盘 dump 时不含被删的 3 个 token 字段，含 is_archived 默认 False。"""
    import json as _json

    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    raw = thread_metadata_path(tmp_path, meta.id).read_text(encoding="utf-8")
    data = _json.loads(raw)
    assert "cumulative_usage" not in data
    assert "last_run_snapshot" not in data
    assert "last_model_name" not in data
    assert data["schema_version"] == 10
    assert data["is_archived"] is False
