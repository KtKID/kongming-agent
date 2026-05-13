"""OpenAI 系（codex + generic_chat-openai）channel 实现。

提供 3 个纯函数：

- :func:`parse_raw_to_usage` —— 从 SDK 原生 usage dict 构造 ``_OpenAITokenUsage``
- :func:`merge_cumulative` —— 把一轮 delta 累加到累计
- :func:`context_usage` —— 派生「当前 context 占用」

⚠️ **架构边界**：本模块是 ``web.usage_token`` 包私有，外部禁止 import。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from web.usage_token._models import _OpenAITokenUsage


def parse_raw_to_usage(raw: Mapping[str, Any]) -> _OpenAITokenUsage:
    """从 OpenAI / Codex SDK 原生 ``usage`` dict 构造 ``_OpenAITokenUsage``。

    输入字段（任意缺失视为 0，**不抛**）：

    - ``input_tokens``：总输入（含 cached_input 子集）
    - ``output_tokens``：总输出（含 reasoning_output 子集）
    - ``cached_input_tokens``：命中 cache 输入（**input 的子集**）
    - ``reasoning_output_tokens``：推理思考输出（**output 的子集**）

    数据源：
    - Codex CLI jsonl 的 ``usage`` / ``token_count.usage``
    - OpenAI Responses API：需要从 ``prompt_tokens_details.cached_tokens`` /
      ``completion_tokens_details.reasoning_tokens`` 单独取（由 LLMProvider task#3
      在调本函数前把字段名归一化好）

    Args:
        raw: SDK 原生 usage dict

    Returns:
        新构造的 ``_OpenAITokenUsage``（frozen）
    """
    return _OpenAITokenUsage(
        input_tokens=_safe_int(raw.get("input_tokens")),
        cached_input_tokens=_safe_int(raw.get("cached_input_tokens")),
        output_tokens=_safe_int(raw.get("output_tokens")),
        reasoning_output_tokens=_safe_int(raw.get("reasoning_output_tokens")),
    )


def merge_cumulative(
    prev: _OpenAITokenUsage,
    delta: _OpenAITokenUsage,
) -> _OpenAITokenUsage:
    """把一轮 ``delta`` 累加到累计 ``prev``。

    4 个字段**都独立累加**——虽然 cached_input 是 input 的子集，但累计层面分别
    维护各自总量（OpenAI 业界惯例：分别累计后再算"命中率" = cached / input）。

    Args:
        prev: 累计起点
        delta: 本轮 usage

    Returns:
        新构造的累计 ``_OpenAITokenUsage``（frozen，不修改 prev）
    """
    return _OpenAITokenUsage(
        input_tokens=prev.input_tokens + delta.input_tokens,
        cached_input_tokens=prev.cached_input_tokens + delta.cached_input_tokens,
        output_tokens=prev.output_tokens + delta.output_tokens,
        reasoning_output_tokens=prev.reasoning_output_tokens + delta.reasoning_output_tokens,
    )


def context_usage(usage: _OpenAITokenUsage) -> int:
    """派生「当前 context 占用」= ``input_tokens``（已含 cache，不再加）。

    OpenAI 的 ``input_tokens`` 本身就是发给模型的总 prompt 大小，
    ``cached_input_tokens`` 是它的子集（计费折扣维度）。

    Args:
        usage: 单轮或累计 ``_OpenAITokenUsage``

    Returns:
        总 context 占用 token 数（非负）
    """
    return usage.input_tokens


def to_extras_dict(usage: _OpenAITokenUsage) -> dict[str, int]:
    """把 channel-specific 字段提取到 ``extras`` dict（构造公共 DTO 用）。

    Returns:
        ``{"cached_input_tokens": N, "reasoning_output_tokens": M}``
    """
    return {
        "cached_input_tokens": usage.cached_input_tokens,
        "reasoning_output_tokens": usage.reasoning_output_tokens,
    }


def _safe_int(value: Any) -> int:
    """把任意 value 转 ``int``，None / 非数字一律 0。负数也归 0。"""
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
