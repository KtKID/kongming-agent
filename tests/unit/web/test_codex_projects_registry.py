"""Pytest for ``src/web/codex/projects_registry.py``.

覆盖 CRUD + bootstrap + migrate 的全部分支。

worktree 隔离：用 ``tmp_path`` fixture 作为 ``home``，绝对不碰真实
``~/.kongming``。``projects_registry`` 内部的所有路径都基于传入的
``home`` 派生，符合该模块的"不调用 Path.home() / 不读环境变量"约束。

与 ``test_claude_projects_registry.py`` 对称——仅两处差异：
1. import 路径换成 ``web.codex.projects_registry`` / ``codex_projects_path``
2. ``migrate_from_thread_metadata`` 的过滤条件是 ``backend_kind == "codex"``
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from web.codex.projects_registry import (
    CURRENT_VERSION,
    ProjectRegistryEntry,
    add_project,
    bootstrap_register_self,
    codex_projects_path,
    load_registry,
    migrate_from_thread_metadata,
    remove_project,
)
from web.thread_metadata import ThreadMetadata, write_thread_metadata

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _write_raw_registry(home: Path, payload: object) -> Path:
    """直接把任意 payload 写到 registry 路径（用于构造损坏 / 异常文件）。"""
    path = codex_projects_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _make_meta(
    *,
    thread_id: str,
    backend_kind: str = "codex",
    cwd: str = "",
    name: str = "t",
) -> ThreadMetadata:
    """造一条 ``ThreadMetadata`` —— 用于喂给 ``write_thread_metadata``。"""
    return ThreadMetadata(
        id=thread_id,
        name=name,
        preset_id="" if backend_kind != "generic_chat" else "p1",
        backend_kind=backend_kind,  # type: ignore[arg-type]
        cwd=cwd,
        created_at=1.0,
        updated_at=1.0,
    )


# ---------------------------------------------------------------------------
# load_registry
# ---------------------------------------------------------------------------


class TestLoadRegistry:
    def test_file_missing_returns_empty(self, tmp_path: Path) -> None:
        assert load_registry(tmp_path) == []

    def test_corrupted_json_returns_empty_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_raw_registry(tmp_path, "{not json{{{")
        with caplog.at_level(logging.WARNING, logger="web.codex.projects_registry"):
            entries = load_registry(tmp_path)
        assert entries == []
        # 至少有一条 warning 提到 "JSON corrupted"
        assert any("JSON corrupted" in rec.message for rec in caplog.records)

    def test_top_level_list_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # 顶层是 list 而不是 dict
        _write_raw_registry(tmp_path, [{"cwd": "/foo", "alias": "", "added_at": 1.0}])
        with caplog.at_level(logging.WARNING, logger="web.codex.projects_registry"):
            entries = load_registry(tmp_path)
        assert entries == []
        assert any("not a JSON object" in rec.message for rec in caplog.records)

    def test_schema_version_higher_than_current_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_raw_registry(
            tmp_path,
            {"version": CURRENT_VERSION + 99, "projects": [{"cwd": "/foo"}]},
        )
        with caplog.at_level(logging.WARNING, logger="web.codex.projects_registry"):
            entries = load_registry(tmp_path)
        assert entries == []
        assert any("exceeds known" in rec.message for rec in caplog.records)

    def test_version_not_int_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_raw_registry(tmp_path, {"version": "1", "projects": []})
        with caplog.at_level(logging.WARNING, logger="web.codex.projects_registry"):
            entries = load_registry(tmp_path)
        assert entries == []
        assert any("version not int" in rec.message for rec in caplog.records)

    def test_projects_field_not_list_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_raw_registry(tmp_path, {"version": 1, "projects": {"foo": "bar"}})
        with caplog.at_level(logging.WARNING, logger="web.codex.projects_registry"):
            entries = load_registry(tmp_path)
        assert entries == []
        assert any("no projects list" in rec.message for rec in caplog.records)

    def test_normal_file_parses_correctly(self, tmp_path: Path) -> None:
        _write_raw_registry(
            tmp_path,
            {
                "version": 1,
                "projects": [
                    {"cwd": "/foo", "alias": "Foo", "added_at": 100.5},
                    {"cwd": "/bar", "alias": "", "added_at": 200.0},
                ],
            },
        )
        entries = load_registry(tmp_path)
        assert entries == [
            ProjectRegistryEntry(cwd="/foo", alias="Foo", added_at=100.5),
            ProjectRegistryEntry(cwd="/bar", alias="", added_at=200.0),
        ]

    def test_invalid_entry_skipped_but_others_kept(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # 中间 entry 损坏（cwd 不以 / 开头）→ 跳过，前后两条保留
        _write_raw_registry(
            tmp_path,
            {
                "version": 1,
                "projects": [
                    {"cwd": "/ok1", "alias": "", "added_at": 1.0},
                    {"cwd": "relative/path", "alias": "", "added_at": 2.0},
                    {"cwd": "/ok2", "alias": "", "added_at": 3.0},
                    "string-not-dict",
                ],
            },
        )
        with caplog.at_level(logging.WARNING, logger="web.codex.projects_registry"):
            entries = load_registry(tmp_path)
        assert [e.cwd for e in entries] == ["/ok1", "/ok2"]

    def test_entry_missing_optional_fields_filled_with_defaults(self, tmp_path: Path) -> None:
        _write_raw_registry(
            tmp_path,
            {"version": 1, "projects": [{"cwd": "/only-cwd"}]},
        )
        entries = load_registry(tmp_path)
        assert entries == [ProjectRegistryEntry(cwd="/only-cwd", alias="", added_at=0.0)]


# ---------------------------------------------------------------------------
# add_project
# ---------------------------------------------------------------------------


class TestAddProject:
    def test_add_new_cwd_appended_and_persisted(self, tmp_path: Path) -> None:
        before = time.time()
        entry = add_project(tmp_path, "/new", alias="NewName")
        after = time.time()
        assert entry.cwd == "/new"
        assert entry.alias == "NewName"
        assert before <= entry.added_at <= after
        # 落盘正确
        loaded = load_registry(tmp_path)
        assert loaded == [entry]

    def test_idempotent_same_cwd_preserves_added_at(self, tmp_path: Path) -> None:
        """关键不变量：同 cwd 重复添加只更新 alias，``added_at`` 不变。"""
        first = add_project(tmp_path, "/same", alias="initial")
        # 等一小段时间确保 time.time() 会变
        time.sleep(0.01)
        second = add_project(tmp_path, "/same", alias="updated")
        assert second.cwd == "/same"
        assert second.alias == "updated"
        # added_at 必须保持初次值
        assert second.added_at == first.added_at
        # registry 里仍然只有一条
        loaded = load_registry(tmp_path)
        assert loaded == [second]

    def test_idempotent_can_clear_alias_to_empty(self, tmp_path: Path) -> None:
        """同 cwd 用 alias="" 调用 → alias 清空，added_at 仍保留。"""
        first = add_project(tmp_path, "/same", alias="non-empty")
        cleared = add_project(tmp_path, "/same")  # alias 默认 ""
        assert cleared.alias == ""
        assert cleared.added_at == first.added_at

    def test_multiple_distinct_cwds_preserve_insertion_order(self, tmp_path: Path) -> None:
        e1 = add_project(tmp_path, "/a")
        e2 = add_project(tmp_path, "/b")
        e3 = add_project(tmp_path, "/c")
        loaded = load_registry(tmp_path)
        assert [e.cwd for e in loaded] == ["/a", "/b", "/c"]
        assert loaded == [e1, e2, e3]

    def test_idempotent_update_does_not_change_position(self, tmp_path: Path) -> None:
        """同 cwd 二次 add 不会被挪到末尾，原位替换。"""
        e_a = add_project(tmp_path, "/a")
        add_project(tmp_path, "/b")
        add_project(tmp_path, "/c")
        # 更新中间那条
        updated = add_project(tmp_path, "/b", alias="renamed")
        loaded = load_registry(tmp_path)
        assert [e.cwd for e in loaded] == ["/a", "/b", "/c"]
        assert loaded[1] == updated
        # 第一条（/a）原 added_at 不受影响
        assert loaded[0].added_at == e_a.added_at

    def test_atomic_write_no_tmp_residue(self, tmp_path: Path) -> None:
        """原子写：调用结束后不应该留下 .tmp 文件。"""
        add_project(tmp_path, "/foo")
        path = codex_projects_path(tmp_path)
        # 实现用 path.with_suffix(".json.tmp") → codex_projects.json.tmp
        tmp_residue = path.with_suffix(".json.tmp")
        assert path.is_file()
        assert not tmp_residue.exists()

    def test_persisted_payload_uses_current_version(self, tmp_path: Path) -> None:
        add_project(tmp_path, "/foo", alias="hello")
        path = codex_projects_path(tmp_path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["version"] == CURRENT_VERSION
        assert raw["projects"] == [
            {"cwd": "/foo", "alias": "hello", "added_at": pytest.approx(time.time(), abs=2.0)}
        ]


# ---------------------------------------------------------------------------
# remove_project
# ---------------------------------------------------------------------------


class TestRemoveProject:
    def test_missing_cwd_returns_false(self, tmp_path: Path) -> None:
        # 文件根本不存在
        assert remove_project(tmp_path, "/nope") is False

    def test_missing_cwd_with_existing_entries_returns_false(self, tmp_path: Path) -> None:
        add_project(tmp_path, "/keep")
        assert remove_project(tmp_path, "/not-here") is False
        # 现有 entry 不动
        assert [e.cwd for e in load_registry(tmp_path)] == ["/keep"]

    def test_existing_cwd_removed(self, tmp_path: Path) -> None:
        add_project(tmp_path, "/a")
        add_project(tmp_path, "/b")
        add_project(tmp_path, "/c")
        assert remove_project(tmp_path, "/b") is True
        assert [e.cwd for e in load_registry(tmp_path)] == ["/a", "/c"]

    def test_remove_idempotent_after_first_removal(self, tmp_path: Path) -> None:
        add_project(tmp_path, "/a")
        assert remove_project(tmp_path, "/a") is True
        assert remove_project(tmp_path, "/a") is False
        assert load_registry(tmp_path) == []


# ---------------------------------------------------------------------------
# bootstrap_register_self
# ---------------------------------------------------------------------------


class TestBootstrap:
    def test_creates_registry_when_file_missing(self, tmp_path: Path) -> None:
        assert not codex_projects_path(tmp_path).exists()
        bootstrap_register_self(tmp_path, "/repo/root")
        loaded = load_registry(tmp_path)
        assert [e.cwd for e in loaded] == ["/repo/root"]
        assert loaded[0].alias == ""

    def test_appends_when_repo_root_not_in_registry(self, tmp_path: Path) -> None:
        add_project(tmp_path, "/already/here", alias="user-set")
        bootstrap_register_self(tmp_path, "/repo/root")
        loaded = load_registry(tmp_path)
        assert [e.cwd for e in loaded] == ["/already/here", "/repo/root"]
        # 用户原 alias 保留
        assert loaded[0].alias == "user-set"

    def test_no_op_when_repo_root_already_present(self, tmp_path: Path) -> None:
        existing = add_project(tmp_path, "/repo/root", alias="user-set")
        # bootstrap 不能覆盖用户的 alias / added_at
        time.sleep(0.01)
        bootstrap_register_self(tmp_path, "/repo/root")
        loaded = load_registry(tmp_path)
        assert len(loaded) == 1
        assert loaded[0] == existing
        assert loaded[0].alias == "user-set"
        assert loaded[0].added_at == existing.added_at


# ---------------------------------------------------------------------------
# migrate_from_thread_metadata
# ---------------------------------------------------------------------------


class TestMigrate:
    def test_migrate_empty_threads_returns_zero(self, tmp_path: Path) -> None:
        # 没有任何 thread metadata
        assert migrate_from_thread_metadata(tmp_path) == 0
        assert load_registry(tmp_path) == []

    def test_migrate_only_codex_with_nonempty_cwd(self, tmp_path: Path) -> None:
        """过滤 backend_kind != 'codex' 的、cwd 空的。"""
        # codex + cwd 非空 → 应迁移
        write_thread_metadata(
            tmp_path,
            _make_meta(thread_id="thread-aaaaaaaaaaaa", cwd="/proj/a"),
        )
        # codex + cwd 空 → 跳过
        write_thread_metadata(
            tmp_path,
            _make_meta(thread_id="thread-bbbbbbbbbbbb", cwd=""),
        )
        # claude_code 后端 → 跳过（即使 cwd 非空）
        write_thread_metadata(
            tmp_path,
            _make_meta(
                thread_id="thread-cccccccccccc",
                backend_kind="claude_code",
                cwd="/proj/claude",
            ),
        )
        # generic_chat → 跳过
        write_thread_metadata(
            tmp_path,
            _make_meta(
                thread_id="thread-dddddddddddd",
                backend_kind="generic_chat",
                cwd="/proj/generic",
            ),
        )
        # 又一条 codex + cwd 非空（不同 cwd）→ 应迁移
        write_thread_metadata(
            tmp_path,
            _make_meta(thread_id="thread-eeeeeeeeeeee", cwd="/proj/e"),
        )
        added = migrate_from_thread_metadata(tmp_path)
        assert added == 2
        loaded_cwds = {e.cwd for e in load_registry(tmp_path)}
        assert loaded_cwds == {"/proj/a", "/proj/e"}

    def test_distinct_cwd_dedup(self, tmp_path: Path) -> None:
        """同一个 cwd 多个 thread → 只算一次。"""
        write_thread_metadata(
            tmp_path,
            _make_meta(thread_id="thread-aaaaaaaaaaaa", cwd="/proj/dup"),
        )
        write_thread_metadata(
            tmp_path,
            _make_meta(thread_id="thread-bbbbbbbbbbbb", cwd="/proj/dup"),
        )
        write_thread_metadata(
            tmp_path,
            _make_meta(thread_id="thread-cccccccccccc", cwd="/proj/dup"),
        )
        added = migrate_from_thread_metadata(tmp_path)
        assert added == 1
        loaded = load_registry(tmp_path)
        assert [e.cwd for e in loaded] == ["/proj/dup"]

    def test_existing_cwd_not_recounted(self, tmp_path: Path) -> None:
        """已经在 registry 里的 cwd 不重复迁移、不计数。"""
        # 先手工 add
        existing = add_project(tmp_path, "/proj/existing", alias="user-pre-set")
        # 再用同 cwd 创建 thread
        write_thread_metadata(
            tmp_path,
            _make_meta(thread_id="thread-aaaaaaaaaaaa", cwd="/proj/existing"),
        )
        # 另加一条全新 cwd → 应迁移
        write_thread_metadata(
            tmp_path,
            _make_meta(thread_id="thread-bbbbbbbbbbbb", cwd="/proj/fresh"),
        )
        added = migrate_from_thread_metadata(tmp_path)
        assert added == 1
        loaded = load_registry(tmp_path)
        assert [e.cwd for e in loaded] == ["/proj/existing", "/proj/fresh"]
        # 已存在条目的 alias / added_at 不被覆盖
        assert loaded[0] == existing

    def test_no_threads_dir_returns_zero(self, tmp_path: Path) -> None:
        """连 ``<home>/web/threads/`` 都不存在 → 0，不抛。"""
        # tmp_path 是空目录，list_thread_metadata 返回 []
        assert migrate_from_thread_metadata(tmp_path) == 0
        # 也不会写出 registry 文件（registry 仍为空）
        assert load_registry(tmp_path) == []
