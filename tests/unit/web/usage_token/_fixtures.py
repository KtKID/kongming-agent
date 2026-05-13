"""测试 helpers。

集中提供：
- ``anthropic_raw`` / ``openai_raw`` factory：构造 SDK raw payload dict
- ``InMemoryThreadMetadataIO``：满足 ``ThreadMetadataIO`` Protocol 的内存实现
- 私有 model 重导出：测试代码内允许 import 私有 model（pytest 在 tests/ 目录下，
  ``.importlinter`` Contract 9 不限制 ``tests`` source_modules）
"""

from __future__ import annotations

from typing import Any

# 测试代码允许 import 私有 model（tests/ 不在 Contract 9 的 source_modules 里）
from web.usage_token._models import (
    _AnthropicTokenUsage,
    _OpenAITokenUsage,
)
from web.usage_token._persistence import ThreadMetadataIO

__all__ = [
    "InMemoryThreadMetadataIO",
    "_AnthropicTokenUsage",
    "_OpenAITokenUsage",
    "anthropic_raw",
    "openai_raw",
]


def anthropic_raw(
    *,
    input_tokens: int | None = None,
    cache_read_input_tokens: int | None = None,
    cache_creation_input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> dict[str, Any]:
    """构造 Anthropic SDK 风格的原生 usage dict（仅设置非 None 字段）。"""
    raw: dict[str, Any] = {}
    if input_tokens is not None:
        raw["input_tokens"] = input_tokens
    if cache_read_input_tokens is not None:
        raw["cache_read_input_tokens"] = cache_read_input_tokens
    if cache_creation_input_tokens is not None:
        raw["cache_creation_input_tokens"] = cache_creation_input_tokens
    if output_tokens is not None:
        raw["output_tokens"] = output_tokens
    return raw


def openai_raw(
    *,
    input_tokens: int | None = None,
    cached_input_tokens: int | None = None,
    output_tokens: int | None = None,
    reasoning_output_tokens: int | None = None,
) -> dict[str, Any]:
    """构造 OpenAI / Codex SDK 风格的原生 usage dict。"""
    raw: dict[str, Any] = {}
    if input_tokens is not None:
        raw["input_tokens"] = input_tokens
    if cached_input_tokens is not None:
        raw["cached_input_tokens"] = cached_input_tokens
    if output_tokens is not None:
        raw["output_tokens"] = output_tokens
    if reasoning_output_tokens is not None:
        raw["reasoning_output_tokens"] = reasoning_output_tokens
    return raw


class InMemoryThreadMetadataIO:
    """满足 ``ThreadMetadataIO`` Protocol 的内存实现，测试用。

    使用方式：

    .. code-block:: python

        io = InMemoryThreadMetadataIO()
        manager = UsageTokenManager(home=tmp_path, thread_metadata_io=io)
        await manager.record_run_usage(...)
        # 验证：io.store["thread-xxx"] == {...}
    """

    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}

    async def read_cumulative_usage(self, thread_id: str) -> dict[str, Any] | None:
        return self.store.get(thread_id)

    async def write_cumulative_usage(self, thread_id: str, usage_dict: dict[str, Any]) -> None:
        self.store[thread_id] = dict(usage_dict)


# Protocol 兼容性自检（typing.runtime_checkable）—— 测试目录内一次性
assert isinstance(InMemoryThreadMetadataIO(), ThreadMetadataIO)
