"""Claude 通道派生器：从 SDK jsonl 取最后一条 assistant.message.usage → ClaudeUsage。

⚠️ **架构边界**：本模块是 ``web.usage_token_v2`` 包私有，外部禁止 import
（``.importlinter`` Contract 9 强制）；只能通过 ``UsageTokenManager`` 间接消费。

设计要点：

- **取最后一条**带 usage 的 assistant entry，**不累加**（Anthropic 的
  ``input_tokens`` 字段是"纯新增"语义，最后一条 input + cache_read + cache_creation
  = 当前 context 占用，跟 v2 设计哲学对齐）
- 流式按行读，避免大文件 OOM
- 单行 JSON 损坏 → warning + skip，**不抛**
- ``type != "assistant"`` / ``message.usage`` 缺失 / 空 dict → skip
- 文件不存在 / 不可读 / 没有任何带 usage 的 assistant entry → 返回 None
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from web.usage_token_v2._model_context_table import lookup_context_window
from web.usage_token_v2._models import ClaudeCacheCreation, ClaudeUsage

logger = logging.getLogger(__name__)

__all__ = ["derive_from_jsonl"]


def derive_from_jsonl(jsonl_path: Path) -> ClaudeUsage | None:
    """扫 Claude SDK jsonl 派生 ClaudeUsage。

    Args:
        jsonl_path: ``~/.claude/projects/<encoded-cwd>/<claude_thread_id>.jsonl``。
            由调用方（manager 注入的 locator）拼好；本函数不感知路径规则。

    Returns:
        ``ClaudeUsage``，或 ``None``（文件不存在 / 不可读 / 无带 usage 的 assistant）
    """
    if not jsonl_path.is_file():
        return None

    last_usage: dict[str, Any] | None = None
    last_model: str = ""

    try:
        fh = jsonl_path.open("r", encoding="utf-8")
    except OSError as exc:
        logger.warning("derive_from_jsonl: cannot open %s: %s", jsonl_path, exc)
        return None

    try:
        with fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (ValueError, json.JSONDecodeError) as exc:
                    logger.warning(
                        "derive_from_jsonl: skip malformed line in %s: %s",
                        jsonl_path,
                        exc,
                    )
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict) or not usage:
                    continue
                last_usage = usage
                model = message.get("model")
                if isinstance(model, str) and model:
                    last_model = model
    except OSError as exc:
        logger.warning("derive_from_jsonl: read error on %s: %s", jsonl_path, exc)
        return None

    if last_usage is None:
        return None

    return _map_to_claude_usage(last_usage, last_model)


def _map_to_claude_usage(raw: dict[str, Any], model: str) -> ClaudeUsage:
    """SDK 原生 usage dict → ClaudeUsage DTO（1:1 映射 + cache_creation 嵌套细分）。"""
    cc_raw = raw.get("cache_creation") or {}
    if not isinstance(cc_raw, dict):
        cc_raw = {}

    input_tokens = _safe_int(raw.get("input_tokens"))
    output_tokens = _safe_int(raw.get("output_tokens"))
    cache_read = _safe_int(raw.get("cache_read_input_tokens"))
    cache_creation_total = _safe_int(raw.get("cache_creation_input_tokens"))

    return ClaudeUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation_total,
        cache_creation=ClaudeCacheCreation(
            ephemeral_1h_input_tokens=_safe_int(cc_raw.get("ephemeral_1h_input_tokens")),
            ephemeral_5m_input_tokens=_safe_int(cc_raw.get("ephemeral_5m_input_tokens")),
        ),
        context_usage=input_tokens + cache_read + cache_creation_total,
        model=model,
        context_window=lookup_context_window(model),
    )


def _safe_int(value: Any) -> int:
    """把任意 value 转 int；None / 非数字 / 负数 → 0。"""
    if value is None:
        return 0
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, n)
