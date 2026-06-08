"""ThreadManager.boot_or_attach 并发安全单测（Phase 2 #17）。

核心断言：100 个 task 同时调用 ``boot_or_attach(thread_id)`` 同一 thread_id：
- ``runtime_factory`` 只被调一次
- 全部 task 拿到同一个 ThreadCell 实例

辅助测试：
- 不存在的 metadata → 抛 KeyError
- create_thread + boot_or_attach 走一条端到端
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from infrastructure.config.models import Config
from web.app_support.host_adapter import WebHostAdapter
from web.threads.manager import ThreadManager
from web.threads.metadata import ThreadMetadata, write_thread_metadata


def _make_cfg() -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {
                "enabled": True,
                "idle_timeout_seconds": 60,
                "idle_check_interval_seconds": 10,
                "pending_approval_timeout_seconds": 60,
            },
        }
    )


def _make_meta(thread_id: str = "thread-aaaaaaaaaaaa", **overrides: Any) -> ThreadMetadata:
    base: dict[str, Any] = {
        "id": thread_id,
        "name": "demo",
        "preset_id": "p1",
        "created_at": 1.0,
        "updated_at": 2.0,
        "message_count": 0,
    }
    base.update(overrides)
    return ThreadMetadata.model_validate(base)


def _make_runtime_mock() -> MagicMock:
    runtime = MagicMock()
    runtime.aclose = AsyncMock(return_value=None)
    return runtime


def _make_bridge_mock() -> MagicMock:
    bridge = MagicMock()
    bridge.run_once = AsyncMock()
    return bridge


def _make_factory_call_counter() -> tuple[Any, dict[str, int]]:
    """构造 runtime_factory + 调用计数器；按 thread_id 统计调用次数。"""
    counts: dict[str, int] = {}

    async def factory(
        thread_id: str,
        preset_id: str,
        adapter: WebHostAdapter,
        sinks: list[Any],
    ) -> tuple[Any, Any]:
        counts[thread_id] = counts.get(thread_id, 0) + 1
        # 模拟一些异步 build 时间，给并发竞争窗口
        await asyncio.sleep(0.01)
        return _make_runtime_mock(), _make_bridge_mock()

    return factory, counts


# ---------------------------------------------------------------------------
# 并发 boot
# ---------------------------------------------------------------------------


async def test_boot_or_attach_concurrent_only_builds_once(tmp_path: Path) -> None:
    cfg = _make_cfg()
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)

    factory, counts = _make_factory_call_counter()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    tasks = [asyncio.create_task(mgr.boot_or_attach(meta.id)) for _ in range(100)]
    cells = await asyncio.gather(*tasks)

    # factory 只被调一次
    assert counts == {meta.id: 1}
    # 所有 task 拿到同一个 cell 实例
    first = cells[0]
    assert all(c is first for c in cells)


async def test_boot_or_attach_returns_existing_cell_on_second_call(tmp_path: Path) -> None:
    cfg = _make_cfg()
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    factory, counts = _make_factory_call_counter()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    cell1 = await mgr.boot_or_attach(meta.id)
    cell2 = await mgr.boot_or_attach(meta.id)
    assert cell1 is cell2
    assert counts[meta.id] == 1


async def test_boot_or_attach_different_thread_ids_build_separately(tmp_path: Path) -> None:
    cfg = _make_cfg()
    meta_a = _make_meta("thread-aaaaaaaaaaaa")
    meta_b = _make_meta("thread-bbbbbbbbbbbb")
    write_thread_metadata(tmp_path, meta_a)
    write_thread_metadata(tmp_path, meta_b)
    factory, counts = _make_factory_call_counter()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    cell_a = await mgr.boot_or_attach(meta_a.id)
    cell_b = await mgr.boot_or_attach(meta_b.id)
    assert cell_a is not cell_b
    assert counts[meta_a.id] == 1
    assert counts[meta_b.id] == 1


async def test_boot_or_attach_unknown_thread_raises_keyerror(tmp_path: Path) -> None:
    cfg = _make_cfg()
    factory, _ = _make_factory_call_counter()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    with pytest.raises(KeyError):
        await mgr.boot_or_attach("thread-ffffffffffff")


# ---------------------------------------------------------------------------
# create_thread + rename
# ---------------------------------------------------------------------------


async def test_create_thread_writes_metadata(tmp_path: Path) -> None:
    cfg = _make_cfg()
    factory, _ = _make_factory_call_counter()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    meta = await mgr.create_thread("My thread", "preset-1")
    assert meta.name == "My thread"
    assert meta.preset_id == "preset-1"
    # 落盘可读回
    from web.threads.metadata import read_thread_metadata

    loaded = read_thread_metadata(tmp_path, meta.id)
    assert loaded == meta


async def test_create_thread_empty_name_uses_thread_id(tmp_path: Path) -> None:
    """空名 → name 回退到 thread_id。"""
    cfg = _make_cfg()
    factory, _ = _make_factory_call_counter()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    meta = await mgr.create_thread("   ", "p1")
    # name 应该是 thread_id（thread-<hex12> 格式）
    assert meta.name == meta.id
    assert meta.name.startswith("thread-")


async def test_create_thread_empty_string_name_uses_thread_id(tmp_path: Path) -> None:
    """完全空串 → name 回退到 thread_id。"""
    cfg = _make_cfg()
    factory, _ = _make_factory_call_counter()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    meta = await mgr.create_thread("", "p1")
    assert meta.name == meta.id


async def test_create_thread_rejects_empty_preset(tmp_path: Path) -> None:
    cfg = _make_cfg()
    factory, _ = _make_factory_call_counter()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    with pytest.raises(ValueError):
        await mgr.create_thread("name", "")


async def test_rename_thread_updates_metadata_and_cell(tmp_path: Path) -> None:
    cfg = _make_cfg()
    factory, _ = _make_factory_call_counter()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    meta = await mgr.create_thread("old", "p1")
    cell = await mgr.boot_or_attach(meta.id)
    assert cell.metadata.name == "old"
    new_meta = await mgr.rename_thread(meta.id, "new")
    assert new_meta.name == "new"
    assert new_meta.updated_at >= meta.updated_at
    assert cell.metadata.name == "new"


async def test_rename_thread_unknown_raises_keyerror(tmp_path: Path) -> None:
    cfg = _make_cfg()
    factory, _ = _make_factory_call_counter()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    with pytest.raises(KeyError):
        await mgr.rename_thread("thread-ffffffffffff", "x")


# ---------------------------------------------------------------------------
# delete_thread
# ---------------------------------------------------------------------------


async def test_delete_thread_removes_metadata_and_cell(tmp_path: Path) -> None:
    cfg = _make_cfg()
    factory, _ = _make_factory_call_counter()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    meta = await mgr.create_thread("x", "p1")
    cell = await mgr.boot_or_attach(meta.id)
    assert mgr.get_cell(meta.id) is cell

    await mgr.delete_thread(meta.id)
    assert mgr.get_cell(meta.id) is None
    from web.threads.metadata import read_thread_metadata

    assert read_thread_metadata(tmp_path, meta.id) is None


async def test_delete_thread_idempotent_for_missing(tmp_path: Path) -> None:
    cfg = _make_cfg()
    factory, _ = _make_factory_call_counter()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    # 直接 delete 不存在的 thread 不抛
    await mgr.delete_thread("thread-ffffffffffff")
