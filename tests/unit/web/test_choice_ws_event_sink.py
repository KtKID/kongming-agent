"""Choice WSEventSink 翻译测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock

from core.contracts import Event
from hosts.web.websocket.event_sink import WSEventSink


async def test_emit_choice_requested_sends_choice_request() -> None:
    ws = AsyncMock()
    ws.send_json = AsyncMock(return_value=None)
    sink = WSEventSink(ws)

    await sink.emit(
        Event(
            kind="choice.requested",
            run_id="run-1",
            turn=2,
            payload={
                "request_id": "call-1",
                "title": "选择方案",
                "description": "请选择下一步方案。",
                "questions": [
                    {
                        "id": "scope",
                        "title": "范围",
                        "description": None,
                        "options": [
                            {
                                "id": "minimal",
                                "label": "最小实现",
                                "description": "先打通主链路。",
                                "value": {"scope": "minimal"},
                            }
                        ],
                    }
                ],
            },
        )
    )

    sent = ws.send_json.await_args.args[0]
    assert sent["frame_type"] == "choice.request"
    assert sent["request_id"] == "call-1"
    assert sent["turn"] == 2
    assert sent["run_id"] == "run-1"
    assert sent["questions"][0]["options"][0]["value"] == {"scope": "minimal"}
