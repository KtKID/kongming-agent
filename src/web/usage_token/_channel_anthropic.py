"""Anthropic 系（claude_code + generic_chat-anthropic）channel 实现。

提供 3 个纯函数：

- :func:`parse_raw_to_usage` —— 从 SDK 原生 usage dict 构造 ``_AnthropicTokenUsage``
- :func:`merge_cumulative` —— 把一轮 delta 累加到累计
- :func:`context_usage` —— 派生「当前 context 占用」

⚠️ **架构边界**：本模块是 ``web.usage_token`` 包私有，外部禁止 import
（``.importlinter`` Contract ``usage-token-encapsulation`` 强制）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from web.usage_token._models import _AnthropicTokenUsage


def parse_raw_to_usage(raw: Mapping[str, Any]) -> _AnthropicTokenUsage:
    """从 Anthropic SDK 原生 ``usage`` dict 构造 ``_AnthropicTokenUsage``。

    输入字段（任意缺失视为 0，**不抛**）：

    - ``input_tokens``：本轮纯新输入（不含 cache）
    - ``output_tokens``：本轮输出
    - ``cache_read_input_tokens``：命中 cache 输入（独立计数）
    - ``cache_creation_input_tokens``：写 cache 输入（独立计数）

    数据源：
    - Claude Code SDK transcript jsonl 的 ``message.usage``
    - Anthropic Messages API 响应的 ``usage``

    Args:
        raw: SDK 原生 usage dict（任何 Mapping 子类型都接受）

    Returns:
        新构造的 ``_AnthropicTokenUsage``（frozen）
    """
    return _AnthropicTokenUsage(
        input_tokens=_safe_int(raw.get("input_tokens")),
        output_tokens=_safe_int(raw.get("output_tokens")),
        cache_read_input_tokens=_safe_int(raw.get("cache_read_input_tokens")),
        cache_creation_input_tokens=_safe_int(raw.get("cache_creation_input_tokens")),
    )


def merge_cumulative(
    prev: _AnthropicTokenUsage,
    delta: _AnthropicTokenUsage,
) -> _AnthropicTokenUsage:
    """把一轮 ``delta`` 累加到累计 ``prev``。

    4 个字段独立相加（Anthropic 的 cache_read / cache_creation 都是独立计数）。

    Args:
        prev: 累计起点（首轮可传 ``_AnthropicTokenUsage()``）
        delta: 本轮 usage（``parse_raw_to_usage`` 产出）

    Returns:
        新构造的累计 ``_AnthropicTokenUsage``（frozen，不修改 prev）
    """
    return _AnthropicTokenUsage(
        input_tokens=prev.input_tokens + delta.input_tokens,
        output_tokens=prev.output_tokens + delta.output_tokens,
        cache_read_input_tokens=prev.cache_read_input_tokens + delta.cache_read_input_tokens,
        cache_creation_input_tokens=prev.cache_creation_input_tokens
        + delta.cache_creation_input_tokens,
    )


def context_usage(usage: _AnthropicTokenUsage) -> int:
    """派生「当前 context 占用」= ``input + cache_read + cache_creation``。

    Anthropic 的三个 input 子项相加才是发给模型的总 prompt 大小。

    Args:
        usage: 单轮或累计 ``_AnthropicTokenUsage``

    Returns:
        总 context 占用 token 数（非负）
    """
    return usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens


def to_extras_dict(usage: _AnthropicTokenUsage) -> dict[str, int]:
    """把 channel-specific 字段提取到 ``extras`` dict（构造公共 DTO 用）。

    Returns:
        ``{"cache_read_input_tokens": N, "cache_creation_input_tokens": M}``
    """
    return {
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
    }


def _safe_int(value: Any) -> int:
    """把任意 value 转 ``int``，None / 非数字一律 0。负数也归 0（不抛）。"""
    if value is None:
        return 0
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, n)


__all__ = [
    "context_usage",
    "merge_cumulative",
    "parse_raw_to_usage",
    "to_extras_dict",
]
