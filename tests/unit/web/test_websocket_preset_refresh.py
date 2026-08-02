"""Generic Chat WebSocket preset 刷新单测。

覆盖：
1. ``user.input`` 分派前刷新 runtime 失败时，只向前端推 error 帧。
2. 刷新成功时才创建 ``run_once`` task，并把任务记录到 cell。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from hosts.web.protocol import UserInputFrame
from hosts.web.websocket.routes import _dispatch_frame


class _FakeWebSocket:
    """测试 websocket：记录后端发出的 JSON 帧。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        """记录一次 ``send_json`` 调用，返回值与 FastAPI WebSocket 对齐。"""
        self.sent.append(payload)


class _FakeBridge:
    """测试 bridge：用事件控制 ``run_once`` 是否结束。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.release = asyncio.Event()

    async def run_once(
        self,
        text: str,
        *,
        reasoning_effort: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
    ) -> None:
        """记录用户输入参数，并等待测试释放任务。"""
        self.calls.append(
            {
                "text": text,
                "reasoning_effort": reasoning_effort,
                "attachments": attachments,
            }
        )
        await self.release.wait()


class _FakeCell:
    """测试 cell：只提供 ``_dispatch_frame`` 需要的字段。"""

    def __init__(self) -> None:
        self.thread_id = "thread-aaaaaaaaaaaa"
        self.bridge = _FakeBridge()
        self.current_run_task: asyncio.Task[Any] | None = None


class _FakeThreadManager:
    """测试 ThreadManager：返回预设刷新结果并记录调用。"""

    def __init__(self, refreshed: bool) -> None:
        self.refreshed = refreshed
        self.calls: list[str] = []

    async def ensure_cell_runtime_preset_current(self, thread_id: str) -> bool:
        """记录刷新目标 thread，并返回预设结果。"""
        self.calls.append(thread_id)
        return self.refreshed


@pytest.mark.asyncio
async def test_user_input_sends_error_when_runtime_refresh_fails() -> None:
    """runtime 刷新失败时，不创建后台 run task。"""
    websocket = _FakeWebSocket()
    cell = _FakeCell()
    tm = _FakeThreadManager(refreshed=False)
    frame = UserInputFrame(
        text="hello",
        request_id="req-1",
        reasoning_effort="high",
    )

    await _dispatch_frame(
        frame,
        cell,
        websocket,  # type: ignore[arg-type]
        tm,  # type: ignore[arg-type]
        cell.thread_id,
        "conn-test",
    )

    assert tm.calls == [cell.thread_id]
    assert cell.current_run_task is None
    assert cell.bridge.calls == []
    assert websocket.sent == [
        {
            "frame_type": "error",
            "error_code": "internal",
            "message": "模型切换尚未完成，runtime 刷新失败；请稍后重试。",
            "turn": None,
            "reason": None,
            "timestamp_ms": websocket.sent[0]["timestamp_ms"],
        }
    ]


@pytest.mark.asyncio
async def test_user_input_starts_run_task_after_runtime_refresh() -> None:
    """runtime 刷新成功后，正常启动后台 run task。"""
    websocket = _FakeWebSocket()
    cell = _FakeCell()
    tm = _FakeThreadManager(refreshed=True)
    frame = UserInputFrame(
        text="hello",
        request_id="req-1",
        reasoning_effort="high",
    )

    await _dispatch_frame(
        frame,
        cell,
        websocket,  # type: ignore[arg-type]
        tm,  # type: ignore[arg-type]
        cell.thread_id,
        "conn-test",
    )

    assert tm.calls == [cell.thread_id]
    assert cell.current_run_task is not None
    await asyncio.sleep(0)
    assert cell.bridge.calls == [
        {
            "text": "hello",
            "reasoning_effort": "high",
            "attachments": None,
        }
    ]

    cell.bridge.release.set()
    await cell.current_run_task
    assert cell.current_run_task is None
    assert websocket.sent == []
