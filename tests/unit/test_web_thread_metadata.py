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

from web.thread_metadata import (
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
    # v0.2.1 bump 到 4（加累计 usage 字段）；老 v1/v2/v3 文件由
    # read_thread_metadata 懒升级
    assert meta.schema_version == THREAD_METADATA_SCHEMA_VERSION == 4


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


def test_read_v3_lazy_upgrades_usage_totals_to_v4(tmp_path: Path) -> None:
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
    assert loaded.schema_version == 4
    assert loaded.cumulative_prompt_tokens == 0
    assert loaded.cumulative_completion_tokens == 0
    assert loaded.cumulative_total_tokens == 0


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
