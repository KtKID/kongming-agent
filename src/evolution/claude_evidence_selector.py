"""从 claude jsonl 历史裁剪 child reviewer 证据窗口。"""

from __future__ import annotations

import logging
from pathlib import Path

from evolution.models import TranscriptMessage, TranscriptWindow
from hosts.web.integrations.claude_code.jsonl_history import jsonl_path_for, parse_jsonl_history

logger = logging.getLogger(__name__)

__all__ = ["build_claude_transcript_window"]


def _empty_window(
    thread_id: str,
    run_id: str,
    *,
    summary: str | None = None,
) -> TranscriptWindow:
    return TranscriptWindow(
        session_id=thread_id,
        run_id=run_id,
        user_turn_count=0,
        included_turns=(),
        messages=(),
        final_message=None,
        tool_call_count=0,
        summary=summary,
    )


def build_claude_transcript_window(
    *,
    thread_id: str,
    claude_thread_id: str,
    cwd: str,
    run_id: str,
    max_messages: int,
    claude_home: Path | None = None,
) -> TranscriptWindow:
    """读 claude jsonl 历史，拍平成 evolution TranscriptWindow。

    纯函数，无 IO 副作用（除复用 jsonl_history 的只读）。
    异常路径返回空 messages 的 TranscriptWindow + log warning，不抛。
    """
    try:
        path = jsonl_path_for(cwd, claude_thread_id, claude_home)

        if not path.exists():
            logger.warning("jsonl not found: %s", path)
            return _empty_window(thread_id, run_id, summary="jsonl not found")

        entries = parse_jsonl_history(path, claude_thread_id)

        # 只保留纯文本消息；历史 parser v0.2 后统一使用 frame_type 字段。
        text_entries = [e for e in entries if (e.get("frame_type") or e.get("kind")) == "text"]

        if max_messages <= 0 or not text_entries:
            return _empty_window(thread_id, run_id, summary="empty jsonl")

        text_entries = text_entries[-max_messages:]

        transcript: list[TranscriptMessage] = []
        current_turn = 0
        for entry in text_entries:
            role = entry.get("role", "")
            if role == "user":
                current_turn += 1
            transcript.append(
                TranscriptMessage(
                    turn=current_turn,
                    role=role,
                    content=entry.get("content", ""),
                    tool_name=None,
                )
            )

        user_turn_count = sum(1 for m in transcript if m.role == "user")
        included_turns = tuple(sorted(set(m.turn for m in transcript if m.turn > 0)))
        summary = f"{len(transcript)} claude messages across {len(included_turns)} turns"

        return TranscriptWindow(
            session_id=thread_id,
            run_id=run_id,
            user_turn_count=user_turn_count,
            included_turns=included_turns,
            messages=tuple(transcript),
            final_message=None,
            tool_call_count=0,
            summary=summary,
        )
    except Exception as exc:
        logger.warning("build_claude_transcript_window failed: %s", exc)
        return _empty_window(thread_id, run_id, summary=f"error: {exc}")
