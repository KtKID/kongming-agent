"""Generic chat channel local JSONL log.

The file lives under ``<kongming_home>/logs/generic-channel/`` and records
transport milestones without writing user message bodies.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_LOG_PATH: Path | None = None


def configure_generic_channel_log(log_dir: Path) -> None:
    """Set the generic-channel log directory.

    The function is safe to call repeatedly; tests pass isolated ``home_dir``
    values and production passes ``get_kongming_home()``.
    """

    global _LOG_PATH
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        _LOG_PATH = log_dir / "generic-channel.jsonl"
    except OSError:
        _LOG_PATH = None


def generic_channel_log_path() -> Path:
    """Return the active JSONL log path."""

    if _LOG_PATH is not None:
        return _LOG_PATH
    return Path.cwd() / ".kongming" / "logs" / "generic-channel" / "generic-channel.jsonl"


def log_generic_channel_event(
    event: str,
    *,
    level: str = "INFO",
    thread_id: str | None = None,
    conn_id: str | None = None,
    frame_type: str | None = None,
    **fields: Any,
) -> Path | None:
    """Append one generic-channel event as JSONL."""

    payload: dict[str, Any] = {
        "ts_ms": int(time.time() * 1000),
        "pid": os.getpid(),
        "level": level.upper(),
        "event": event,
    }
    if thread_id is not None:
        payload["thread_id"] = thread_id
    if conn_id is not None:
        payload["conn_id"] = conn_id
    if frame_type is not None:
        payload["frame_type"] = frame_type
    for key, value in fields.items():
        if value is not None:
            payload[key] = _json_safe(value)

    path = generic_channel_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with _LOCK, path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        return None
    return path


def log_generic_channel_exception(
    event: str,
    exc: BaseException,
    *,
    level: str = "WARNING",
    thread_id: str | None = None,
    conn_id: str | None = None,
    frame_type: str | None = None,
    **fields: Any,
) -> Path | None:
    """Append an exception event with type/message/traceback."""

    return log_generic_channel_event(
        event,
        level=level,
        thread_id=thread_id,
        conn_id=conn_id,
        frame_type=frame_type,
        error_type=type(exc).__name__,
        error=str(exc),
        traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        **fields,
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return str(value)


__all__ = [
    "configure_generic_channel_log",
    "generic_channel_log_path",
    "log_generic_channel_event",
    "log_generic_channel_exception",
]
