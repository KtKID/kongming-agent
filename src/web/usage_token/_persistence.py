"""TokenUsage ↔ 落盘 dict 互转 + ThreadMetadataIO Protocol。

设计要点：

- ``ThreadMetadataIO`` 是 manager 对落盘的**抽象依赖**（Protocol）
  - task#1 用 ``InMemoryThreadMetadataIO`` 测试 dummy 满足
  - task#2 把真实 ``web.thread_metadata`` 包装成 adapter 满足
- ``to_persisted_dict`` / ``from_persisted_dict`` 是 ``TokenUsage`` 跟透明 dict
  之间的桥梁——落盘格式是 dict（外部模块看到），manager 内部用 TokenUsage 类型

⚠️ **架构边界**：``ThreadMetadataIO`` 是公共 Protocol（外部 adapter 可以实现）；
其他符号（``to_persisted_dict`` / ``from_persisted_dict``）是私有，外部禁止 import。
"""

from __future__ import annotations

from typing import Any, Protocol, cast, runtime_checkable

from pydantic import TypeAdapter

from web.usage_token._models import (
    TokenUsage,
    _AnthropicTokenUsage,
    _OpenAITokenUsage,
)

# =============================================================================
# 公共：ThreadMetadataIO Protocol
# =============================================================================


@runtime_checkable
class ThreadMetadataIO(Protocol):
    """Manager 对 ThreadMetadata 落盘的抽象依赖。

    实现要求：

    - ``read_cumulative_usage(thread_id)`` 返回 ``cumulative_usage`` dict 或
      ``None``（thread 不存在 / 首轮前）
    - ``write_cumulative_usage(thread_id, usage_dict)`` 原子写入

    task#1 在测试目录提供 ``InMemoryThreadMetadataIO`` dummy；
    task#2 提供真实 adapter 包装 ``web.thread_metadata.read_thread_metadata`` /
    ``write_thread_metadata``。
    """

    async def read_cumulative_usage(self, thread_id: str) -> dict[str, Any] | None:
        """读取 thread 的 ``cumulative_usage`` dict 或 ``None``。"""
        ...

    async def write_cumulative_usage(self, thread_id: str, usage_dict: dict[str, Any]) -> None:
        """把 ``cumulative_usage`` dict 原子写盘。"""
        ...


# =============================================================================
# 私有：TokenUsage ↔ dict 互转
# =============================================================================


_TOKEN_USAGE_ADAPTER: TypeAdapter[TokenUsage] = TypeAdapter(TokenUsage)
"""Pydantic TypeAdapter for discriminated union。module 级缓存，避免重复构造。"""


def to_persisted_dict(usage: TokenUsage) -> dict[str, Any]:
    """``TokenUsage`` → 落盘 dict。

    格式（Anthropic）：
        {"channel": "anthropic", "input_tokens": N,
         "cache_read_input_tokens": N, "cache_creation_input_tokens": N,
         "output_tokens": N}

    格式（OpenAI）：
        {"channel": "openai", "input_tokens": N, "cached_input_tokens": N,
         "output_tokens": N, "reasoning_output_tokens": N}

    Args:
        usage: ``TokenUsage`` 实例（_AnthropicTokenUsage 或 _OpenAITokenUsage）

    Returns:
        可 ``json.dumps`` 的 dict（``frozen`` model 转完是普通 dict）
    """
    # mode="json" 让 dict 里全是 JSON-safe 原生类型（int / str），避免 Pydantic
    # 子模型嵌套结构（本场景没有嵌套子模型但保险起见）。
    # ``dump_python`` 在某些 typing stub 下返回 Any，cast 一下显式化为 dict。
    return cast(dict[str, Any], _TOKEN_USAGE_ADAPTER.dump_python(usage, mode="json"))


def from_persisted_dict(raw: dict[str, Any] | None) -> TokenUsage | None:
    """落盘 dict → ``TokenUsage`` 实例。

    校验规则：

    - ``raw is None`` → 返回 ``None``（thread 首轮前）
    - 缺 ``channel`` 字段 / channel 值不在 ``("anthropic", "openai")`` → 返回 ``None``
      并不抛（损坏数据视为未初始化；防御性）
    - 字段类型错误 → Pydantic 报错（让调用方决定 retry/log）

    Args:
        raw: 落盘 dict 或 None

    Returns:
        ``TokenUsage`` 或 None（损坏 / 首轮前）
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None
    channel = raw.get("channel")
    if channel not in ("anthropic", "openai"):
        return None
    return _TOKEN_USAGE_ADAPTER.validate_python(raw)


def empty_usage_for_channel(channel: str) -> TokenUsage:
    """构造一个全零的 ``TokenUsage``（manager 首轮用作累加起点）。

    Args:
        channel: ``"anthropic"`` 或 ``"openai"``

    Raises:
        ValueError: channel 不在已知值
    """
    if channel == "anthropic":
        return _AnthropicTokenUsage()
    if channel == "openai":
        return _OpenAITokenUsage()
    raise ValueError(f"unknown channel: {channel!r}")


__all__ = [
    "ThreadMetadataIO",
    "empty_usage_for_channel",
    "from_persisted_dict",
    "to_persisted_dict",
]
