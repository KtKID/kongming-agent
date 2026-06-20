"""Choice WS 协议合同测试。"""

from __future__ import annotations

from hosts.web.protocol.ws_frames import (
    ChoiceRequestFrame,
    ChoiceSubmitFrame,
    WSFrameC2SAdapter,
    WSFrameS2CAdapter,
)


def test_choice_request_frame_round_trip() -> None:
    frame = ChoiceRequestFrame(
        timestamp_ms=1_700_000_000_000,
        request_id="call-1",
        title="选择方案",
        description="请选择下一步实现方案。",
        turn=2,
        run_id="run-1",
        questions=[
            {
                "id": "scope",
                "title": "范围",
                "description": "控制本次范围。",
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
    )

    restored = ChoiceRequestFrame.model_validate_json(frame.model_dump_json())
    dispatched = WSFrameS2CAdapter.validate_python(frame.model_dump())

    assert restored == frame
    assert isinstance(dispatched, ChoiceRequestFrame)
    assert dispatched.questions[0].options[0].value == {"scope": "minimal"}


def test_choice_submit_frame_round_trip() -> None:
    frame = ChoiceSubmitFrame(
        request_id="call-1",
        answers=[
            {
                "question_id": "scope",
                "option_id": "minimal",
                "option_label": "最小实现",
                "custom_text": None,
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

    restored = ChoiceSubmitFrame.model_validate_json(frame.model_dump_json())
    dispatched = WSFrameC2SAdapter.validate_python(frame.model_dump())

    assert restored == frame
    assert isinstance(dispatched, ChoiceSubmitFrame)
    assert dispatched.answers[1].option_id == "__custom__"
