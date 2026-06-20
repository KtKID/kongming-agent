"""choice.submit WebSocket 分发测试。"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

from hosts.web.protocol.ws_frames import ChoiceSubmitFrame
from hosts.web.websocket import routes


class _Cell:
    def __init__(self) -> None:
        self.current_run_task: asyncio.Task[Any] | None = None
        self.thread_id = "thread-aaaaaaaaaaaa"


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


async def test_dispatch_choice_submit_starts_run_once(monkeypatch) -> None:
    cell = _Cell()
    ws = AsyncMock()
    captured: list[str] = []

    async def fake_run_once(cell_arg, text, websocket, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(text)

    monkeypatch.setattr(routes, "_run_once_safely", fake_run_once)

    await routes._dispatch_frame(
        _frame(),
        cell,
        ws,
        tm=object(),
        thread_id="thread-aaaaaaaaaaaa",
        conn_id="conn-1",
    )
    await asyncio.sleep(0)

    assert len(captured) == 1
    assert "request_id: call-1" in captured[0]
    assert ws.send_json.await_count == 0


async def test_dispatch_choice_submit_rejects_active_run() -> None:
    cell = _Cell()
    cell.current_run_task = asyncio.create_task(asyncio.sleep(10))
    ws = AsyncMock()
    ws.send_json = AsyncMock(return_value=None)

    try:
        await routes._dispatch_frame(
            _frame(),
            cell,
            ws,
            tm=object(),
            thread_id="thread-aaaaaaaaaaaa",
            conn_id="conn-1",
        )
    finally:
        cell.current_run_task.cancel()

    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "error"
    assert "当前任务仍在运行" in sent["message"]


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
