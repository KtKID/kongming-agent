"""ThreadStatusManager 的 active snapshot、run lease 与连接队列合同测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from hosts.web.websocket.thread_status_manager import ThreadStatusManager


class _RecordingWebSocket:
    """记录 Manager 单 writer 发送的帧，并允许测试模拟慢连接。"""

    def __init__(self, *, blocked: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed: list[tuple[int, str]] = []
        self._send_gate = asyncio.Event()
        self._sending = False
        if not blocked:
            self._send_gate.set()

    async def send_json(self, payload: dict[str, Any]) -> None:
        """等待发送闸门后记录 payload。"""
        self._sending = True
        try:
            await self._send_gate.wait()
            self.sent.append(payload)
        finally:
            self._sending = False

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        """记录 Manager 主动关闭慢连接的 code 与 reason。"""
        assert self._sending is False
        self.closed.append((code, reason))
        self._send_gate.set()


async def _wait_for_frames(ws: _RecordingWebSocket, count: int) -> None:
    """等待 fake websocket 收到指定数量帧。"""
    async with asyncio.timeout(1):
        while len(ws.sent) < count:
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_attach_first_frame_is_active_snapshot_then_sequence_delta() -> None:
    """SC_06：新连接先拿 active snapshot，后续增量 sequence 严格递增。"""
    manager = ThreadStatusManager()
    lease_a = await manager.begin_run("thread-a", "run-a")
    lease_b = await manager.begin_run("thread-b", "run-b")
    await manager.publish_status(lease_a, phase="responding")
    await manager.publish_status(lease_b, phase="tool_calling", tool_name="Read")

    ws = _RecordingWebSocket()
    await manager.attach(ws)
    await _wait_for_frames(ws, 1)

    snapshot = ws.sent[0]
    assert snapshot["frame_type"] == "thread-status.snapshot"
    assert snapshot["watermark"] == 2
    assert {
        (item["threadId"], item["phase"], item["runId"], item["runGeneration"])
        for item in snapshot["items"]
    } == {
        ("thread-a", "responding", "run-a", 1),
        ("thread-b", "tool_calling", "run-b", 1),
    }

    await manager.publish_status(lease_a, phase="thinking")
    await _wait_for_frames(ws, 2)
    assert ws.sent[1]["sequence"] == 3
    assert ws.sent[1]["runId"] == "run-a"
    assert ws.sent[1]["runGeneration"] == 1
    await manager.detach(ws)


@pytest.mark.asyncio
async def test_stale_terminal_cannot_clear_new_run() -> None:
    """SC_27：旧 run 的迟到终态无法覆盖同 thread 的新 generation。"""
    manager = ThreadStatusManager()
    old_lease = await manager.begin_run("thread-a", "run-1")
    await manager.publish_status(old_lease, phase="responding")
    new_lease = await manager.begin_run("thread-a", "run-2")
    await manager.publish_status(new_lease, phase="responding")

    accepted = await manager.publish_status(old_lease, phase="complete")

    assert accepted is False
    active = manager.active_statuses
    assert active["thread-a"].runId == "run-2"
    assert active["thread-a"].runGeneration == 2
    assert active["thread-a"].phase == "responding"

    accepted = await manager.publish_status(new_lease, phase="complete")
    assert accepted is True
    assert "thread-a" not in manager.active_statuses


@pytest.mark.asyncio
async def test_attach_and_terminal_share_one_ordered_lock_boundary() -> None:
    """SC_07：attach 与 terminal 交错后，客户端最终不会停留在 running。"""
    manager = ThreadStatusManager()
    lease = await manager.begin_run("thread-a", "run-a")
    await manager.publish_status(lease, phase="responding")
    ws = _RecordingWebSocket()

    await asyncio.gather(
        manager.attach(ws),
        manager.publish_status(lease, phase="complete"),
    )
    await _wait_for_frames(ws, 1)
    await asyncio.sleep(0)

    snapshot = ws.sent[0]
    snapshot_has_thread = any(item["threadId"] == "thread-a" for item in snapshot["items"])
    if snapshot_has_thread:
        await _wait_for_frames(ws, 2)
    terminal_seen = any(
        frame.get("frame_type") == "thread-status"
        and frame.get("threadId") == "thread-a"
        and frame.get("phase") == "complete"
        for frame in ws.sent[1:]
    )
    assert snapshot_has_thread is False or terminal_seen is True
    assert "thread-a" not in manager.active_statuses
    await manager.detach(ws)


@pytest.mark.asyncio
async def test_slow_connection_overflow_closes_only_that_connection() -> None:
    """SC_09：慢连接队列溢出会被关闭，正常连接与 active map 继续工作。"""
    manager = ThreadStatusManager(queue_size=2)
    slow = _RecordingWebSocket(blocked=True)
    fast = _RecordingWebSocket()
    await manager.attach(slow)
    await manager.attach(fast)
    await _wait_for_frames(fast, 1)

    lease = await manager.begin_run("thread-a", "run-a")
    await manager.publish_status(lease, phase="responding")
    await _wait_for_frames(fast, 2)
    await manager.publish_status(lease, phase="thinking")
    await _wait_for_frames(fast, 3)
    await manager.publish_status(lease, phase="tool_calling")
    await _wait_for_frames(fast, 4)
    async with asyncio.timeout(1):
        while not slow.closed:
            await asyncio.sleep(0)
        while manager.connection_count != 1:
            await asyncio.sleep(0)

    assert manager.connection_count == 1
    assert manager.active_statuses["thread-a"].phase == "tool_calling"
    assert slow.closed[0][0] == 1013
    await manager.detach(fast)
