"""从 session history 裁剪 child reviewer 证据窗口。"""

from __future__ import annotations

from collections.abc import Sequence

from core.message import Message
from evolution.models import TranscriptMessage, TranscriptWindow


def count_user_turns(history: Sequence[Message]) -> int:
    return sum(1 for message in history if message.role == "user")


def _summarize_assistant(message: Message) -> tuple[str, int]:
    text = (message.content or "").strip()
    tool_call_count = len(message.tool_calls or ())
    if tool_call_count:
        tool_names = ", ".join(call.tool_name for call in message.tool_calls or ())
        summary = f"{text}\n[tool_calls] {tool_names}".strip()
        return summary, tool_call_count
    return text, 0


def _summarize_tool(message: Message) -> str:
    prefix = message.name or "tool"
    content = (message.content or "").strip()
    if not content:
        return prefix
    return f"{prefix}: {content}"


def build_transcript_window(
    *,
    session_id: str,
    run_id: str,
    history: Sequence[Message],
    final_message: Message | None,
    max_messages: int,
) -> TranscriptWindow:
    user_turn_count = count_user_turns(history)
    current_turn = 0
    transcript: list[TranscriptMessage] = []
    start_index = max(len(history) - max_messages, 0) if max_messages > 0 else 0
    tool_call_count = 0

    for index, message in enumerate(history):
        if message.role == "user":
            current_turn += 1
        if index < start_index:
            continue
        turn = current_turn if current_turn > 0 else 0
        if message.role == "assistant":
            content, tool_calls = _summarize_assistant(message)
            tool_call_count += tool_calls
            transcript.append(
                TranscriptMessage(
                    turn=turn,
                    role="assistant",
                    content=content or "[empty assistant message]",
                )
            )
            continue
        if message.role == "tool":
            transcript.append(
                TranscriptMessage(
                    turn=turn,
                    role="tool",
                    content=_summarize_tool(message),
                    tool_name=message.name,
                )
            )
            continue
        transcript.append(
            TranscriptMessage(
                turn=turn,
                role=message.role,
                content=(message.content or "").strip() or f"[empty {message.role} message]",
            )
        )

    included_turns = tuple(
        dict.fromkeys(message.turn for message in transcript if message.turn > 0).keys()
    )
    summary = (
        f"{len(transcript)} messages across {len(included_turns)} turns; "
        f"{tool_call_count} tool calls in window."
    )
    final_text = None
    if final_message is not None and isinstance(final_message.content, str):
        final_text = final_message.content

    return TranscriptWindow(
        session_id=session_id,
        run_id=run_id,
        user_turn_count=user_turn_count,
        included_turns=included_turns,
        messages=tuple(transcript),
        final_message=final_text,
        tool_call_count=tool_call_count,
        summary=summary,
    )
