"""thread-status producer adapter 与 EventSink 的 run lease 合同测试。"""

from __future__ import annotations

import pytest

from core.contracts import Event
from hosts.web.websocket.thread_status import (
    ThreadStatusEventSink,
    get_thread_status_manager,
    publish_normalized_status,
    reset_broadcaster_for_testing,
)
from hosts.web.websocket.thread_status_manager import ThreadStatusManager

THREAD_ID = "thread-abcdef123456"


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    """隔离模块级 ThreadStatusManager。"""
    reset_broadcaster_for_testing()
    yield
    reset_broadcaster_for_testing()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("normalized", "phase", "tool_name"),
    [
        ({"frame_type": "stream_status", "phase": "responding"}, "responding", None),
        ({"frame_type": "stream_status", "phase": "thinking"}, "thinking", None),
        (
            {
                "frame_type": "stream_status",
                "phase": "tool_calling",
                "toolName": "Read",
            },
            "tool_calling",
            "Read",
        ),
        ({"frame_type": "permission_request"}, "waiting_approval", None),
        ({"frame_type": "permission_cancelled"}, "idle", None),
        ({"frame_type": "complete"}, "complete", None),
        ({"frame_type": "error"}, "error", None),
    ],
)
async def test_publish_normalized_status_maps_canonical_phase(
    normalized: dict[str, object],
    phase: str,
    tool_name: str | None,
) -> None:
    """Claude/Codex adapter 只把 canonical phase 交给同一个 Manager。"""
    manager = ThreadStatusManager()
    lease = await manager.begin_run(THREAD_ID, "run-1")

    accepted = await publish_normalized_status(manager, lease, normalized)

    assert accepted is True
    if phase in {"responding", "thinking", "tool_calling", "waiting_approval"}:
        frame = manager.active_statuses[THREAD_ID]
        assert frame.phase == phase
        assert frame.toolName == tool_name
    else:
        assert THREAD_ID not in manager.active_statuses


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "normalized",
    [
        {},
        {"frame_type": "text"},
        {"frame_type": "stream_status", "phase": "unknown"},
    ],
)
async def test_publish_normalized_status_ignores_non_state_frames(
    normalized: dict[str, object],
) -> None:
    """内容帧和未知 phase 不创建 active 投影。"""
    manager = ThreadStatusManager()
    lease = await manager.begin_run(THREAD_ID, "run-1")

    assert await publish_normalized_status(manager, lease, normalized) is False
    assert manager.active_statuses == {}


@pytest.mark.asyncio
async def test_event_sink_uses_one_lease_and_terminal_removes_active_status() -> None:
    """generic EventSink 在 run 内复用 lease，终态删除 active 项。"""
    sink = ThreadStatusEventSink(THREAD_ID)
    manager = get_thread_status_manager()

    await sink.emit(
        Event(
            kind="tool.call.start",
            run_id="run-1",
            turn=1,
            payload={"tool_name": "run_shell"},
        )
    )
    running = manager.active_statuses[THREAD_ID]
    assert running.phase == "tool_calling"
    assert running.toolName == "run_shell"
    assert running.runGeneration == 1

    await sink.emit(
        Event(
            kind="run.end",
            run_id="run-1",
            turn=1,
            payload={"status": "completed", "run_end_reason": 1},
        )
    )

    assert THREAD_ID not in manager.active_statuses
    assert manager.sequence == 2


@pytest.mark.asyncio
async def test_event_sink_old_run_terminal_cannot_clear_new_run() -> None:
    """SC_27：新 run 取得 generation 后，旧 run 终态会被 Manager 拒绝。"""
    sink = ThreadStatusEventSink(THREAD_ID)
    manager = get_thread_status_manager()

    await sink.emit(Event(kind="turn.start", run_id="run-old", turn=1))
    await sink.emit(Event(kind="turn.start", run_id="run-new", turn=1))
    await sink.emit(
        Event(
            kind="run.end",
            run_id="run-old",
            turn=1,
            payload={"status": "completed", "run_end_reason": 1},
        )
    )

    active = manager.active_statuses[THREAD_ID]
    assert active.runId == "run-new"
    assert active.runGeneration == 2
