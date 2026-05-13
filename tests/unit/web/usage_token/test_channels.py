"""测试 ``_channel_anthropic`` / ``_channel_openai`` 内部 channel 实现。

覆盖：parse / merge / context_usage / to_extras_dict
"""

from __future__ import annotations

from tests.unit.web.usage_token._fixtures import (
    _AnthropicTokenUsage,
    _OpenAITokenUsage,
    anthropic_raw,
    openai_raw,
)
from web.usage_token import _channel_anthropic as ant
from web.usage_token import _channel_openai as oai

# =============================================================================
# Anthropic channel
# =============================================================================


class TestAnthropicChannel:
    def test_parse_with_all_fields(self) -> None:
        usage = ant.parse_raw_to_usage(
            anthropic_raw(
                input_tokens=100,
                cache_read_input_tokens=80,
                cache_creation_input_tokens=10,
                output_tokens=20,
            )
        )
        assert usage.channel == "anthropic"
        assert usage.input_tokens == 100
        assert usage.cache_read_input_tokens == 80
        assert usage.cache_creation_input_tokens == 10
        assert usage.output_tokens == 20

    def test_parse_with_missing_fields_defaults_to_zero(self) -> None:
        usage = ant.parse_raw_to_usage({})
        assert usage.input_tokens == 0
        assert usage.cache_read_input_tokens == 0
        assert usage.cache_creation_input_tokens == 0
        assert usage.output_tokens == 0

    def test_parse_with_none_values_defaults_to_zero(self) -> None:
        usage = ant.parse_raw_to_usage(
            {
                "input_tokens": None,
                "cache_read_input_tokens": None,
                "cache_creation_input_tokens": None,
                "output_tokens": None,
            }
        )
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0

    def test_parse_with_negative_values_clamps_to_zero(self) -> None:
        usage = ant.parse_raw_to_usage({"input_tokens": -5, "output_tokens": -10})
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0

    def test_parse_with_string_int_values(self) -> None:
        # SDK 偶尔可能给字符串数字（JSON 解析 quirk）
        usage = ant.parse_raw_to_usage({"input_tokens": "100", "output_tokens": "20"})
        assert usage.input_tokens == 100
        assert usage.output_tokens == 20

    def test_merge_cumulative(self) -> None:
        prev = _AnthropicTokenUsage(input_tokens=100, cache_read_input_tokens=80, output_tokens=20)
        delta = _AnthropicTokenUsage(input_tokens=50, cache_read_input_tokens=30, output_tokens=10)
        merged = ant.merge_cumulative(prev, delta)
        assert merged.input_tokens == 150
        assert merged.cache_read_input_tokens == 110
        assert merged.output_tokens == 30
        # frozen：原 prev / delta 不被修改
        assert prev.input_tokens == 100

    def test_context_usage_sums_input_and_cache(self) -> None:
        usage = _AnthropicTokenUsage(
            input_tokens=10,
            cache_read_input_tokens=80,
            cache_creation_input_tokens=5,
            output_tokens=20,
        )
        # 10 + 80 + 5（output 不算）
        assert ant.context_usage(usage) == 95

    def test_to_extras_dict_keys(self) -> None:
        usage = _AnthropicTokenUsage(cache_read_input_tokens=80, cache_creation_input_tokens=5)
        extras = ant.to_extras_dict(usage)
        assert extras == {
            "cache_read_input_tokens": 80,
            "cache_creation_input_tokens": 5,
        }


# =============================================================================
# OpenAI channel
# =============================================================================


class TestOpenAIChannel:
    def test_parse_with_all_fields(self) -> None:
        usage = oai.parse_raw_to_usage(
            openai_raw(
                input_tokens=100,
                cached_input_tokens=80,
                output_tokens=20,
                reasoning_output_tokens=15,
            )
        )
        assert usage.channel == "openai"
        assert usage.input_tokens == 100
        assert usage.cached_input_tokens == 80
        assert usage.output_tokens == 20
        assert usage.reasoning_output_tokens == 15

    def test_parse_with_missing_fields_defaults_to_zero(self) -> None:
        usage = oai.parse_raw_to_usage({})
        assert usage.input_tokens == 0
        assert usage.cached_input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.reasoning_output_tokens == 0

    def test_merge_cumulative(self) -> None:
        prev = _OpenAITokenUsage(input_tokens=100, cached_input_tokens=80, output_tokens=20)
        delta = _OpenAITokenUsage(input_tokens=50, cached_input_tokens=30, output_tokens=10)
        merged = oai.merge_cumulative(prev, delta)
        assert merged.input_tokens == 150
        assert merged.cached_input_tokens == 110
        assert merged.output_tokens == 30

    def test_context_usage_equals_input_only(self) -> None:
        """OpenAI 的 context_usage = input_tokens（已含 cache，不再加）"""
        usage = _OpenAITokenUsage(
            input_tokens=100,
            cached_input_tokens=80,  # 注意：是 input 子集
            output_tokens=20,
            reasoning_output_tokens=10,
        )
        # context_usage 只取 input_tokens，不重复加 cached
        assert oai.context_usage(usage) == 100

    def test_to_extras_dict_keys(self) -> None:
        usage = _OpenAITokenUsage(cached_input_tokens=80, reasoning_output_tokens=15)
        extras = oai.to_extras_dict(usage)
        assert extras == {
            "cached_input_tokens": 80,
            "reasoning_output_tokens": 15,
        }
