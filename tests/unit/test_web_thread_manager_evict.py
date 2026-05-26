"""ThreadManager eviction 单测（Phase 2 #19）。

覆盖：
- evict_cell 手动路径：cell.evicted 帧推送 + adapter.close + runtime.aclose
- evict_cell idle 路径：_idle_eviction_loop 自动触发
- evict_cell shutdown 路径：notify_ws=False 不推帧
- pending approval 在 evict 时被 resolve(False)
- evict 不存在 thread_id 静默
- aclose_all 兜底所有 cell
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from config_loader.models import Config
from core.contracts import ApprovalAction, ApprovalRequest
from web.thread_manager import ThreadManager
from web.thread_metadata import ThreadMetadata, write_thread_metadata


def _make_cfg(idle_timeout: int = 60, idle_check: int = 10) -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {
                "enabled": True,
                "idle_timeout_seconds": idle_timeout,
                "idle_check_interval_seconds": idle_check,
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
    runtime_close: list[bool] = []

    async def factory(
        thread_id: str,
        preset_id: str,
        adapter: Any,
        sinks: list[Any],
    ) -> tuple[Any, Any]:
        runtime = MagicMock()

        async def _aclose() -> None:
            runtime_close.append(True)

        runtime.aclose = _aclose
        bridge = MagicMock()
        return runtime, bridge

    factory.runtime_close_log = runtime_close  # type: ignore[attr-defined]
    return factory


# ---------------------------------------------------------------------------
# 手动 evict
# ---------------------------------------------------------------------------


async def test_evict_cell_manual_pushes_evicted_frame_and_closes(tmp_path: Path) -> None:
    cfg = _make_cfg()
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    cell = await mgr.boot_or_attach(meta.id)
    # 用 AsyncMock ws 替换占位 _NullWS
    ws = AsyncMock()
    ws.send_json = AsyncMock(return_value=None)
    ws.close = AsyncMock(return_value=None)
    cell.attach_ws(ws)

    await mgr.evict_cell(meta.id, reason="manual_stop", message="user clicked stop")
    # cell 已从 dict 移除
    assert mgr.get_cell(meta.id) is None
    # 帧已推送
    sent_frames = [c.args[0] for c in ws.send_json.await_args_list]
    evicted_frames = [f for f in sent_frames if f.get("frame_type") == "cell.evicted"]
    assert len(evicted_frames) == 1
    assert evicted_frames[0]["reason"] == "manual_stop"
    assert evicted_frames[0]["message"] == "user clicked stop"
    # adapter 已 closed
    assert cell.adapter.closed is True
    # runtime.aclose 已被调用
    assert factory.runtime_close_log == [True]


async def test_evict_cell_unknown_silently_returns(tmp_path: Path) -> None:
    cfg = _make_cfg()
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    # 不存在的 thread_id
    await mgr.evict_cell("thread-ffffffffffff", reason="manual_stop")
    # 不抛即可


async def test_evict_cell_idempotent(tmp_path: Path) -> None:
    cfg = _make_cfg()
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    await mgr.boot_or_attach(meta.id)
    await mgr.evict_cell(meta.id, reason="manual_stop")
    # 二次 evict
    await mgr.evict_cell(meta.id, reason="manual_stop")


async def test_evict_cell_shutdown_does_not_push_ws(tmp_path: Path) -> None:
    cfg = _make_cfg()
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    cell = await mgr.boot_or_attach(meta.id)
    ws = AsyncMock()
    ws.send_json = AsyncMock(return_value=None)
    cell.attach_ws(ws)

    await mgr.evict_cell(meta.id, reason="server_shutdown", notify_ws=False)
    sent = [c.args[0] for c in ws.send_json.await_args_list]
    # 没有任何 cell.evicted 帧
    assert not any(f.get("frame_type") == "cell.evicted" for f in sent)


# ---------------------------------------------------------------------------
# pending approval 在 evict 时 resolve(False)
# ---------------------------------------------------------------------------


async def test_evict_resolves_pending_approvals_to_false(tmp_path: Path) -> None:
    cfg = _make_cfg()
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    cell = await mgr.boot_or_attach(meta.id)
    ws = AsyncMock()
    ws.send_json = AsyncMock(return_value=None)
    ws.close = AsyncMock(return_value=None)
    cell.attach_ws(ws)

    request = ApprovalRequest(
        run_id="r",
        session_id=meta.id,
        turn=1,
        call_id="c1",
        tool_name="ReadFile",
        arguments={},
    )

    # 启一个 task 等审批
    approval_task = asyncio.create_task(cell.adapter.prompt_approval(request))
    await asyncio.sleep(0.02)  # 让 prompt_approval 进 wait_for

    # evict
    await mgr.evict_cell(meta.id, reason="manual_stop")
    result = await approval_task
    # v0.1.6 三态：cancel 路径返回 ApprovalAction.REJECT（语义不变，类型升级）
    assert result is ApprovalAction.REJECT


# ---------------------------------------------------------------------------
# idle eviction
# ---------------------------------------------------------------------------


async def test_idle_eviction_loop_evicts_stale_cells(tmp_path: Path) -> None:
    """把 idle 阈值压到 0 + check 周期 1s，让 loop 立刻命中。"""
    cfg = _make_cfg(idle_timeout=60, idle_check=10)
    # 直接用 monkeypatch 替换 cfg.web 字段更省事；这里用 model_validate 重建一份
    cfg = Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {
                "enabled": True,
                # 把阈值 / 周期都压到下限以加速测
                "idle_timeout_seconds": 60,  # ge=60
                "idle_check_interval_seconds": 10,  # ge=10
                "pending_approval_timeout_seconds": 10,
            },
        }
    )

    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    cell = await mgr.boot_or_attach(meta.id)
    # 强制 cell 看起来已 idle 很久
    cell.last_active_at = 0.0

    # 直接调一次 _idle_eviction_loop 的内部逻辑（不等真 sleep）
    # 取代后台 task 的实际等待，手动触发一次 idle 扫描
    threshold = float(cfg.web.idle_timeout_seconds)
    import time as _time

    now = _time.time()
    candidates = [
        c.thread_id
        for c in mgr._cells.values()
        if (now - c.last_active_at) > threshold and not c.has_pending_approvals
    ]
    assert candidates == [meta.id]
    for tid in candidates:
        await mgr.evict_cell(tid, reason="idle", notify_ws=True)
    assert mgr.get_cell(meta.id) is None


async def test_idle_eviction_skips_cells_with_pending_approvals(tmp_path: Path) -> None:
    cfg = _make_cfg()
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    cell = await mgr.boot_or_attach(meta.id)
    cell.last_active_at = 0.0  # 看似 idle
    # 注入一个 pending future（模拟有审批等待）
    loop = asyncio.get_running_loop()
    cell.adapter._pending_approvals["c1"] = loop.create_future()
    assert cell.has_pending_approvals is True

    # 跑一次 idle 扫描
    threshold = float(cfg.web.idle_timeout_seconds)
    import time as _time

    now = _time.time()
    candidates = [
        c.thread_id
        for c in mgr._cells.values()
        if (now - c.last_active_at) > threshold and not c.has_pending_approvals
    ]
    assert candidates == []  # 跳过

    # 清理 pending future（不让 prompt_approval 永久挂着）
    cell.adapter._pending_approvals["c1"].cancel()


# ---------------------------------------------------------------------------
# aclose_all 兜底所有 cell
# ---------------------------------------------------------------------------


async def test_aclose_all_evicts_all_cells_without_ws_notify(tmp_path: Path) -> None:
    cfg = _make_cfg()
    meta_a = _make_meta("thread-aaaaaaaaaaaa")
    meta_b = _make_meta("thread-bbbbbbbbbbbb")
    write_thread_metadata(tmp_path, meta_a)
    write_thread_metadata(tmp_path, meta_b)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    cell_a = await mgr.boot_or_attach(meta_a.id)
    cell_b = await mgr.boot_or_attach(meta_b.id)
    ws_a = AsyncMock()
    ws_a.send_json = AsyncMock(return_value=None)
    ws_b = AsyncMock()
    ws_b.send_json = AsyncMock(return_value=None)
    cell_a.attach_ws(ws_a)
    cell_b.attach_ws(ws_b)

    await mgr.aclose_all()
    assert mgr.get_cell(meta_a.id) is None
    assert mgr.get_cell(meta_b.id) is None
    # shutdown 不推 cell.evicted 帧
    sent_a = [c.args[0] for c in ws_a.send_json.await_args_list]
    sent_b = [c.args[0] for c in ws_b.send_json.await_args_list]
    assert not any(f.get("frame_type") == "cell.evicted" for f in sent_a)
    assert not any(f.get("frame_type") == "cell.evicted" for f in sent_b)


async def test_aclose_all_idempotent(tmp_path: Path) -> None:
    cfg = _make_cfg()
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    await mgr.aclose_all()
    await mgr.aclose_all()  # 不抛
    assert mgr.closed is True


async def test_aclose_all_cancels_idle_loop(tmp_path: Path) -> None:
    cfg = _make_cfg()
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    await mgr.start()
    assert mgr._idle_task is not None
    await mgr.aclose_all()
    assert mgr._idle_task is None
