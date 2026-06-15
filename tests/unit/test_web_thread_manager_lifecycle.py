"""ThreadManager 端到端 lifecycle 单测（Phase 2 #20）。

覆盖：
- start() 触发预热扫盘 + 启动 idle eviction
- start() 幂等（二次调用不重复启动 idle task）
- list_threads / list_cells 返回内容（含 disk + memory 合并）
- list_cells 反映 status 与 last_active_at
- resolve_approval 委托到 cell.adapter
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from hosts.web.threads.manager import ThreadManager
from hosts.web.threads.metadata import ThreadMetadata, read_thread_metadata, write_thread_metadata
from infrastructure.config.models import Config


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
                "idle_timeout_seconds": 1800,
                "idle_check_interval_seconds": 60,
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


def _make_factory() -> Any:
    async def factory(
        thread_id: str,
        preset_id: str,
        adapter: Any,
        sinks: list[Any],
    ) -> tuple[Any, Any]:
        runtime = MagicMock()
        runtime.aclose = AsyncMock(return_value=None)
        bridge = MagicMock()
        return runtime, bridge

    return factory


# ---------------------------------------------------------------------------
# start / shutdown
# ---------------------------------------------------------------------------


async def test_start_initializes_idle_task(tmp_path: Path) -> None:
    cfg = _make_cfg()
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    assert mgr.started is False
    assert mgr._idle_task is None

    await mgr.start()
    assert mgr.started is True
    assert mgr._idle_task is not None

    # 幂等
    saved = mgr._idle_task
    await mgr.start()
    assert mgr._idle_task is saved

    await mgr.aclose_all()


async def test_start_with_existing_threads_on_disk(tmp_path: Path) -> None:
    cfg = _make_cfg()
    factory = _make_factory()
    a = _make_meta("thread-aaaaaaaaaaaa", updated_at=10.0)
    b = _make_meta("thread-bbbbbbbbbbbb", updated_at=20.0)
    write_thread_metadata(tmp_path, a)
    write_thread_metadata(tmp_path, b)

    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    await mgr.start()
    listed = mgr.list_threads()
    # 有效但未 boot：list_threads 应当从盘上扫到
    assert {m.id for m in listed} == {a.id, b.id}
    # 排序：updated_at 降序
    assert listed[0].id == b.id
    await mgr.aclose_all()


# ---------------------------------------------------------------------------
# list_threads / list_cells
# ---------------------------------------------------------------------------


async def test_list_threads_merges_disk_and_memory(tmp_path: Path) -> None:
    """rename 后内存版本应覆盖 disk 版本。"""
    cfg = _make_cfg()
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    meta = await mgr.create_thread("orig", "p1")
    cell = await mgr.boot_or_attach(meta.id)
    # 直接修改 cell.metadata（绕过 rename，模拟内存与盘版本不一致）
    new_meta = ThreadMetadata.model_validate(
        {
            **meta.model_dump(),
            "name": "memory-only-name",
            "updated_at": meta.updated_at + 100,
        }
    )
    cell.metadata = new_meta

    listed = mgr.list_threads()
    assert len(listed) == 1
    assert listed[0].name == "memory-only-name"


async def test_usage_manager_v2_exposed_with_get_thread_usage_only(tmp_path: Path) -> None:
    """**usage-token-v2-bigbang**：ThreadManager.usage_manager 暴露 v2 无状态门面。

    v2 manager 公共方法**只有** ``get_thread_usage(thread_id)``——v1 时代的
    ``record_run_usage`` / ``set_last_assistant_usage`` / ``get_thread_summary``
    等方法全部删除（防回归）。
    """
    cfg = _make_cfg()
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    meta = await mgr.create_thread("usage", "p1")

    # v2 manager 只有 get_thread_usage 公共方法
    assert hasattr(mgr.usage_manager, "get_thread_usage")

    # v1 方法全部删除（防回归）
    assert not hasattr(mgr.usage_manager, "record_run_usage")
    assert not hasattr(mgr.usage_manager, "set_last_assistant_usage")
    assert not hasattr(mgr.usage_manager, "get_thread_summary")
    assert not hasattr(mgr.usage_manager, "to_ws_frame")
    assert not hasattr(mgr.usage_manager, "context_window")
    assert not hasattr(mgr.usage_manager, "context_usage_pct")
    assert not hasattr(mgr.usage_manager, "set_thread_model")

    # 新建 thread 没绑 SDK 真源 → get_thread_usage 返回 None（不抛）
    result = await mgr.usage_manager.get_thread_usage(meta.id)
    assert result is None

    # metadata.json 不含任何 token 字段（schema v9 物理删 3 字段）；v11 为当前版本
    loaded = read_thread_metadata(tmp_path, meta.id)
    assert loaded is not None
    assert not hasattr(loaded, "cumulative_usage")
    assert not hasattr(loaded, "last_run_snapshot")
    assert not hasattr(loaded, "last_model_name")
    assert loaded.schema_version == 11


async def test_list_cells_returns_only_active_cells(tmp_path: Path) -> None:
    cfg = _make_cfg()
    factory = _make_factory()
    a = _make_meta("thread-aaaaaaaaaaaa")
    b = _make_meta("thread-bbbbbbbbbbbb")
    write_thread_metadata(tmp_path, a)
    write_thread_metadata(tmp_path, b)

    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    await mgr.boot_or_attach(a.id)  # 仅 boot a
    cells = mgr.list_cells()
    assert {c.thread_id for c in cells} == {a.id}
    summary = cells[0]
    assert summary.thread_name == "demo"
    assert summary.preset_id == "p1"
    assert summary.status == "idle"
    assert summary.pending_approval_count == 0


# ---------------------------------------------------------------------------
# resolve_approval 委托
# ---------------------------------------------------------------------------


async def test_resolve_approval_forwards_to_cell_adapter(tmp_path: Path) -> None:
    cfg = _make_cfg()
    factory = _make_factory()
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    cell = await mgr.boot_or_attach(meta.id)

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[bool] = loop.create_future()
    cell.adapter._pending_approvals["c1"] = fut

    mgr.resolve_approval(meta.id, "c1", True)
    assert fut.result() is True


async def test_resolve_approval_unknown_thread_silent(tmp_path: Path) -> None:
    cfg = _make_cfg()
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    # 不抛
    mgr.resolve_approval("thread-ffffffffffff", "c1", True)


# ---------------------------------------------------------------------------
# get_cell / kongming_home
# ---------------------------------------------------------------------------


async def test_get_cell_returns_none_when_not_booted(tmp_path: Path) -> None:
    cfg = _make_cfg()
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    assert mgr.get_cell("thread-aaaaaaaaaaaa") is None


async def test_kongming_home_property(tmp_path: Path) -> None:
    cfg = _make_cfg()
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    assert mgr.kongming_home == tmp_path


# ---------------------------------------------------------------------------
# 端到端：create + boot + list + delete + aclose_all
# ---------------------------------------------------------------------------


async def test_full_lifecycle_create_boot_delete(tmp_path: Path) -> None:
    cfg = _make_cfg()
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    await mgr.start()

    meta = await mgr.create_thread("hello", "p1")
    cell = await mgr.boot_or_attach(meta.id)
    assert mgr.get_cell(meta.id) is cell

    # list_threads + list_cells
    assert any(m.id == meta.id for m in mgr.list_threads())
    assert any(s.thread_id == meta.id for s in mgr.list_cells())

    # delete
    await mgr.delete_thread(meta.id)
    assert mgr.get_cell(meta.id) is None
    assert not any(m.id == meta.id for m in mgr.list_threads())

    await mgr.aclose_all()


async def test_aclose_all_after_start(tmp_path: Path) -> None:
    cfg = _make_cfg()
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    await mgr.start()
    meta = await mgr.create_thread("x", "p1")
    await mgr.boot_or_attach(meta.id)
    await mgr.aclose_all()
    assert mgr.closed is True
    assert mgr.get_cell(meta.id) is None
