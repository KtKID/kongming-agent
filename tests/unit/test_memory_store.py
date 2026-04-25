"""MemoryStore 单元测试。

策略：
- 所有测试使用 tmp_path，不触碰项目真实 .kongming/memory/。
- v0.1.3 覆盖：ensure/load 兼容 + load_from_disk/snapshot/entries/render_prompt 新功能。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memory.store import (
    MemorySnapshot,
    MemoryStore,
)

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _write_memory(tmp_path: pytest.TempPathFactory, filename: str, content: str) -> None:
    """写入 memory 目录下指定文件。"""
    mem_dir = tmp_path / ".kongming" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / filename).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# 兼容测试：ensure / load
# ---------------------------------------------------------------------------


class TestMemoryStoreEnsure:
    """MemoryStore.ensure() 兼容测试。"""

    async def test_ensure_creates_file_if_missing(self, tmp_path: Path) -> None:
        """ensure() 在文件不存在时创建默认模板。"""
        store = MemoryStore(base_path=tmp_path)
        path = await store.ensure()
        assert path.exists()
        assert path.name == "MEMORY.md"

    async def test_ensure_idempotent(self, tmp_path: Path) -> None:
        """多次 ensure 不覆盖已有内容。"""
        store = MemoryStore(base_path=tmp_path)
        await store.ensure()
        content_before = store.memory_file_path.read_text(encoding="utf-8")
        await store.ensure()
        content_after = store.memory_file_path.read_text(encoding="utf-8")
        assert content_before == content_after


class TestMemoryStoreLoad:
    """MemoryStore.load() 兼容测试。"""

    async def test_load_reads_existing_file(self, tmp_path: Path) -> None:
        """ensure 后 load 能读到内容。"""
        _write_memory(tmp_path, "MEMORY.md", "# Agent Memory\nhello")
        store = MemoryStore(base_path=tmp_path)
        result = await store.load()
        assert result is not None
        assert "hello" in result

    async def test_load_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        store = MemoryStore(base_path=tmp_path / "nonexistent")
        result = await store.load()
        assert result is None

    async def test_load_returns_none_when_file_empty(self, tmp_path: Path) -> None:
        _write_memory(tmp_path, "MEMORY.md", "")
        store = MemoryStore(base_path=tmp_path)
        result = await store.load()
        assert result is None

    async def test_load_returns_none_when_whitespace_only(self, tmp_path: Path) -> None:
        _write_memory(tmp_path, "MEMORY.md", "   \n\n  \t  \n")
        store = MemoryStore(base_path=tmp_path)
        result = await store.load()
        assert result is None

    async def test_load_cjk_content(self, tmp_path: Path) -> None:
        content = "# 用户偏好\n- 使用中文回复\n- 代码注释用英文"
        _write_memory(tmp_path, "MEMORY.md", content)
        store = MemoryStore(base_path=tmp_path)
        result = await store.load()
        assert result == content


class TestMemoryStorePath:
    """路径相关测试。"""

    def test_memory_file_path_property(self, tmp_path: Path) -> None:
        store = MemoryStore(base_path=tmp_path)
        p = store.memory_file_path
        assert p.is_absolute()
        assert p.name == "MEMORY.md"

    def test_memory_dir_property(self, tmp_path: Path) -> None:
        store = MemoryStore(base_path=tmp_path)
        assert store.memory_dir == tmp_path / ".kongming" / "memory"

    def test_target_path(self, tmp_path: Path) -> None:
        store = MemoryStore(base_path=tmp_path)
        assert store.target_path("memory").name == "MEMORY.md"
        assert store.target_path("user").name == "USER.md"
        assert store.target_path("errors").name == "ERRORS.md"


# ---------------------------------------------------------------------------
# v0.1.3 新功能：load_from_disk / snapshot / entries / render_prompt
# ---------------------------------------------------------------------------


class TestLoadFromDisk:
    """load_from_disk() 多文件读取 + snapshot 捕获。"""

    async def test_creates_directory(self, tmp_path: Path) -> None:
        """目录不存在时自动创建。"""
        store = MemoryStore(base_path=tmp_path)
        assert not (tmp_path / ".kongming" / "memory").exists()
        await store.load_from_disk()
        assert (tmp_path / ".kongming" / "memory").exists()

    async def test_empty_snapshot_when_no_files(self, tmp_path: Path) -> None:
        """文件不存在时 snapshot 为空但非 None。"""
        store = MemoryStore(base_path=tmp_path)
        snap = await store.load_from_disk()
        assert snap is not None
        assert snap.is_empty
        assert snap.memory_text == ""
        assert snap.user_text == ""

    async def test_reads_all_three_files(self, tmp_path: Path) -> None:
        """读取 MEMORY / USER / ERRORS 三个文件。"""
        _write_memory(tmp_path, "MEMORY.md", "mem1\n§\nmem2")
        _write_memory(tmp_path, "USER.md", "user1\n§\nuser2")
        _write_memory(tmp_path, "ERRORS.md", "err1\n§\nerr2")

        store = MemoryStore(base_path=tmp_path)
        snap = await store.load_from_disk()

        assert "mem1" in snap.memory_text
        assert "user1" in snap.user_text
        assert len(store.memory_entries) == 2
        assert len(store.user_entries) == 2
        assert len(store.error_entries) == 2

    async def test_missing_files_return_empty(self, tmp_path: Path) -> None:
        """部分文件缺失不影响读取。"""
        _write_memory(tmp_path, "MEMORY.md", "only memory")

        store = MemoryStore(base_path=tmp_path)
        snap = await store.load_from_disk()

        assert "only memory" in snap.memory_text
        assert snap.user_text == ""
        assert len(store.user_entries) == 0

    async def test_checksum(self, tmp_path: Path) -> None:
        """checksum 以 sha256: 前缀开头。"""
        _write_memory(tmp_path, "MEMORY.md", "content")
        store = MemoryStore(base_path=tmp_path)
        snap = await store.load_from_disk()
        assert snap.checksum.startswith("sha256:")
        assert len(snap.checksum) == 71  # sha256: + 64 hex chars

    async def test_source_paths_only_existing(self, tmp_path: Path) -> None:
        """source_paths 只包含实际存在的文件。"""
        _write_memory(tmp_path, "MEMORY.md", "mem")
        store = MemoryStore(base_path=tmp_path)
        snap = await store.load_from_disk()
        assert len(snap.source_paths) == 1
        assert "MEMORY.md" in snap.source_paths[0]

    async def test_entries_dedup_preserves_order(self, tmp_path: Path) -> None:
        """去重保序：重复条目只保留第一次出现。"""
        _write_memory(tmp_path, "MEMORY.md", "alpha\n§\nbeta\n§\nalpha")
        store = MemoryStore(base_path=tmp_path)
        await store.load_from_disk()
        contents = [e.content for e in store.memory_entries]
        assert contents == ["alpha", "beta"]


class TestSnapshot:
    """MemorySnapshot frozen dataclass 测试。"""

    def test_is_empty_when_both_blank(self) -> None:
        snap = MemorySnapshot(
            memory_text="",
            user_text="",
            captured_at_ms=0,
            source_paths=(),
            checksum="sha256:abc",
        )
        assert snap.is_empty

    def test_is_not_empty_with_memory(self) -> None:
        snap = MemorySnapshot(
            memory_text="content",
            user_text="",
            captured_at_ms=0,
            source_paths=(),
            checksum="sha256:abc",
        )
        assert not snap.is_empty

    def test_render_prompt_returns_none_when_empty(self) -> None:
        snap = MemorySnapshot(
            memory_text="",
            user_text="",
            captured_at_ms=0,
            source_paths=(),
            checksum="sha256:abc",
        )
        assert snap.render_prompt() is None

    def test_render_prompt_includes_memory_block(self) -> None:
        snap = MemorySnapshot(
            memory_text="mem content",
            user_text="",
            captured_at_ms=0,
            source_paths=(),
            checksum="sha256:abc",
        )
        rendered = snap.render_prompt()
        assert rendered is not None
        assert "MEMORY" in rendered
        assert "mem content" in rendered
        assert "chars]" in rendered

    def test_render_prompt_includes_both_blocks(self) -> None:
        snap = MemorySnapshot(
            memory_text="mem",
            user_text="usr",
            captured_at_ms=0,
            source_paths=(),
            checksum="sha256:abc",
        )
        rendered = snap.render_prompt()
        assert "MEMORY" in rendered
        assert "USER PROFILE" in rendered


class TestFormatForSystemPrompt:
    """format_for_system_prompt() 只读冻结态。"""

    async def test_returns_none_without_snapshot(self, tmp_path: Path) -> None:
        store = MemoryStore(base_path=tmp_path)
        assert store.format_for_system_prompt("memory") is None

    async def test_returns_frozen_text(self, tmp_path: Path) -> None:
        _write_memory(tmp_path, "MEMORY.md", "frozen content")
        store = MemoryStore(base_path=tmp_path)
        await store.load_from_disk()

        # refresh 活态 entries，但 snapshot 不变
        store.refresh_entries_for("memory", "live content")
        result = store.format_for_system_prompt("memory")
        assert result == "frozen content"

    async def test_errors_target_returns_none(self, tmp_path: Path) -> None:
        """errors 分区不注入 system prompt。"""
        _write_memory(tmp_path, "ERRORS.md", "some error")
        store = MemoryStore(base_path=tmp_path)
        await store.load_from_disk()
        assert store.format_for_system_prompt("errors") is None


class TestRefreshEntries:
    """refresh_entries_for() 更新活态但不触碰 snapshot。"""

    async def test_refresh_updates_entries_not_snapshot(self, tmp_path: Path) -> None:
        _write_memory(tmp_path, "MEMORY.md", "original")
        store = MemoryStore(base_path=tmp_path)
        await store.load_from_disk()

        store.refresh_entries_for("memory", "new1\n§\nnew2")
        assert len(store.memory_entries) == 2
        assert store.memory_entries[0].content == "new1"
        # snapshot 不变
        assert store.snapshot is not None
        assert "original" in store.snapshot.memory_text

    async def test_reload_refreshes_snapshot(self, tmp_path: Path) -> None:
        _write_memory(tmp_path, "MEMORY.md", "original")
        store = MemoryStore(base_path=tmp_path)
        await store.load_from_disk()

        # 写入新内容到磁盘
        _write_memory(tmp_path, "MEMORY.md", "updated content")
        await store.load_from_disk()

        assert "updated content" in store.snapshot.memory_text
