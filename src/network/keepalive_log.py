"""Claude keepalive 本地日志。

写入位置固定为 ``<kongming_home>/logs/claude-keepalive.jsonl``，一行一个 JSON
事件，便于用 ``tail -f`` 直接观察 Claude WS 保活链路。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOG_FILENAME = "claude-keepalive.jsonl"


def append_keepalive_log(home: Path, *, event: str, **fields: Any) -> Path:
    """向 ``<home>/logs/claude-keepalive.jsonl`` 追加一条 JSONL 记录。"""
    path = home / "logs" / _LOG_FILENAME
    payload: dict[str, Any] = {
        "ts_ms": int(time.time() * 1000),
        "pid": os.getpid(),
        "event": event,
    }
    for key, value in fields.items():
        if value is not None:
            payload[key] = value
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        logger.warning("claude keepalive log append failed: path=%s", path, exc_info=True)
    return path


__all__ = ["append_keepalive_log"]
