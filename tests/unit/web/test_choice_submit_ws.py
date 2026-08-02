"""choice.submit WebSocket 分发测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from hosts.web.protocol.ws_frames import (
    ChoiceSubmitFrame,
    PendingInputReorderFrame,
    PendingInputSendNowFrame,
    UserInputFrame,
)
from hosts.web.threads.manager import PendingInputOperationError
from hosts.web.websocket import routes


class _Cell:
    def __init__(self) -> None:
        self.current_run_task: asyncio.Task[Any] | None = None
        self.thread_id = "thread-aaaaaaaaaaaa"


class _ThreadManager:
    def __init__(self, *, started: bool = True) -> None:
        self.started = started
        self.calls: list[dict[str, Any]] = []
        self.user_input_calls: list[dict[str, Any]] = []
        self.reorder_calls: list[dict[str, Any]] = []
        self.send_now_calls: list[dict[str, Any]] = []
        self.send_now_error: Exception | None = None

    async def submit_choice_result(
        self,
        thread_id: str,
        choice_text: str,
        *,
        request_id: str,
    ) -> Any:
        self.calls.append(
            {
                "thread_id": thread_id,
                "choice_text": choice_text,
                "request_id": request_id,
            }
        )
        return SimpleNamespace(started=self.started)

    async def submit_user_input(
        self,
        thread_id: str,
        text: str,
        *,
        request_id: str | None = None,
        reasoning_effort: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
        source_conn_id: str | None = None,
    ) -> Any:
        self.user_input_calls.append(
            {
                "thread_id": thread_id,
                "text": text,
                "request_id": request_id,
                "reasoning_effort": reasoning_effort,
                "attachments": attachments,
                "references": references,
                "source_conn_id": source_conn_id,
            }
        )
        return SimpleNamespace(started=self.started)

    async def reorder_pending_inputs(
        self,
        thread_id: str,
        ordered_ids: list[str],
    ) -> Any:
        self.reorder_calls.append(
            {
                "thread_id": thread_id,
                "ordered_ids": ordered_ids,
            }
        )
        return SimpleNamespace(items=[])

    async def send_pending_input_now(
        self,
        thread_id: str,
        pending_input_id: str,
    ) -> Any:
        if self.send_now_error is not None:
            raise self.send_now_error
        self.send_now_calls.append(
            {
                "thread_id": thread_id,
                "pending_input_id": pending_input_id,
            }
        )
        return SimpleNamespace(items=[])


class _QueueCell(_Cell):
    def __init__(self) -> None:
        super().__init__()
        self.pending_input_lock = object()


class _HostDispatcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.release = asyncio.Event()

    async def submit(
        self,
        text: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.calls.append(
            {
                "text": text,
                "metadata": metadata,
                "attachments": attachments,
                "references": references,
            }
        )
        await self.release.wait()


class _EphemeralCell(_Cell):
    def __init__(self) -> None:
        super().__init__()
        self.thread_id = "sched-session-1"
        self.host_dispatcher = _HostDispatcher()


def _frame() -> ChoiceSubmitFrame:
    return ChoiceSubmitFrame(
        request_id="call-1",
        answers=[
            {
                "question_id": "scope",
                "option_id": "minimal",
                "option_label": "最小实现",
                "value": {"scope": "minimal"},
            },
            {
                "question_id": "note",
                "option_id": "__custom__",
                "option_label": "自己输入",
                "custom_text": "只做 Web。",
            },
        ],
    )


def test_format_choice_submit_as_user_input() -> None:
    text = routes.format_choice_submit_as_user_input(_frame())

    assert "用户已完成选择：" in text
    assert "request_id: call-1" in text
    assert "1. question_id=scope" in text
    assert "选择：minimal / 最小实现" in text
    assert 'value: {"scope": "minimal"}' in text
    assert "自定义：只做 Web。" in text


async def test_dispatch_user_input_uses_thread_manager_for_metadata_thread() -> None:
    cell = _QueueCell()
    ws = AsyncMock()
    tm = _ThreadManager(started=True)

    await routes._dispatch_frame(
        UserInputFrame(text="hello", request_id="req-1", reasoning_effort="high"),
        cell,
        ws,
        tm=tm,
        thread_id="thread-aaaaaaaaaaaa",
        conn_id="conn-1",
    )

    assert tm.user_input_calls == [
        {
            "thread_id": "thread-aaaaaaaaaaaa",
            "text": "hello",
            "request_id": "req-1",
            "reasoning_effort": "high",
            "attachments": None,
            "references": None,
            "source_conn_id": "conn-1",
        }
    ]
    assert cell.current_run_task is None


async def test_dispatch_user_input_runs_ephemeral_cell_without_thread_metadata() -> None:
    cell = _EphemeralCell()
    ws = AsyncMock()
    tm = _ThreadManager(started=True)

    await routes._dispatch_frame(
        UserInputFrame(text="cron follow up", request_id="req-cron"),
        cell,
        ws,
        tm=tm,
        thread_id="sched-session-1",
        conn_id="conn-cron",
    )

    assert tm.user_input_calls == []
    assert cell.current_run_task is not None
    await asyncio.sleep(0)
    assert cell.host_dispatcher.calls == [
        {
            "text": "cron follow up",
            "metadata": None,
            "attachments": None,
            "references": None,
        }
    ]

    cell.host_dispatcher.release.set()
    await cell.current_run_task
    assert cell.current_run_task is None


async def test_dispatch_choice_submit_uses_thread_manager_entry() -> None:
    cell = _QueueCell()
    ws = AsyncMock()
    tm = _ThreadManager(started=True)

    await routes._dispatch_frame(
        _frame(),
        cell,
        ws,
        tm=tm,
        thread_id="thread-aaaaaaaaaaaa",
        conn_id="conn-1",
    )

    assert len(tm.calls) == 1
    assert tm.calls[0]["thread_id"] == "thread-aaaaaaaaaaaa"
    assert tm.calls[0]["request_id"] == "call-1"
    assert "request_id: call-1" in tm.calls[0]["choice_text"]
    assert ws.send_json.await_count == 0


async def test_dispatch_choice_submit_runs_ephemeral_cell_without_thread_metadata() -> None:
    cell = _EphemeralCell()
    ws = AsyncMock()
    tm = _ThreadManager(started=True)

    await routes._dispatch_frame(
        _frame(),
        cell,
        ws,
        tm=tm,
        thread_id="sched-session-1",
        conn_id="conn-cron",
    )

    assert tm.calls == []
    assert cell.current_run_task is not None
    await asyncio.sleep(0)
    assert len(cell.host_dispatcher.calls) == 1
    assert "用户已完成选择：" in cell.host_dispatcher.calls[0]["text"]
    assert "request_id: call-1" in cell.host_dispatcher.calls[0]["text"]
    assert cell.host_dispatcher.calls[0]["metadata"] is None
    assert cell.host_dispatcher.calls[0]["attachments"] is None
    assert cell.host_dispatcher.calls[0]["references"] is None

    cell.host_dispatcher.release.set()
    await cell.current_run_task
    assert cell.current_run_task is None


async def test_dispatch_choice_submit_queues_during_active_run() -> None:
    cell = _QueueCell()
    cell.current_run_task = asyncio.create_task(asyncio.sleep(10))
    ws = AsyncMock()
    ws.send_json = AsyncMock(return_value=None)
    tm = _ThreadManager(started=False)

    try:
        await routes._dispatch_frame(
            _frame(),
            cell,
            ws,
            tm=tm,
            thread_id="thread-aaaaaaaaaaaa",
            conn_id="conn-1",
        )
    finally:
        cell.current_run_task.cancel()

    assert len(tm.calls) == 1
    assert ws.send_json.await_count == 0


async def test_dispatch_pending_input_reorder_uses_thread_manager_entry() -> None:
    cell = _Cell()
    ws = AsyncMock()
    tm = _ThreadManager(started=True)

    await routes._dispatch_frame(
        PendingInputReorderFrame(ordered_ids=["pin-1", "pin-4", "pin-2", "pin-3"]),
        cell,
        ws,
        tm=tm,
        thread_id="thread-aaaaaaaaaaaa",
        conn_id="conn-1",
    )

    assert tm.reorder_calls == [
        {
            "thread_id": "thread-aaaaaaaaaaaa",
            "ordered_ids": ["pin-1", "pin-4", "pin-2", "pin-3"],
        }
    ]
    assert ws.send_json.await_count == 0


async def test_dispatch_pending_input_send_now_uses_thread_manager_entry() -> None:
    cell = _Cell()
    ws = AsyncMock()
    tm = _ThreadManager(started=True)

    await routes._dispatch_frame(
        PendingInputSendNowFrame(pending_input_id="pin-1"),
        cell,
        ws,
        tm=tm,
        thread_id="thread-aaaaaaaaaaaa",
        conn_id="conn-1",
    )

    assert tm.send_now_calls == [
        {
            "thread_id": "thread-aaaaaaaaaaaa",
            "pending_input_id": "pin-1",
        }
    ]
    assert ws.send_json.await_count == 0


async def test_dispatch_pending_input_send_now_not_found_sends_reason() -> None:
    cell = _Cell()
    ws = AsyncMock()
    ws.send_json = AsyncMock(return_value=None)
    tm = _ThreadManager(started=True)
    tm.send_now_error = PendingInputOperationError(
        "pending_input_not_found",
        "pending input not found",
    )

    await routes._dispatch_frame(
        PendingInputSendNowFrame(pending_input_id="pin-missing"),
        cell,
        ws,
        tm=tm,
        thread_id="thread-aaaaaaaaaaaa",
        conn_id="conn-1",
    )

    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "error"
    assert sent["reason"] == "pending_input_not_found"


async def test_dispatch_pending_input_send_now_closed_registry_sends_reason() -> None:
    cell = _Cell()
    ws = AsyncMock()
    ws.send_json = AsyncMock(return_value=None)
    tm = _ThreadManager(started=True)
    tm.send_now_error = PendingInputOperationError(
        "root_agent_registry_closed",
        "root agent registry is closed",
    )

    await routes._dispatch_frame(
        PendingInputSendNowFrame(pending_input_id="pin-after-interrupt"),
        cell,
        ws,
        tm=tm,
        thread_id="thread-aaaaaaaaaaaa",
        conn_id="conn-1",
    )

    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "error"
    assert sent["reason"] == "root_agent_registry_closed"


async def test_dispatch_choice_submit_rejects_empty_answers() -> None:
    cell = _Cell()
    ws = AsyncMock()
    ws.send_json = AsyncMock(return_value=None)

    await routes._dispatch_frame(
        ChoiceSubmitFrame(request_id="call-1", answers=[]),
        cell,
        ws,
        tm=object(),
        thread_id="thread-aaaaaaaaaaaa",
        conn_id="conn-1",
    )

    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "error"
    assert "answers must contain at least one item" in sent["message"]
