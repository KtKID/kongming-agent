"""ProviderUsageManager 单元测试。

本文件验证 provider 原始 usage 经统一门户归一化后的公共指标、原始字段保真、
流式时序、缺失/零值区分和异常证据。外部 HTTP 与 Runner 不在本测试范围。
"""

from __future__ import annotations

from core.contracts import (
    ProviderUsageCompleteness,
    ProviderUsageFamily,
    ProviderUsageMetricOrigin,
    ProviderUsageScope,
    ProviderUsageSnapshot,
    aggregate_provider_usage_snapshots,
)
from infrastructure.llm_providers.usage import ProviderUsageManager


def test_anthropic_usage_preserves_raw_and_derives_exact_totals() -> None:
    """Anthropic 三段输入与输出完整时生成精确总量并深拷贝 raw。"""
    raw = {
        "input_tokens": 194,
        "output_tokens": 119,
        "cache_read_input_tokens": 9088,
        "cache_creation_input_tokens": 0,
        "cache_creation": {"ephemeral_5m_input_tokens": 0},
        "service_tier": "standard",
        "vendor_extra": {"nested": [1, 2, 3]},
    }

    snapshot = ProviderUsageManager().normalize(
        family=ProviderUsageFamily.ANTHROPIC_MESSAGES,
        raw_usage=raw,
        provider_response_id="msg_1",
    )
    raw["vendor_extra"]["nested"].append(4)

    assert snapshot.family is ProviderUsageFamily.ANTHROPIC_MESSAGES
    assert snapshot.provider_response_id == "msg_1"
    assert snapshot.input_total_tokens.value == 9282
    assert snapshot.input_total_tokens.origin is ProviderUsageMetricOrigin.DERIVED_EXACT
    assert snapshot.input_uncached_tokens.value == 194
    assert snapshot.cache_read_tokens.value == 9088
    assert snapshot.cache_write_tokens.value == 0
    assert snapshot.output_total_tokens.value == 119
    assert snapshot.total_tokens.value == 9401
    assert snapshot.raw_usage["vendor_extra"] == {"nested": [1, 2, 3]}


def test_missing_and_reported_zero_remain_distinct_after_round_trip() -> None:
    """缺失 cache read 保持 unknown，provider 原报零保持 provider_reported。"""
    manager = ProviderUsageManager()
    missing = manager.normalize(
        family=ProviderUsageFamily.ANTHROPIC_MESSAGES,
        raw_usage={"input_tokens": 4, "output_tokens": 2},
    )
    reported_zero = manager.normalize(
        family=ProviderUsageFamily.ANTHROPIC_MESSAGES,
        raw_usage={
            "input_tokens": 4,
            "output_tokens": 2,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    )

    restored = ProviderUsageSnapshot.from_payload(missing.to_payload())

    assert restored.cache_read_tokens.value is None
    assert restored.cache_read_tokens.origin is ProviderUsageMetricOrigin.UNAVAILABLE
    assert reported_zero.cache_read_tokens.value == 0
    assert reported_zero.cache_read_tokens.origin is ProviderUsageMetricOrigin.PROVIDER_REPORTED


def test_invalid_serialized_origin_cannot_promote_value_to_reported() -> None:
    """损坏的 origin 不能让持久化数值获得 provider_reported 语义。"""
    snapshot = ProviderUsageManager().normalize(
        family=ProviderUsageFamily.OPENAI_RESPONSES,
        raw_usage={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
    )
    payload = snapshot.to_payload()
    payload["metrics"]["total_tokens"]["origin"] = "invalid-origin"

    restored = ProviderUsageSnapshot.from_payload(payload)

    assert restored.total_tokens.value is None
    assert restored.total_tokens.origin is ProviderUsageMetricOrigin.UNAVAILABLE


def test_anthropic_stream_uses_latest_present_field_without_clearing_omitted_fields() -> None:
    """后续 delta 只覆盖出现字段，省略字段保留前一累计值。"""
    stream = ProviderUsageManager().start_stream(ProviderUsageFamily.ANTHROPIC_MESSAGES)
    stream.ingest({"input_tokens": 0, "output_tokens": 0})
    stream.ingest({"cache_read_input_tokens": 9088})
    stream.ingest({"input_tokens": 194, "output_tokens": 119}, terminal=True)

    snapshot = stream.finalize(provider_response_id="msg_stream")

    assert snapshot.input_uncached_tokens.value == 194
    assert snapshot.output_total_tokens.value == 119
    assert snapshot.cache_read_tokens.value == 9088
    assert snapshot.raw_usage == {
        "input_tokens": 194,
        "output_tokens": 119,
        "cache_read_input_tokens": 9088,
    }


def test_openai_chat_maps_inclusive_and_subset_metrics() -> None:
    """Chat Completions inclusive 与 details 子集使用独立语义。"""
    snapshot = ProviderUsageManager().normalize(
        family=ProviderUsageFamily.OPENAI_CHAT_COMPLETIONS,
        raw_usage={
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "total_tokens": 1200,
            "prompt_tokens_details": {"cached_tokens": 800, "audio_tokens": 4},
            "completion_tokens_details": {
                "reasoning_tokens": 50,
                "accepted_prediction_tokens": 7,
            },
        },
    )

    assert snapshot.input_total_tokens.value == 1000
    assert snapshot.input_uncached_tokens.value is None
    assert snapshot.cache_read_tokens.value == 800
    assert snapshot.cache_write_tokens.value is None
    assert snapshot.output_total_tokens.value == 200
    assert snapshot.reasoning_tokens.value == 50
    assert snapshot.total_tokens.value == 1200


def test_openai_responses_uses_responses_field_paths() -> None:
    """Responses family 从 input/output 字段读取，不依赖 Chat 字段。"""
    snapshot = ProviderUsageManager().normalize(
        family=ProviderUsageFamily.OPENAI_RESPONSES,
        raw_usage={
            "input_tokens": 700,
            "input_tokens_details": {"cached_tokens": 512, "vendor": {"x": 1}},
            "output_tokens": 90,
            "output_tokens_details": {"reasoning_tokens": 32},
            "total_tokens": 790,
        },
    )

    assert snapshot.input_total_tokens.value == 700
    assert snapshot.input_uncached_tokens.value is None
    assert snapshot.cache_read_tokens.value == 512
    assert snapshot.output_total_tokens.value == 90
    assert snapshot.reasoning_tokens.value == 32
    assert snapshot.total_tokens.value == 790


def test_provider_total_conflict_keeps_reported_value_and_records_anomaly() -> None:
    """provider total 优先，精确求和只形成冲突证据。"""
    snapshot = ProviderUsageManager().normalize(
        family=ProviderUsageFamily.OPENAI_CHAT_COMPLETIONS,
        raw_usage={
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 999,
        },
    )

    assert snapshot.total_tokens.value == 999
    assert snapshot.total_tokens.origin is ProviderUsageMetricOrigin.PROVIDER_REPORTED
    assert [item.code.value for item in snapshot.anomalies] == ["total_mismatch"]
    assert snapshot.anomalies[0].computed_value == 120


def test_invalid_and_out_of_range_values_stay_unavailable() -> None:
    """bool、数字字符串、负数和越界子集进入 anomalies。"""
    snapshot = ProviderUsageManager().normalize(
        family=ProviderUsageFamily.OPENAI_RESPONSES,
        raw_usage={
            "input_tokens": True,
            "input_tokens_details": {"cached_tokens": "12"},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 8},
            "total_tokens": -1,
        },
    )

    assert snapshot.input_total_tokens.value is None
    assert snapshot.cache_read_tokens.value is None
    assert snapshot.reasoning_tokens.value is None
    assert snapshot.total_tokens.value is None
    assert {item.code.value for item in snapshot.anomalies} == {
        "invalid_metric",
        "negative_metric",
        "subset_exceeds_total",
    }


def test_openai_stream_without_final_usage_is_incomplete_and_unknown() -> None:
    """Chat stream 没收到最终 usage chunk时保留 incomplete/unknown。"""
    stream = ProviderUsageManager().start_stream(ProviderUsageFamily.OPENAI_CHAT_COMPLETIONS)

    snapshot = stream.finalize(provider_response_id="chatcmpl_missing_usage")

    assert snapshot.completeness is ProviderUsageCompleteness.INCOMPLETE
    assert snapshot.input_total_tokens.value is None
    assert snapshot.output_total_tokens.value is None
    assert snapshot.total_tokens.value is None
    assert [item.code.value for item in snapshot.anomalies] == ["missing_usage"]


def test_run_aggregation_preserves_unknown_instead_of_partial_sum() -> None:
    """任一请求指标未知时 run 累计保持 unknown，已知指标执行精确求和。"""
    manager = ProviderUsageManager()
    complete = manager.normalize(
        family=ProviderUsageFamily.OPENAI_CHAT_COMPLETIONS,
        raw_usage={
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
        },
        provider_response_id="resp_1",
    )
    partial = manager.normalize(
        family=ProviderUsageFamily.OPENAI_CHAT_COMPLETIONS,
        raw_usage={"completion_tokens": 3},
        provider_response_id="resp_2",
    )

    aggregate = aggregate_provider_usage_snapshots(
        (complete, partial),
        scope=ProviderUsageScope.RUN,
    )

    assert aggregate is not None
    assert aggregate.scope is ProviderUsageScope.RUN
    assert aggregate.input_total_tokens.value is None
    assert aggregate.output_total_tokens.value == 5
    assert aggregate.total_tokens.value is None
    assert aggregate.raw_usage["requests"][0]["provider_response_id"] == "resp_1"
