"""Claude Code session jsonl 写入工具。

目前只提供 :func:`append_custom_title` —— 追加 ``custom-title`` 行到 jsonl，
用于 Web UI 的 session 重命名。

设计要点：

- ``O_APPEND`` 原子追加，并发安全（POSIX 语义：单次 ``write`` ≤ ``PIPE_BUF``
  不会被撕裂）。
- 纯标准库，不引入第三方依赖。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

__all__ = ["append_archived", "append_custom_title"]


def append_archived(jsonl_path: Path, session_id: str) -> None:
    """``O_APPEND`` 追加 ``archived`` 标记到 jsonl。原子写入，并发安全。"""
    line = (
        json.dumps(
            {
                "type": "archived",
                "archived": True,
                "sessionId": session_id,
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    fd = os.open(str(jsonl_path), os.O_WRONLY | os.O_APPEND)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def append_custom_title(jsonl_path: Path, session_id: str, title: str) -> None:
    """``O_APPEND`` 追加 ``custom-title`` 行到 jsonl。原子写入，并发安全。"""
    line = (
        json.dumps(
            {
                "type": "custom-title",
                "customTitle": title,
                "sessionId": session_id,
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    fd = os.open(str(jsonl_path), os.O_WRONLY | os.O_APPEND)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
