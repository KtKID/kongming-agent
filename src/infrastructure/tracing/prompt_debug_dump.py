"""Prompt debug JSON dump sink."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from core.message import Message
from infrastructure.config.paths import resolve_kongming_path

_DEFAULT_DEBUG_DIR = Path(".kongming/debug")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class PromptDebugDumpSink:
    """把每个 LLM turn 的 prompt build 快照写成 JSON。"""

    def __init__(self, output_dir: str | Path = _DEFAULT_DEBUG_DIR) -> None:
        self._output_dir = resolve_kongming_path(output_dir)

    def dump(
        self,
        *,
        session_id: str,
        run_id: str,
        turn: int,
        model: str,
        instruction_origins: Sequence[str],
        history_before_assemble: Sequence[Message],
        assembled_messages: Sequence[Message],
        metadata: Mapping[str, Any],
        added_system_prompt: str | None,
    ) -> str:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        ts_file = time.strftime("%Y%m%d-%H%M%S")
        path = self._output_dir / (
            "prompt-debug-"
            f"{_safe_filename_part(session_id or 'anonymous')}-"
            f"{_safe_filename_part(run_id)}-"
            f"turn-{turn}-"
            f"{ts_file}.json"
        )

        system_prompts = [m.content for m in assembled_messages if m.role == "system"]
        payload = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "session_id": session_id,
            "run_id": run_id,
            "turn": turn,
            "model": model,
            "instruction_origins": list(instruction_origins),
            "added_system_prompt": added_system_prompt,
            "system_prompts": system_prompts,
            "history_before_assemble": [_message_to_dict(m) for m in history_before_assemble],
            "assembled_messages": [_message_to_dict(m) for m in assembled_messages],
            "metadata": dict(metadata),
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return str(path)


def _safe_filename_part(value: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("-", value).strip("-._")
    return cleaned or "empty"


def _message_to_dict(message: Message) -> dict[str, Any]:
    data: dict[str, Any] = {
        "role": message.role,
        "content": message.content,
    }
    if message.tool_calls:
        data["tool_calls"] = [
            {
                "call_id": call.call_id,
                "tool_name": call.tool_name,
                "arguments": call.arguments,
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        data["tool_call_id"] = message.tool_call_id
    if message.name is not None:
        data["name"] = message.name
    if message.metadata:
        data["metadata"] = dict(message.metadata)
    return data


__all__ = ["PromptDebugDumpSink"]
