"""generic_chat 通道派生器：从 FileSession session JSONL 派生。

⚠️ **架构边界**：本模块是 ``web.usage.usage_token_v2`` 包私有，外部禁止 import
（``.importlinter`` Contract 9 强制）；只能通过 ``UsageTokenManager`` 间接消费。

设计要点：

- **只支持 FileSession** backend。InMemorySession 不持久化 usage；
  SQLiteSession 项目内已不维护。memory/sqlite backend → locator 返回 None →
  本派生器**不会被调用**。
- 取**最后一条**带 ``usage`` 字段的 message line，**不累加**（跟 Claude 通道对称）
- family 只读取持久化的 canonical snapshot，不使用 preset/model/provider 名猜测。
- FileSession session JSONL 格式：每行 JSON 含可选 ``usage`` dict（跟 SDK 原生
  canonical snapshot）；详见 ``sessions/file_session.py::FileSession.append``
- 文件不存在 / 无含 usage 的行 → 返回 None
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.contracts import ProviderUsageFamily, ProviderUsageSnapshot
from hosts.web.usage.usage_token_v2._model_context_table import lookup_context_window
from hosts.web.usage.usage_token_v2._models import (
    GenericChatAnthropicUsage,
    GenericChatCacheCreation,
    GenericChatOpenAIUsage,
    GenericChatTokenBreakdown,
)

logger = logging.getLogger(__name__)

__all__ = ["derive_from_session"]


def derive_from_session(
    session_jsonl_path: Path,
) -> GenericChatAnthropicUsage | GenericChatOpenAIUsage | None:
    """扫 FileSession session JSONL 派生 generic_chat 通道 usage。

    Args:
        session_jsonl_path: FileSession ``manifest.json`` 的 ``format`` 指向的 JSONL 文件。
    Returns:
        ``GenericChatAnthropicUsage`` 或 ``GenericChatOpenAIUsage`` 或 ``None``
    """
    if not session_jsonl_path.is_file():
        return None

    last_usage: ProviderUsageSnapshot | None = None
    last_model: str = ""

    try:
        fh = session_jsonl_path.open("r", encoding="utf-8")
    except OSError as exc:
        logger.warning("derive_from_session: cannot open %s: %s", session_jsonl_path, exc)
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
                        "derive_from_session: skip malformed line in %s: %s",
                        session_jsonl_path,
                        exc,
                    )
                    continue
                if not isinstance(entry, dict):
                    continue
                usage = entry.get("usage")
                if not isinstance(usage, dict) or not usage:
                    continue
                try:
                    last_usage = ProviderUsageSnapshot.from_payload(usage)
                except ValueError:
                    logger.warning(
                        "derive_from_session: skip invalid canonical usage in %s",
                        session_jsonl_path,
                    )
                    continue
                model = entry.get("model_name")
                if isinstance(model, str) and model:
                    last_model = model
    except OSError as exc:
        logger.warning("derive_from_session: read error on %s: %s", session_jsonl_path, exc)
        return None

    if last_usage is None:
        return None

    if last_usage.family is ProviderUsageFamily.ANTHROPIC_MESSAGES:
        return _map_anthropic(last_usage, last_model)
    return _map_openai(last_usage, last_model)


def _map_anthropic(
    snapshot: ProviderUsageSnapshot,
    model: str,
) -> GenericChatAnthropicUsage:
    """Anthropic snapshot 投影为 generic DTO，未知指标保持 None。"""
    raw = snapshot.raw_usage
    cc_raw = raw.get("cache_creation") or {}
    if not isinstance(cc_raw, dict):
        cc_raw = {}

    return GenericChatAnthropicUsage(
        input_tokens=snapshot.input_uncached_tokens.value,
        output_tokens=snapshot.output_total_tokens.value,
        cache_read_input_tokens=snapshot.cache_read_tokens.value,
        cache_creation_input_tokens=snapshot.cache_write_tokens.value,
        cache_creation=GenericChatCacheCreation(
            ephemeral_1h_input_tokens=_optional_token(cc_raw.get("ephemeral_1h_input_tokens")),
            ephemeral_5m_input_tokens=_optional_token(cc_raw.get("ephemeral_5m_input_tokens")),
        ),
        context_usage=snapshot.input_total_tokens.value,
        model=model,
        context_window=lookup_context_window(model),
    )


def _map_openai(
    snapshot: ProviderUsageSnapshot,
    model: str,
) -> GenericChatOpenAIUsage:
    """OpenAI snapshot 投影为 generic DTO，未知指标保持 None。"""
    return GenericChatOpenAIUsage(
        last=GenericChatTokenBreakdown(
            input_tokens=snapshot.input_total_tokens.value,
            cached_input_tokens=snapshot.cache_read_tokens.value,
            output_tokens=snapshot.output_total_tokens.value,
            reasoning_output_tokens=snapshot.reasoning_tokens.value,
            total_tokens=snapshot.total_tokens.value,
        ),
        model=model,
        context_window=lookup_context_window(model),
    )


def _optional_token(value: Any) -> int | None:
    """读取 raw 中的可选 token，输入为开放值，输出为非负整数或 None。"""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
