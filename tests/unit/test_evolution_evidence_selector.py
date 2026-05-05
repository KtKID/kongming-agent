"""unit：evidence selector。"""

from __future__ import annotations

import pytest

from core.message import Message, ToolCall
from evolution.evidence_selector import build_transcript_window


@pytest.mark.unit
def test_build_transcript_window_counts_turns_and_tool_calls() -> None:
    history = [
        Message.system("sys"),
        Message.user("turn1"),
        Message.assistant("thinking", tool_calls=[ToolCall("c1", "shell", {"cmd": "pwd"})]),
        Message.tool_result("c1", "/repo", name="shell"),
        Message.user("turn2"),
        Message.assistant("done"),
    ]

    window = build_transcript_window(
        session_id="s1",
        run_id="run-s1-2",
        history=history,
        final_message=history[-1],
        max_messages=4,
    )

    assert window.user_turn_count == 2
    assert window.included_turns == (1, 2)
    assert window.tool_call_count == 1
    assert len(window.messages) == 4
    assert window.messages[0].role == "assistant"
    assert "[tool_calls] shell" in window.messages[0].content
    assert window.final_message == "done"
