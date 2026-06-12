"""Network exception/event audit log.

Writes JSONL records to ``<kongming_home>/logs/network/network-events.jsonl`` so runtime
network failures are visible even when callers deliberately swallow them.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from infrastructure.config.paths import get_kongming_home

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_LOG_FILE = "network-events.jsonl"


def _log_path() -> Path:
    return get_kongming_home().joinpath("logs", "network", _LOG_FILE)


def log_network_event(
    component: str,
    action: str,
    *,
    level: str = "INFO",
    message: str | None = None,
    **fields: Any,
) -> Path | None:
    payload: dict[str, Any] = {
        "ts_ms": int(time.time() * 1000),
        "pid": os.getpid(),
        "level": level.upper(),
        "component": component,
        "action": action,
    }
    if message:
        payload["message"] = message
    for key, value in fields.items():
        if value is not None:
            payload[key] = value

    path = _log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False)
        with _LOCK, path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        logger.warning(
            "network log append failed: path=%s component=%s action=%s",
            path,
            component,
            action,
            exc_info=True,
        )
        return None
    return path


def log_network_exception(
    component: str,
    action: str,
    exc: BaseException,
    *,
    level: str = "WARNING",
    message: str | None = None,
    **fields: Any,
) -> Path | None:
    return log_network_event(
        component,
        action,
        level=level,
        message=message or str(exc),
        error_type=type(exc).__name__,
        error=str(exc),
        traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        **fields,
    )


__all__ = ["log_network_event", "log_network_exception"]
