"""测试 ``_persistence`` —— TokenUsage ↔ dict 互转 + ThreadMetadataIO Protocol。"""

from __future__ import annotations

import pytest

from tests.unit.web.usage_token._fixtures import (
    InMemoryThreadMetadataIO,
    _AnthropicTokenUsage,
    _OpenAITokenUsage,
)
from web.usage_token._persistence import (
    ThreadMetadataIO,
    empty_usage_for_channel,
    from_persisted_dict,
    to_persisted_dict,
)


class TestToPersistedDict:
    def test_anthropic_usage_serializes_all_fields(self) -> None:
        usage = _AnthropicTokenUsage(
            input_tokens=100,
            cache_read_input_tokens=80,
            cache_creation_input_tokens=10,
            output_tokens=20,
        )
        d = to_persisted_dict(usage)
        assert d == {
            "channel": "anthropic",
            "input_tokens": 100,
            "cache_read_input_tokens": 80,
            "cache_creation_input_tokens": 10,
            "output_tokens": 20,
        }

    def test_openai_usage_serializes_all_fields(self) -> None:
        usage = _OpenAITokenUsage(
            input_tokens=100,
            cached_input_tokens=80,
            output_tokens=20,
            reasoning_output_tokens=15,
        )
        d = to_persisted_dict(usage)
        assert d == {
            "channel": "openai",
            "input_tokens": 100,
            "cached_input_tokens": 80,
            "output_tokens": 20,
            "reasoning_output_tokens": 15,
        }


class TestFromPersistedDict:
    def test_none_input_returns_none(self) -> None:
        assert from_persisted_dict(None) is None

    def test_non_dict_returns_none(self) -> None:
        # 防御性：损坏数据视为 None
        assert from_persisted_dict("not a dict") is None  # type: ignore[arg-type]
        assert from_persisted_dict(42) is None  # type: ignore[arg-type]

    def test_missing_channel_returns_none(self) -> None:
        assert from_persisted_dict({"input_tokens": 100}) is None

    def test_unknown_channel_returns_none(self) -> None:
        assert from_persisted_dict({"channel": "google", "input_tokens": 100}) is None

    def test_anthropic_roundtrip(self) -> None:
        original = _AnthropicTokenUsage(
            input_tokens=100,
            cache_read_input_tokens=80,
            cache_creation_input_tokens=10,
            output_tokens=20,
        )
        restored = from_persisted_dict(to_persisted_dict(original))
        assert restored == original

    def test_openai_roundtrip(self) -> None:
        original = _OpenAITokenUsage(
            input_tokens=100,
            cached_input_tokens=80,
            output_tokens=20,
            reasoning_output_tokens=15,
        )
        restored = from_persisted_dict(to_persisted_dict(original))
        assert restored == original


class TestEmptyUsageForChannel:
    def test_anthropic(self) -> None:
        usage = empty_usage_for_channel("anthropic")
        assert isinstance(usage, _AnthropicTokenUsage)
        assert usage.input_tokens == 0
        assert usage.cache_read_input_tokens == 0
        assert usage.cache_creation_input_tokens == 0
        assert usage.output_tokens == 0

    def test_openai(self) -> None:
        usage = empty_usage_for_channel("openai")
        assert isinstance(usage, _OpenAITokenUsage)
        assert usage.input_tokens == 0
        assert usage.cached_input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.reasoning_output_tokens == 0

    def test_unknown_channel_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown channel"):
            empty_usage_for_channel("google")


class TestInMemoryThreadMetadataIO:
    """fixture 自身的 sanity test。"""

    def test_satisfies_protocol(self) -> None:
        io = InMemoryThreadMetadataIO()
        assert isinstance(io, ThreadMetadataIO)

    @pytest.mark.asyncio
    async def test_read_returns_none_for_unknown_thread(self) -> None:
        io = InMemoryThreadMetadataIO()
        assert await io.read_cumulative_usage("thread-unknown") is None

    @pytest.mark.asyncio
    async def test_write_then_read_roundtrip(self) -> None:
        io = InMemoryThreadMetadataIO()
        payload = {"channel": "anthropic", "input_tokens": 100}
        await io.write_cumulative_usage("thread-abc", payload)
        got = await io.read_cumulative_usage("thread-abc")
        assert got == payload
