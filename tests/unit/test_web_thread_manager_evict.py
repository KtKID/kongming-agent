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
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from hosts.web.threads.cell import ThreadCellStatus
from hosts.web.threads.manager import ThreadManager
from hosts.web.threads.metadata import ThreadMetadata, write_thread_metadata
from infrastructure.config.models import Config
from safety.approval.manager import ApprovalManager
from safety.approval.permissions_manager import PermissionsManager


def _write_model_catalog(tmp_path: Path) -> None:
    """写入 eviction 测试使用的当前模型目录真源。"""
    (tmp_path / "model-providers.yaml").write_text(
        """\
version: 2
providers:
  - provider_id: eviction-test
    default_preset_id: p1
    display_name: Eviction Test
    region_label: Local
    description: eviction fixture
    logo_text: E
    protocol: openai
    default_base_url: http://127.0.0.1:1234/v1
    request_defaults: {}
    models:
      - preset_id: p1
        display_name: P1
        model: fake
""",
        encoding="utf-8",
    )


def _make_cfg(
    tmp_path: Path,
    idle_timeout: int = 60,
    idle_check: int = 10,
) -> Config:
    """返回使用 preset_id 配置入口的 eviction 测试配置。"""
    _write_model_catalog(tmp_path)
    return Config.model_validate(
        {
            "model": {"preset_id": "p1"},
            "web": {
                "enabled": True,
                "idle_timeout_seconds": idle_timeout,
                "idle_check_interval_seconds": idle_check,
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
    dispatcher_close: list[bool] = []

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
        dispatcher = MagicMock()
        dispatcher.reset_for_reuse = AsyncMock()

        async def _dispatcher_aclose(*, drain: bool = False) -> None:
            dispatcher_close.append(drain)

        dispatcher.aclose = _dispatcher_aclose
        return runtime, dispatcher

    factory.runtime_close_log = runtime_close  # type: ignore[attr-defined]
    factory.dispatcher_close_log = dispatcher_close  # type: ignore[attr-defined]
    return factory


# ---------------------------------------------------------------------------
# 手动 evict
# ---------------------------------------------------------------------------


async def test_evict_cell_manual_pushes_evicted_frame_and_closes(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
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
    assert factory.dispatcher_close_log == [False]


async def test_evict_cell_unknown_silently_returns(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    # 不存在的 thread_id
    await mgr.evict_cell("thread-ffffffffffff", reason="manual_stop")
    # 不抛即可


async def test_evict_cell_idempotent(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    await mgr.boot_or_attach(meta.id)
    await mgr.evict_cell(meta.id, reason="manual_stop")
    # 二次 evict
    await mgr.evict_cell(meta.id, reason="manual_stop")


async def test_evict_cell_shutdown_does_not_push_ws(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
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
# pending approval 在 evict 时由 ApprovalManager 取消
# ---------------------------------------------------------------------------


async def test_evict_cancels_approval_manager_pending(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg(tmp_path)
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    approval_manager = ApprovalManager(
        permissions_manager=PermissionsManager(tmp_path), default_timeout_ms=10_000
    )
    mgr.set_approval_manager(approval_manager)

    cell = await mgr.boot_or_attach(meta.id)
    ws = AsyncMock()
    ws.send_json = AsyncMock(return_value=None)
    ws.close = AsyncMock(return_value=None)
    cell.attach_ws(ws)

    approval_task = asyncio.create_task(
        approval_manager.request(
            channel="generic_chat",
            thread_id=meta.id,
            cwd="/",
            tool_name="ReadFile",
            tool_input={},
        ),
    )
    await asyncio.sleep(0.02)
    assert approval_manager.pending_count_for_thread(meta.id, channel="generic_chat") == 1

    # evict
    await mgr.evict_cell(meta.id, reason="manual_stop")
    result = await approval_task
    assert result.outcome == "rejected"
    assert result.metadata.get("reason") == "cell_evict"
    assert approval_manager.pending_count_for_thread(meta.id, channel="generic_chat") == 0


async def test_close_ephemeral_session_cell_cancels_approval_manager_pending(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg(tmp_path)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    approval_manager = ApprovalManager(
        permissions_manager=PermissionsManager(tmp_path), default_timeout_ms=10_000
    )
    mgr.set_approval_manager(approval_manager)
    session_id = "sched-task-1-run-1"

    cell = await mgr.build_ephemeral_session_cell(
        session_id=session_id,
        preset_id="p1",
    )
    approval_task = asyncio.create_task(
        approval_manager.request(
            channel="generic_chat",
            thread_id=session_id,
            cwd="/",
            tool_name="ReadFile",
            tool_input={},
        ),
    )
    await asyncio.sleep(0.02)
    assert approval_manager.pending_count_for_thread(session_id, channel="generic_chat") == 1

    await mgr.close_ephemeral_session_cell(cell, reason="session_close")
    result = await approval_task
    assert result.outcome == "rejected"
    assert result.metadata.get("reason") == "session_close"
    assert approval_manager.pending_count_for_thread(session_id, channel="generic_chat") == 0
    assert cell.adapter.closed is True
    assert factory.runtime_close_log == [True]
    assert factory.dispatcher_close_log == [False]


# ---------------------------------------------------------------------------
# idle eviction
# ---------------------------------------------------------------------------


async def test_idle_eviction_evicts_stale_ownerless_cells(tmp_path: Path) -> None:
    """过期且没有 owner work 的 cell 会走最终复核路径回收。"""
    cfg = _make_cfg(tmp_path)
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    cell = await mgr.boot_or_attach(meta.id)
    # 强制 cell 看起来已 idle 很久
    cell.last_active_at = 0.0

    evicted = await mgr._evict_cell_if_idle(
        meta.id,
        now=10_000.0,
        threshold=60.0,
    )

    assert evicted is True
    assert mgr.get_cell(meta.id) is None
    assert factory.runtime_close_log == [True]
    assert factory.dispatcher_close_log == [False]


async def test_idle_eviction_skips_cells_with_pending_approvals(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    approval_manager = ApprovalManager(
        permissions_manager=PermissionsManager(tmp_path), default_timeout_ms=10_000
    )
    mgr.set_approval_manager(approval_manager)

    cell = await mgr.boot_or_attach(meta.id)
    cell.last_active_at = 0.0  # 看似 idle

    approval_task = asyncio.create_task(
        approval_manager.request(
            channel="generic_chat",
            thread_id=meta.id,
            cwd="/",
            tool_name="ReadFile",
            tool_input={},
        ),
    )
    await asyncio.sleep(0.02)
    assert mgr._has_pending_approval(meta.id) is True

    # 跑一次 idle 扫描
    threshold = float(cfg.web.idle_timeout_seconds)
    import time as _time

    now = _time.time()
    candidates = [
        c.thread_id
        for c in mgr._cells.values()
        if (now - c.last_active_at) > threshold and not mgr._has_pending_approval(c.thread_id)
    ]
    assert candidates == []  # 跳过

    approval_manager.cancel_by_thread(meta.id, reason="test_cleanup")
    result = await approval_task
    assert result.outcome == "rejected"


async def test_idle_eviction_skips_cells_with_active_run(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    cell = await mgr.boot_or_attach(meta.id)
    cell.last_active_at = 0.0
    run_task = asyncio.create_task(asyncio.sleep(60))
    cell.current_run_task = run_task

    try:
        evicted = await mgr._evict_cell_if_idle(
            meta.id,
            now=10_000.0,
            threshold=60.0,
        )

        assert evicted is False
        assert mgr.get_cell(meta.id) is cell
    finally:
        run_task.cancel()
        with suppress(asyncio.CancelledError):
            await run_task


async def test_effective_status_precedence(tmp_path: Path) -> None:
    """现算唯一入口的优先级：evicting > awaiting_approval > running > idle。

    running / awaiting_approval 都从事实真源（current_run_task / pending approval）
    现算，不落字段；只有 evicting 是粘性写入字段，压过一切。
    """
    cfg = _make_cfg(tmp_path)
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    approval_manager = ApprovalManager(
        permissions_manager=PermissionsManager(tmp_path), default_timeout_ms=10_000
    )
    mgr.set_approval_manager(approval_manager)

    cell = await mgr.boot_or_attach(meta.id)

    # 默认无任何工作 → idle
    assert mgr._effective_status(cell) is ThreadCellStatus.IDLE

    approval_task: asyncio.Task[Any] | None = None
    run_task = asyncio.create_task(asyncio.sleep(60))
    cell.current_run_task = run_task
    try:
        # 有 active run → running（现算自 current_run_task）
        assert mgr._effective_status(cell) is ThreadCellStatus.RUNNING

        # 有 pending approval → awaiting_approval，优先于 running
        approval_task = asyncio.create_task(
            approval_manager.request(
                channel="generic_chat",
                thread_id=meta.id,
                cwd="/",
                tool_name="ReadFile",
                tool_input={},
            ),
        )
        await asyncio.sleep(0.02)
        assert mgr._has_pending_approval(meta.id) is True
        assert mgr._effective_status(cell) is ThreadCellStatus.AWAITING_APPROVAL

        # evicting 粘性，压过 running / awaiting_approval
        mgr._set_cell_status(cell, ThreadCellStatus.EVICTING)
        assert mgr._effective_status(cell) is ThreadCellStatus.EVICTING
    finally:
        approval_manager.cancel_by_thread(meta.id, reason="test_cleanup")
        if approval_task is not None:
            with suppress(asyncio.CancelledError):
                await approval_task
        run_task.cancel()
        with suppress(asyncio.CancelledError):
            await run_task


async def test_idle_eviction_skips_cells_with_pending_input_queue(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    cell = await mgr.boot_or_attach(meta.id)
    cell.last_active_at = 0.0
    cell.pending_inputs = [
        mgr._create_pending_input(
            cell,
            "queued",
            source="user_input",
            priority="user_message",
            metadata={},
        )
    ]

    evicted = await mgr._evict_cell_if_idle(
        meta.id,
        now=10_000.0,
        threshold=60.0,
    )

    assert evicted is False
    assert mgr.get_cell(meta.id) is cell


async def test_idle_eviction_skips_done_run_before_drain_clears_owner(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg(tmp_path)
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    cell = await mgr.boot_or_attach(meta.id)
    cell.last_active_at = 0.0
    completed_task = asyncio.create_task(asyncio.sleep(0))
    await completed_task
    cell.current_run_task = completed_task

    evicted = await mgr._evict_cell_if_idle(
        meta.id,
        now=10_000.0,
        threshold=60.0,
    )

    assert evicted is False
    assert mgr.get_cell(meta.id) is cell


async def test_pending_run_done_restores_idle_status(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    cell = await mgr.boot_or_attach(meta.id)
    result = MagicMock()
    result.status = "completed"

    async def _completed_run() -> Any:
        return result

    run_task = asyncio.create_task(_completed_run())
    await run_task
    cell.current_run_task = run_task

    await mgr._handle_pending_run_done(cell, run_task, run_task)

    # drain 清空 current_run_task 后，现算入口自然回到 idle（不再依赖散写字段）。
    assert cell.current_run_task is None
    assert mgr._effective_status(cell) is ThreadCellStatus.IDLE


async def test_idle_eviction_skips_cells_with_drain_block_reason(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    cell = await mgr.boot_or_attach(meta.id)
    cell.last_active_at = 0.0
    cell.pending_input_drain_block_reason = "runtime_refresh_failed"

    evicted = await mgr._evict_cell_if_idle(
        meta.id,
        now=10_000.0,
        threshold=60.0,
    )

    assert evicted is False
    assert mgr.get_cell(meta.id) is cell


async def test_idle_eviction_rechecks_owner_work_after_candidate_scan(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg(tmp_path)
    meta = _make_meta()
    write_thread_metadata(tmp_path, meta)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)

    cell = await mgr.boot_or_attach(meta.id)
    cell.last_active_at = 0.0
    candidates = [
        stale_cell.thread_id
        for stale_cell in mgr._cells.values()
        if (10_000.0 - stale_cell.last_active_at) > 60.0
    ]
    assert candidates == [meta.id]

    run_task = asyncio.create_task(asyncio.sleep(60))
    cell.current_run_task = run_task
    try:
        evicted = await mgr._evict_cell_if_idle(
            candidates[0],
            now=10_000.0,
            threshold=60.0,
        )

        assert evicted is False
        assert mgr.get_cell(meta.id) is cell
    finally:
        run_task.cancel()
        with suppress(asyncio.CancelledError):
            await run_task


# ---------------------------------------------------------------------------
# aclose_all 兜底所有 cell
# ---------------------------------------------------------------------------


async def test_aclose_all_evicts_all_cells_without_ws_notify(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
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
    assert factory.dispatcher_close_log == [False, False]


async def test_aclose_all_idempotent(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    await mgr.aclose_all()
    await mgr.aclose_all()  # 不抛
    assert mgr.closed is True


async def test_aclose_all_cancels_idle_loop(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    factory = _make_factory()
    mgr = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    await mgr.start()
    assert mgr._idle_task is not None
    await mgr.aclose_all()
    assert mgr._idle_task is None
