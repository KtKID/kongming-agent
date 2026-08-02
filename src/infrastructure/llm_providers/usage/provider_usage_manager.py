"""ProviderUsageManager：provider usage 归一化唯一门户。

本模块接收 Anthropic Messages、OpenAI Chat Completions 和 OpenAI Responses
原始 usage。流式 session 先按协议时序聚合 fragment，终态再统一构造
``ProviderUsageSnapshot``。Manager 无 IO、无跨请求缓存。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from core.contracts import (
    ProviderUsageAnomaly,
    ProviderUsageAnomalyCode,
    ProviderUsageCompleteness,
    ProviderUsageFamily,
    ProviderUsageMetric,
    ProviderUsageMetricName,
    ProviderUsageScope,
    ProviderUsageSnapshot,
)


class ProviderUsageManager:
    """把 provider family 原始 usage 收敛为 canonical snapshot。"""

    def normalize(
        self,
        *,
        family: ProviderUsageFamily,
        raw_usage: dict[str, Any],
        completeness: ProviderUsageCompleteness = ProviderUsageCompleteness.COMPLETE,
        scope: ProviderUsageScope = ProviderUsageScope.REQUEST,
        provider_response_id: str | None = None,
    ) -> ProviderUsageSnapshot:
        """归一化一次 usage，输入为 family/raw/范围，输出为冻结快照。"""
        raw = deepcopy(raw_usage)
        if family is ProviderUsageFamily.ANTHROPIC_MESSAGES:
            return self._normalize_anthropic(
                raw=raw,
                completeness=completeness,
                scope=scope,
                provider_response_id=provider_response_id,
            )
        if family is ProviderUsageFamily.OPENAI_CHAT_COMPLETIONS:
            return self._normalize_openai_chat(
                raw=raw,
                completeness=completeness,
                scope=scope,
                provider_response_id=provider_response_id,
            )
        return self._normalize_openai_responses(
            raw=raw,
            completeness=completeness,
            scope=scope,
            provider_response_id=provider_response_id,
        )

    def start_stream(self, family: ProviderUsageFamily) -> _ProviderUsageStreamSession:
        """创建单条流的 usage session，输入为 family，输出为可变 collector。"""
        return _ProviderUsageStreamSession(manager=self, family=family)

    def _normalize_anthropic(
        self,
        *,
        raw: dict[str, Any],
        completeness: ProviderUsageCompleteness,
        scope: ProviderUsageScope,
        provider_response_id: str | None,
    ) -> ProviderUsageSnapshot:
        """归一化 Anthropic Messages usage，输入为 raw，输出为 snapshot。"""
        anomalies = _incomplete_anomalies(raw, completeness)
        input_uncached = _read_metric(
            raw,
            path=("input_tokens",),
            metric_name=ProviderUsageMetricName.INPUT_UNCACHED_TOKENS,
            anomalies=anomalies,
        )
        cache_read = _read_metric(
            raw,
            path=("cache_read_input_tokens",),
            metric_name=ProviderUsageMetricName.CACHE_READ_TOKENS,
            anomalies=anomalies,
        )
        cache_write = _read_metric(
            raw,
            path=("cache_creation_input_tokens",),
            metric_name=ProviderUsageMetricName.CACHE_WRITE_TOKENS,
            anomalies=anomalies,
        )
        output_total = _read_metric(
            raw,
            path=("output_tokens",),
            metric_name=ProviderUsageMetricName.OUTPUT_TOTAL_TOKENS,
            anomalies=anomalies,
        )
        reasoning = _read_metric(
            raw,
            path=("output_tokens_details", "reasoning_tokens"),
            metric_name=ProviderUsageMetricName.REASONING_TOKENS,
            anomalies=anomalies,
        )
        reasoning = _validate_subset(
            subset=reasoning,
            total=output_total,
            subset_name=ProviderUsageMetricName.REASONING_TOKENS,
            total_name=ProviderUsageMetricName.OUTPUT_TOTAL_TOKENS,
            anomalies=anomalies,
        )
        input_total = _sum_metrics(
            (input_uncached, cache_read, cache_write),
            formula=("input_tokens + cache_read_input_tokens + cache_creation_input_tokens"),
        )
        reported_total = _read_metric(
            raw,
            path=("total_tokens",),
            metric_name=ProviderUsageMetricName.TOTAL_TOKENS,
            anomalies=anomalies,
        )
        total = _resolve_total(
            reported=reported_total,
            input_total=input_total,
            output_total=output_total,
            anomalies=anomalies,
        )
        return ProviderUsageSnapshot(
            family=ProviderUsageFamily.ANTHROPIC_MESSAGES,
            scope=scope,
            completeness=completeness,
            input_total_tokens=input_total,
            input_uncached_tokens=input_uncached,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            output_total_tokens=output_total,
            reasoning_tokens=reasoning,
            total_tokens=total,
            raw_usage=raw,
            anomalies=tuple(anomalies),
            provider_response_id=provider_response_id,
        )

    def _normalize_openai_chat(
        self,
        *,
        raw: dict[str, Any],
        completeness: ProviderUsageCompleteness,
        scope: ProviderUsageScope,
        provider_response_id: str | None,
    ) -> ProviderUsageSnapshot:
        """归一化 OpenAI Chat usage，输入为 raw，输出为 snapshot。"""
        return self._normalize_openai_common(
            family=ProviderUsageFamily.OPENAI_CHAT_COMPLETIONS,
            raw=raw,
            completeness=completeness,
            scope=scope,
            provider_response_id=provider_response_id,
            input_path=("prompt_tokens",),
            cache_read_path=("prompt_tokens_details", "cached_tokens"),
            output_path=("completion_tokens",),
            reasoning_path=("completion_tokens_details", "reasoning_tokens"),
        )

    def _normalize_openai_responses(
        self,
        *,
        raw: dict[str, Any],
        completeness: ProviderUsageCompleteness,
        scope: ProviderUsageScope,
        provider_response_id: str | None,
    ) -> ProviderUsageSnapshot:
        """归一化 OpenAI Responses usage，输入为 raw，输出为 snapshot。"""
        return self._normalize_openai_common(
            family=ProviderUsageFamily.OPENAI_RESPONSES,
            raw=raw,
            completeness=completeness,
            scope=scope,
            provider_response_id=provider_response_id,
            input_path=("input_tokens",),
            cache_read_path=("input_tokens_details", "cached_tokens"),
            output_path=("output_tokens",),
            reasoning_path=("output_tokens_details", "reasoning_tokens"),
        )

    def _normalize_openai_common(
        self,
        *,
        family: ProviderUsageFamily,
        raw: dict[str, Any],
        completeness: ProviderUsageCompleteness,
        scope: ProviderUsageScope,
        provider_response_id: str | None,
        input_path: tuple[str, ...],
        cache_read_path: tuple[str, ...],
        output_path: tuple[str, ...],
        reasoning_path: tuple[str, ...],
    ) -> ProviderUsageSnapshot:
        """归一化 OpenAI 两类公共语义，输入为字段路径，输出为 snapshot。"""
        anomalies = _incomplete_anomalies(raw, completeness)
        input_total = _read_metric(
            raw,
            path=input_path,
            metric_name=ProviderUsageMetricName.INPUT_TOTAL_TOKENS,
            anomalies=anomalies,
        )
        cache_read = _read_metric(
            raw,
            path=cache_read_path,
            metric_name=ProviderUsageMetricName.CACHE_READ_TOKENS,
            anomalies=anomalies,
        )
        cache_read = _validate_subset(
            subset=cache_read,
            total=input_total,
            subset_name=ProviderUsageMetricName.CACHE_READ_TOKENS,
            total_name=ProviderUsageMetricName.INPUT_TOTAL_TOKENS,
            anomalies=anomalies,
        )
        input_uncached = ProviderUsageMetric.unavailable(
            "cache subset policy incomplete for this OpenAI family"
        )
        cache_write = ProviderUsageMetric.unavailable("provider_path_unavailable")
        if "cache_write_tokens" in raw:
            anomalies.append(
                ProviderUsageAnomaly(
                    code=ProviderUsageAnomalyCode.UNSUPPORTED_PATH,
                    metric=ProviderUsageMetricName.CACHE_WRITE_TOKENS,
                    raw_path="usage.cache_write_tokens",
                    observed_repr=repr(raw["cache_write_tokens"]),
                    message="cache write path requires an explicit family policy",
                )
            )
        output_total = _read_metric(
            raw,
            path=output_path,
            metric_name=ProviderUsageMetricName.OUTPUT_TOTAL_TOKENS,
            anomalies=anomalies,
        )
        reasoning = _read_metric(
            raw,
            path=reasoning_path,
            metric_name=ProviderUsageMetricName.REASONING_TOKENS,
            anomalies=anomalies,
        )
        reasoning = _validate_subset(
            subset=reasoning,
            total=output_total,
            subset_name=ProviderUsageMetricName.REASONING_TOKENS,
            total_name=ProviderUsageMetricName.OUTPUT_TOTAL_TOKENS,
            anomalies=anomalies,
        )
        reported_total = _read_metric(
            raw,
            path=("total_tokens",),
            metric_name=ProviderUsageMetricName.TOTAL_TOKENS,
            anomalies=anomalies,
        )
        total = _resolve_total(
            reported=reported_total,
            input_total=input_total,
            output_total=output_total,
            anomalies=anomalies,
        )
        return ProviderUsageSnapshot(
            family=family,
            scope=scope,
            completeness=completeness,
            input_total_tokens=input_total,
            input_uncached_tokens=input_uncached,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            output_total_tokens=output_total,
            reasoning_tokens=reasoning,
            total_tokens=total,
            raw_usage=raw,
            anomalies=tuple(anomalies),
            provider_response_id=provider_response_id,
        )


@dataclass
class _ProviderUsageStreamSession:
    """单条 provider 流的 usage fragment collector。"""

    manager: ProviderUsageManager
    family: ProviderUsageFamily
    _effective_raw: dict[str, Any] = field(default_factory=dict)
    _saw_usage: bool = False
    _saw_terminal_usage: bool = False

    def ingest(
        self,
        raw_usage_fragment: dict[str, Any],
        *,
        terminal: bool = False,
    ) -> None:
        """接收 usage fragment，输入为原始对象/终态标记，输出为空。"""
        if not raw_usage_fragment:
            return
        self._saw_usage = True
        self._saw_terminal_usage = self._saw_terminal_usage or terminal
        if self.family is ProviderUsageFamily.ANTHROPIC_MESSAGES:
            for key, value in raw_usage_fragment.items():
                self._effective_raw[key] = deepcopy(value)
            return
        self._effective_raw = deepcopy(raw_usage_fragment)

    def finalize(self, *, provider_response_id: str | None = None) -> ProviderUsageSnapshot:
        """完成流式归一化，输入为 response id，输出为终态 snapshot。"""
        completeness = (
            ProviderUsageCompleteness.COMPLETE
            if self._saw_usage and self._saw_terminal_usage
            else ProviderUsageCompleteness.INCOMPLETE
        )
        return self.manager.normalize(
            family=self.family,
            raw_usage=self._effective_raw,
            completeness=completeness,
            provider_response_id=provider_response_id,
        )


def _read_metric(
    raw: dict[str, Any],
    *,
    path: tuple[str, ...],
    metric_name: ProviderUsageMetricName,
    anomalies: list[ProviderUsageAnomaly],
) -> ProviderUsageMetric:
    """读取 raw 数值路径，输入为字段路径，输出为 metric 并追加异常。"""
    found, value = _read_path(raw, path)
    evidence_path = f"usage.{'.'.join(path)}"
    if not found:
        return ProviderUsageMetric.unavailable(f"{evidence_path}:missing")
    if isinstance(value, bool) or not isinstance(value, int):
        anomalies.append(
            ProviderUsageAnomaly(
                code=ProviderUsageAnomalyCode.INVALID_METRIC,
                metric=metric_name,
                raw_path=evidence_path,
                observed_repr=repr(value),
                message="token metric must be an integer",
            )
        )
        return ProviderUsageMetric.unavailable(f"{evidence_path}:invalid")
    if value < 0:
        anomalies.append(
            ProviderUsageAnomaly(
                code=ProviderUsageAnomalyCode.NEGATIVE_METRIC,
                metric=metric_name,
                raw_path=evidence_path,
                observed_repr=repr(value),
                message="token metric must be non-negative",
            )
        )
        return ProviderUsageMetric.unavailable(f"{evidence_path}:negative")
    return ProviderUsageMetric.provider_reported(value, evidence_path)


def _read_path(raw: dict[str, Any], path: tuple[str, ...]) -> tuple[bool, object]:
    """读取嵌套 raw 路径，输入为字典和路径，输出为 found/value。"""
    current: object = raw
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _sum_metrics(
    metrics: tuple[ProviderUsageMetric, ...],
    *,
    formula: str,
) -> ProviderUsageMetric:
    """精确求和指标，输入为完整 metric 元组，输出为 derived 或 unknown。"""
    values = [metric.value for metric in metrics]
    if any(value is None for value in values):
        return ProviderUsageMetric.unavailable(f"{formula}:incomplete")
    return ProviderUsageMetric.derived_exact(
        sum(value for value in values if value is not None), formula
    )


def _validate_subset(
    *,
    subset: ProviderUsageMetric,
    total: ProviderUsageMetric,
    subset_name: ProviderUsageMetricName,
    total_name: ProviderUsageMetricName,
    anomalies: list[ProviderUsageAnomaly],
) -> ProviderUsageMetric:
    """校验子集上界，输入为 subset/total，输出为有效或 unknown metric。"""
    if subset.value is None or total.value is None or subset.value <= total.value:
        return subset
    anomalies.append(
        ProviderUsageAnomaly(
            code=ProviderUsageAnomalyCode.SUBSET_EXCEEDS_TOTAL,
            metric=subset_name,
            raw_path=subset.evidence[0] if subset.evidence else None,
            observed_repr=repr(subset.value),
            computed_value=total.value,
            message=f"{subset_name.value} exceeds {total_name.value}",
        )
    )
    return ProviderUsageMetric.unavailable(f"{subset_name.value}:subset_exceeds_total")


def _resolve_total(
    *,
    reported: ProviderUsageMetric,
    input_total: ProviderUsageMetric,
    output_total: ProviderUsageMetric,
    anomalies: list[ProviderUsageAnomaly],
) -> ProviderUsageMetric:
    """选择 provider total 或精确派生值，输入为三项指标，输出为 total metric。"""
    derived = _sum_metrics(
        (input_total, output_total),
        formula="input_total_tokens + output_total_tokens",
    )
    if reported.value is None:
        return derived
    if derived.value is not None and reported.value != derived.value:
        anomalies.append(
            ProviderUsageAnomaly(
                code=ProviderUsageAnomalyCode.TOTAL_MISMATCH,
                metric=ProviderUsageMetricName.TOTAL_TOKENS,
                raw_path=reported.evidence[0] if reported.evidence else "usage.total_tokens",
                observed_repr=repr(reported.value),
                computed_value=derived.value,
                message="provider total differs from exact input/output sum",
            )
        )
    return reported


def _incomplete_anomalies(
    raw: dict[str, Any],
    completeness: ProviderUsageCompleteness,
) -> list[ProviderUsageAnomaly]:
    """建立不完整 usage 异常，输入为 raw/completeness，输出为异常列表。"""
    if completeness is ProviderUsageCompleteness.COMPLETE:
        return []
    return [
        ProviderUsageAnomaly(
            code=ProviderUsageAnomalyCode.MISSING_USAGE,
            observed_repr=repr(raw) if raw else None,
            message="provider stream ended without a complete terminal usage object",
        )
    ]


__all__ = ["ProviderUsageManager"]
