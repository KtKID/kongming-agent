"""generic_chat 通道派生器：从 FileSession messages.jsonl 派生。

⚠️ **架构边界**：本模块是 ``web.usage_token_v2`` 包私有，外部禁止 import
（``.importlinter`` Contract 9 强制）；只能通过 ``UsageTokenManager`` 间接消费。

设计要点（D-1/D-2/D-3 决策已敲死，详见任务 README）：

- **只支持 FileSession** backend。InMemorySession 不持久化 usage；
  SQLiteSession 项目内已不维护。memory/sqlite backend → locator 返回 None →
  本派生器**不会被调用**。
- 取**最后一条**带 ``usage`` 字段的 message line，**不累加**（跟 Claude 通道对称）
- 按调用方传入的 ``provider``（``"anthropic"`` / ``"openai_compatible"``）路由
  到对应 DTO 映射：
    - ``"anthropic"`` → ``GenericChatAnthropicUsage``
    - ``"openai_compatible"`` → ``GenericChatOpenAIUsage``
- FileSession messages.jsonl 格式：每行 JSON 含可选 ``usage`` dict（跟 SDK 原生
  usage dict 同形）；详见 ``sessions/file_session.py::FileSession.append``
- 文件不存在 / 无含 usage 的行 → 返回 None
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal

from web.usage_token_v2._model_context_table import lookup_context_window
from web.usage_token_v2._models import (
    ClaudeCacheCreation,
    CodexTokenBreakdown,
    GenericChatAnthropicUsage,
    GenericChatOpenAIUsage,
)

logger = logging.getLogger(__name__)

__all__ = ["derive_from_session"]


ProviderKind = Literal["anthropic", "openai_compatible"]


def derive_from_session(
    session_jsonl_path: Path,
    provider: ProviderKind,
) -> GenericChatAnthropicUsage | GenericChatOpenAIUsage | None:
    """扫 FileSession messages.jsonl 派生 generic_chat 通道 usage。

    Args:
        session_jsonl_path: ``<kongming_home>/sessions/<sid>/messages.jsonl``
        provider: 底层 LLMProvider 厂商（``"anthropic"`` / ``"openai_compatible"``）

    Returns:
        ``GenericChatAnthropicUsage`` 或 ``GenericChatOpenAIUsage`` 或 ``None``
    """
    if provider not in ("anthropic", "openai_compatible"):
        logger.warning("derive_from_session: unknown provider %r; returning None", provider)
        return None

    if not session_jsonl_path.is_file():
        return None

    last_usage: dict[str, Any] | None = None
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
                last_usage = usage
                model = entry.get("model_name")
                if isinstance(model, str) and model:
                    last_model = model
    except OSError as exc:
        logger.warning("derive_from_session: read error on %s: %s", session_jsonl_path, exc)
        return None

    if last_usage is None:
        return None

    if provider == "anthropic":
        return _map_anthropic(last_usage, last_model)
    return _map_openai(last_usage, last_model)


def _map_anthropic(raw: dict[str, Any], model: str) -> GenericChatAnthropicUsage:
    """Anthropic 系 usage → GenericChatAnthropicUsage（字段同 ClaudeUsage 平行）。"""
    cc_raw = raw.get("cache_creation") or {}
    if not isinstance(cc_raw, dict):
        cc_raw = {}

    input_tokens = _safe_int(raw.get("input_tokens"))
    output_tokens = _safe_int(raw.get("output_tokens"))
    cache_read = _safe_int(raw.get("cache_read_input_tokens"))
    cache_creation_total = _safe_int(raw.get("cache_creation_input_tokens"))

    return GenericChatAnthropicUsage(
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


def _map_openai(raw: dict[str, Any], model: str) -> GenericChatOpenAIUsage:
    """OpenAI 系 usage → GenericChatOpenAIUsage（只填 last，不累加 total）。"""
    return GenericChatOpenAIUsage(
        last=CodexTokenBreakdown(
            input_tokens=_safe_int(raw.get("input_tokens")),
            cached_input_tokens=_safe_int(raw.get("cached_input_tokens")),
            output_tokens=_safe_int(raw.get("output_tokens")),
            reasoning_output_tokens=_safe_int(raw.get("reasoning_output_tokens")),
            total_tokens=_safe_int(raw.get("total_tokens")),
        ),
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
