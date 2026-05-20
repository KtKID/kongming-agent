"""解析 Claude Code SDK transcript JSONL，累计 token usage。

逐行读取 transcript 文件，只累加 ``type == "assistant"`` 且含
``message.usage`` 的条目。文件不存在 / 读取失败时返回全 0，不抛异常。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

__all__ = ["TranscriptUsage", "parse_transcript_usage"]


@dataclass(frozen=True)
class TranscriptUsage:
    """累计 token 用量（frozen，可安全缓存 / 比较）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


def _positive_int(value: object) -> int:
    """非正整数一律按 0 处理。"""
    if isinstance(value, int) and value > 0:
        return value
    return 0


def parse_transcript_usage(jsonl_path: Path) -> TranscriptUsage:
    """解析 *jsonl_path* 并返回累计 :class:`TranscriptUsage`。

    - 空行 / JSON 解析失败的行 → 跳过
    - 只累加 ``type == "assistant"`` 且 ``entry["message"]["usage"]`` 存在的条目
    - 文件不存在或读取失败 → 返回全 0 的 ``TranscriptUsage``
    """
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_creation_tokens = 0

    try:
        text = jsonl_path.read_text(encoding="utf-8")
    except Exception:
        return TranscriptUsage()

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue

        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "assistant":
            continue

        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue

        input_tokens += _positive_int(usage.get("input_tokens"))
        output_tokens += _positive_int(usage.get("output_tokens"))
        cache_read_tokens += _positive_int(usage.get("cache_read_input_tokens"))
        cache_creation_tokens += _positive_int(
            usage.get("cache_creation_input_tokens"),
        )

    return TranscriptUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
    )
