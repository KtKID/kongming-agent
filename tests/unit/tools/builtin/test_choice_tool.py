"""ChoiceTool 单元测试。"""

from __future__ import annotations

from typing import Any

from core.contracts import Event, ToolContext
from safety.approval.default_rules import DEFAULT_ALLOW_TOOLS_SILENT
from tools import ToolRegistry, register_choice_tool
from tools.builtin.choice_tool import CUSTOM_OPTION_ID, build_choice_tool


class _Sink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


def _ctx() -> ToolContext:
    return ToolContext(run_id="run-1", session_id="thread-abc123abc123", turn=2, call_id="call-1")


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "选择实现方案",
        "description": "请选择本次实现范围。",
        "questions": [
            {
                "id": "scope",
                "title": "范围",
                "description": "控制本次改动边界。",
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
    }
    payload.update(overrides)
    return payload


def test_register_choice_tool_adds_present_choices() -> None:
    registry = ToolRegistry()

    register_choice_tool(registry)

    assert "present_choices" in registry.names()


def test_present_choices_is_silent_allowed_by_default() -> None:
    assert "present_choices" in DEFAULT_ALLOW_TOOLS_SILENT


async def test_tool_emits_choice_requested() -> None:
    sink = _Sink()
    tool = build_choice_tool(event_sinks=[sink])

    result = await tool.execute(_payload(), _ctx())

    assert result.ok is True
    assert result.data == {
        "request_id": "call-1",
        "title": "选择实现方案",
        "question_count": 1,
        "custom_option_id": CUSTOM_OPTION_ID,
    }
    assert sink.events[0].kind == "choice.requested"
    assert sink.events[0].run_id == "run-1"
    assert sink.events[0].turn == 2
    assert sink.events[0].payload["request_id"] == "call-1"
    assert sink.events[0].payload["questions"][0]["options"][0]["value"] == {"scope": "minimal"}


async def test_tool_rejects_empty_questions() -> None:
    tool = build_choice_tool()

    result = await tool.execute(_payload(questions=[]), _ctx())

    assert result.ok is False
    assert "questions must contain at least one item" in (result.error_message or "")


async def test_tool_rejects_duplicate_question_id() -> None:
    tool = build_choice_tool()
    payload = _payload()
    payload["questions"].append(payload["questions"][0])

    result = await tool.execute(payload, _ctx())

    assert result.ok is False
    assert "duplicate question id" in (result.error_message or "")


async def test_tool_rejects_duplicate_option_id() -> None:
    tool = build_choice_tool()
    payload = _payload()
    payload["questions"][0]["options"].append(payload["questions"][0]["options"][0])

    result = await tool.execute(payload, _ctx())

    assert result.ok is False
    assert "duplicate option id" in (result.error_message or "")


async def test_tool_rejects_reserved_custom_option_id() -> None:
    tool = build_choice_tool()
    payload = _payload()
    payload["questions"][0]["options"][0]["id"] = CUSTOM_OPTION_ID

    result = await tool.execute(payload, _ctx())

    assert result.ok is False
    assert "reserved option id" in (result.error_message or "")
