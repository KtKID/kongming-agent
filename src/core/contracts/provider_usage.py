"""Provider token usage 跨模块数据合同。

本模块只定义不可变值对象与有限枚举，负责表达 provider family、计量范围、
完整性、公共指标、来源证据和异常。协议 family 的原始字段解析由
``infrastructure.llm_providers.usage.ProviderUsageManager`` 负责。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ProviderUsageFamily(StrEnum):
    """provider usage 原始 wire 协议族。"""

    ANTHROPIC_MESSAGES = "anthropic_messages"
    OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"
    OPENAI_RESPONSES = "openai_responses"


class ProviderUsageScope(StrEnum):
    """usage snapshot 的计量范围。"""

    REQUEST = "request"
    RUN = "run"
    THREAD = "thread"


class ProviderUsageCompleteness(StrEnum):
    """provider usage 是否包含协议要求的终态计量事件。"""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class ProviderUsageMetricOrigin(StrEnum):
    """公共指标的数值来源。"""

    PROVIDER_REPORTED = "provider_reported"
    DERIVED_EXACT = "derived_exact"
    UNAVAILABLE = "unavailable"


class ProviderUsageMetricName(StrEnum):
    """canonical provider usage 公共指标名。"""

    INPUT_TOTAL_TOKENS = "input_total_tokens"
    INPUT_UNCACHED_TOKENS = "input_uncached_tokens"
    CACHE_READ_TOKENS = "cache_read_tokens"
    CACHE_WRITE_TOKENS = "cache_write_tokens"
    OUTPUT_TOTAL_TOKENS = "output_total_tokens"
    REASONING_TOKENS = "reasoning_tokens"
    TOTAL_TOKENS = "total_tokens"


class ProviderUsageAnomalyCode(StrEnum):
    """usage 归一化异常的稳定代码。"""

    INVALID_METRIC = "invalid_metric"
    NEGATIVE_METRIC = "negative_metric"
    SUBSET_EXCEEDS_TOTAL = "subset_exceeds_total"
    TOTAL_MISMATCH = "total_mismatch"
    MISSING_USAGE = "missing_usage"
    UNSUPPORTED_PATH = "unsupported_path"
    FAMILY_MISMATCH = "family_mismatch"


@dataclass(frozen=True)
class ProviderUsageMetric:
    """单个 canonical token 指标及其来源证据。"""

    value: int | None
    origin: ProviderUsageMetricOrigin
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """校验 value 与 origin 一致，输入为当前实例，输出为空。"""
        if self.value is not None and (
            isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 0
        ):
            raise ValueError("provider usage metric value must be a non-negative int or None")
        if self.value is None and self.origin is not ProviderUsageMetricOrigin.UNAVAILABLE:
            raise ValueError("provider usage metric without value must be unavailable")
        if self.value is not None and self.origin is ProviderUsageMetricOrigin.UNAVAILABLE:
            raise ValueError("available provider usage metric requires a concrete origin")

    @classmethod
    def unavailable(cls, *evidence: str) -> ProviderUsageMetric:
        """构造未知指标，输入为缺失证据，输出为 unavailable metric。"""
        return cls(
            value=None,
            origin=ProviderUsageMetricOrigin.UNAVAILABLE,
            evidence=tuple(evidence),
        )

    @classmethod
    def provider_reported(cls, value: int, path: str) -> ProviderUsageMetric:
        """构造 provider 原报指标，输入为数值和 raw 路径，输出为 metric。"""
        return cls(
            value=value,
            origin=ProviderUsageMetricOrigin.PROVIDER_REPORTED,
            evidence=(path,),
        )

    @classmethod
    def derived_exact(cls, value: int, formula: str) -> ProviderUsageMetric:
        """构造精确派生指标，输入为数值和公式，输出为 metric。"""
        return cls(
            value=value,
            origin=ProviderUsageMetricOrigin.DERIVED_EXACT,
            evidence=(formula,),
        )

    def to_payload(self) -> dict[str, Any]:
        """序列化指标，输入为当前值对象，输出为 JSON-compatible dict。"""
        return {
            "value": self.value,
            "origin": self.origin.value,
            "evidence": list(self.evidence),
        }

    @classmethod
    def from_payload(cls, payload: object) -> ProviderUsageMetric:
        """反序列化指标，输入为任意 payload，输出为校验后的 metric。"""
        if not isinstance(payload, dict):
            return cls.unavailable("metric_payload_missing")
        value_raw = payload.get("value")
        value = (
            value_raw
            if isinstance(value_raw, int) and not isinstance(value_raw, bool) and value_raw >= 0
            else None
        )
        origin_raw = payload.get("origin")
        try:
            origin = ProviderUsageMetricOrigin(str(origin_raw))
        except ValueError:
            origin = ProviderUsageMetricOrigin.UNAVAILABLE
        evidence_raw = payload.get("evidence")
        evidence = (
            tuple(item for item in evidence_raw if isinstance(item, str))
            if isinstance(evidence_raw, list)
            else ()
        )
        if value is None or origin is ProviderUsageMetricOrigin.UNAVAILABLE:
            value = None
            origin = ProviderUsageMetricOrigin.UNAVAILABLE
        return cls(value=value, origin=origin, evidence=evidence)


@dataclass(frozen=True)
class ProviderUsageAnomaly:
    """归一化期间发现的非法、冲突或不完整证据。"""

    code: ProviderUsageAnomalyCode
    metric: ProviderUsageMetricName | None = None
    raw_path: str | None = None
    observed_repr: str | None = None
    computed_value: int | None = None
    message: str = ""

    def to_payload(self) -> dict[str, Any]:
        """序列化异常，输入为当前值对象，输出为 JSON-compatible dict。"""
        return {
            "code": self.code.value,
            "metric": self.metric.value if self.metric is not None else None,
            "raw_path": self.raw_path,
            "observed_repr": self.observed_repr,
            "computed_value": self.computed_value,
            "message": self.message,
        }

    @classmethod
    def from_payload(cls, payload: object) -> ProviderUsageAnomaly | None:
        """反序列化异常，输入为任意 payload，输出为 anomaly 或 None。"""
        if not isinstance(payload, dict):
            return None
        try:
            code = ProviderUsageAnomalyCode(str(payload.get("code")))
        except ValueError:
            return None
        metric_raw = payload.get("metric")
        try:
            metric = ProviderUsageMetricName(str(metric_raw)) if metric_raw is not None else None
        except ValueError:
            metric = None
        computed_raw = payload.get("computed_value")
        computed_value = (
            computed_raw
            if isinstance(computed_raw, int)
            and not isinstance(computed_raw, bool)
            and computed_raw >= 0
            else None
        )
        return cls(
            code=code,
            metric=metric,
            raw_path=_optional_str(payload.get("raw_path")),
            observed_repr=_optional_str(payload.get("observed_repr")),
            computed_value=computed_value,
            message=_optional_str(payload.get("message")) or "",
        )


@dataclass(frozen=True)
class ProviderUsageSnapshot:
    """单次 provider 响应或聚合范围的 canonical token usage 快照。"""

    family: ProviderUsageFamily
    scope: ProviderUsageScope = ProviderUsageScope.REQUEST
    completeness: ProviderUsageCompleteness = ProviderUsageCompleteness.COMPLETE
    input_total_tokens: ProviderUsageMetric = field(default_factory=ProviderUsageMetric.unavailable)
    input_uncached_tokens: ProviderUsageMetric = field(
        default_factory=ProviderUsageMetric.unavailable
    )
    cache_read_tokens: ProviderUsageMetric = field(default_factory=ProviderUsageMetric.unavailable)
    cache_write_tokens: ProviderUsageMetric = field(default_factory=ProviderUsageMetric.unavailable)
    output_total_tokens: ProviderUsageMetric = field(
        default_factory=ProviderUsageMetric.unavailable
    )
    reasoning_tokens: ProviderUsageMetric = field(default_factory=ProviderUsageMetric.unavailable)
    total_tokens: ProviderUsageMetric = field(default_factory=ProviderUsageMetric.unavailable)
    raw_usage: dict[str, Any] = field(default_factory=dict)
    anomalies: tuple[ProviderUsageAnomaly, ...] = ()
    provider_response_id: str | None = None

    def __post_init__(self) -> None:
        """冻结调用方 raw 副本，输入为当前实例，输出为空。"""
        object.__setattr__(self, "raw_usage", deepcopy(self.raw_usage))
        object.__setattr__(self, "anomalies", tuple(self.anomalies))

    def metric_items(
        self,
    ) -> tuple[tuple[ProviderUsageMetricName, ProviderUsageMetric], ...]:
        """列出公共指标，输入为当前快照，输出为稳定顺序的名称和值。"""
        return (
            (ProviderUsageMetricName.INPUT_TOTAL_TOKENS, self.input_total_tokens),
            (ProviderUsageMetricName.INPUT_UNCACHED_TOKENS, self.input_uncached_tokens),
            (ProviderUsageMetricName.CACHE_READ_TOKENS, self.cache_read_tokens),
            (ProviderUsageMetricName.CACHE_WRITE_TOKENS, self.cache_write_tokens),
            (ProviderUsageMetricName.OUTPUT_TOTAL_TOKENS, self.output_total_tokens),
            (ProviderUsageMetricName.REASONING_TOKENS, self.reasoning_tokens),
            (ProviderUsageMetricName.TOTAL_TOKENS, self.total_tokens),
        )

    def to_payload(self) -> dict[str, Any]:
        """序列化完整快照，输入为当前值对象，输出为 JSON-compatible dict。"""
        return {
            "family": self.family.value,
            "scope": self.scope.value,
            "completeness": self.completeness.value,
            "provider_response_id": self.provider_response_id,
            "metrics": {
                metric_name.value: metric.to_payload()
                for metric_name, metric in self.metric_items()
            },
            "raw_usage": deepcopy(self.raw_usage),
            "anomalies": [item.to_payload() for item in self.anomalies],
        }

    @classmethod
    def from_payload(cls, payload: object) -> ProviderUsageSnapshot:
        """反序列化完整快照，输入为任意 payload，输出为校验后的 snapshot。"""
        if not isinstance(payload, dict):
            raise ValueError("provider usage snapshot payload must be an object")
        try:
            family = ProviderUsageFamily(str(payload.get("family")))
            scope = ProviderUsageScope(str(payload.get("scope")))
            completeness = ProviderUsageCompleteness(str(payload.get("completeness")))
        except ValueError as exc:
            raise ValueError("provider usage snapshot enum field is invalid") from exc
        metrics_raw = payload.get("metrics")
        metrics = metrics_raw if isinstance(metrics_raw, dict) else {}
        raw_usage_value = payload.get("raw_usage")
        raw_usage = deepcopy(raw_usage_value) if isinstance(raw_usage_value, dict) else {}
        anomalies_raw = payload.get("anomalies")
        anomalies: list[ProviderUsageAnomaly] = []
        if isinstance(anomalies_raw, list):
            for item in anomalies_raw:
                anomaly = ProviderUsageAnomaly.from_payload(item)
                if anomaly is not None:
                    anomalies.append(anomaly)
        return cls(
            family=family,
            scope=scope,
            completeness=completeness,
            input_total_tokens=ProviderUsageMetric.from_payload(
                metrics.get(ProviderUsageMetricName.INPUT_TOTAL_TOKENS.value)
            ),
            input_uncached_tokens=ProviderUsageMetric.from_payload(
                metrics.get(ProviderUsageMetricName.INPUT_UNCACHED_TOKENS.value)
            ),
            cache_read_tokens=ProviderUsageMetric.from_payload(
                metrics.get(ProviderUsageMetricName.CACHE_READ_TOKENS.value)
            ),
            cache_write_tokens=ProviderUsageMetric.from_payload(
                metrics.get(ProviderUsageMetricName.CACHE_WRITE_TOKENS.value)
            ),
            output_total_tokens=ProviderUsageMetric.from_payload(
                metrics.get(ProviderUsageMetricName.OUTPUT_TOTAL_TOKENS.value)
            ),
            reasoning_tokens=ProviderUsageMetric.from_payload(
                metrics.get(ProviderUsageMetricName.REASONING_TOKENS.value)
            ),
            total_tokens=ProviderUsageMetric.from_payload(
                metrics.get(ProviderUsageMetricName.TOTAL_TOKENS.value)
            ),
            raw_usage=raw_usage,
            anomalies=tuple(anomalies),
            provider_response_id=_optional_str(payload.get("provider_response_id")),
        )


def aggregate_provider_usage_snapshots(
    snapshots: tuple[ProviderUsageSnapshot, ...],
    *,
    scope: ProviderUsageScope,
) -> ProviderUsageSnapshot | None:
    """聚合同一范围的请求快照，输入为有序快照，输出为保守累计快照。"""
    if not snapshots:
        return None

    family = snapshots[0].family
    anomalies = [anomaly for snapshot in snapshots for anomaly in snapshot.anomalies]
    family_mismatch = any(snapshot.family is not family for snapshot in snapshots[1:])
    if family_mismatch:
        anomalies.append(
            ProviderUsageAnomaly(
                code=ProviderUsageAnomalyCode.FAMILY_MISMATCH,
                message="cannot prove metric compatibility across provider families",
            )
        )

    metric_values: dict[ProviderUsageMetricName, ProviderUsageMetric] = {}
    for metric_name in ProviderUsageMetricName:
        metrics = tuple(_metric_by_name(snapshot, metric_name) for snapshot in snapshots)
        if family_mismatch or any(metric.value is None for metric in metrics):
            metric_values[metric_name] = ProviderUsageMetric.unavailable(
                f"sum(request.{metric_name.value})"
            )
            continue
        metric_values[metric_name] = ProviderUsageMetric.derived_exact(
            sum(metric.value for metric in metrics if metric.value is not None),
            f"sum(request.{metric_name.value})",
        )

    completeness = (
        ProviderUsageCompleteness.INCOMPLETE
        if family_mismatch
        or any(
            snapshot.completeness is ProviderUsageCompleteness.INCOMPLETE for snapshot in snapshots
        )
        else ProviderUsageCompleteness.COMPLETE
    )
    return ProviderUsageSnapshot(
        family=family,
        scope=scope,
        completeness=completeness,
        input_total_tokens=metric_values[ProviderUsageMetricName.INPUT_TOTAL_TOKENS],
        input_uncached_tokens=metric_values[ProviderUsageMetricName.INPUT_UNCACHED_TOKENS],
        cache_read_tokens=metric_values[ProviderUsageMetricName.CACHE_READ_TOKENS],
        cache_write_tokens=metric_values[ProviderUsageMetricName.CACHE_WRITE_TOKENS],
        output_total_tokens=metric_values[ProviderUsageMetricName.OUTPUT_TOTAL_TOKENS],
        reasoning_tokens=metric_values[ProviderUsageMetricName.REASONING_TOKENS],
        total_tokens=metric_values[ProviderUsageMetricName.TOTAL_TOKENS],
        raw_usage={
            "requests": [
                {
                    "provider_response_id": snapshot.provider_response_id,
                    "raw_usage": snapshot.raw_usage,
                }
                for snapshot in snapshots
            ]
        },
        anomalies=tuple(anomalies),
    )


def _metric_by_name(
    snapshot: ProviderUsageSnapshot,
    metric_name: ProviderUsageMetricName,
) -> ProviderUsageMetric:
    """按枚举读取快照指标，输入为 snapshot/name，输出为对应 metric。"""
    return {
        ProviderUsageMetricName.INPUT_TOTAL_TOKENS: snapshot.input_total_tokens,
        ProviderUsageMetricName.INPUT_UNCACHED_TOKENS: snapshot.input_uncached_tokens,
        ProviderUsageMetricName.CACHE_READ_TOKENS: snapshot.cache_read_tokens,
        ProviderUsageMetricName.CACHE_WRITE_TOKENS: snapshot.cache_write_tokens,
        ProviderUsageMetricName.OUTPUT_TOTAL_TOKENS: snapshot.output_total_tokens,
        ProviderUsageMetricName.REASONING_TOKENS: snapshot.reasoning_tokens,
        ProviderUsageMetricName.TOTAL_TOKENS: snapshot.total_tokens,
    }[metric_name]


def _optional_str(value: object) -> str | None:
    """读取可选字符串，输入为任意值，输出为非空字符串或 None。"""
    return value if isinstance(value, str) and value else None


__all__ = [
    "ProviderUsageAnomaly",
    "ProviderUsageAnomalyCode",
    "ProviderUsageCompleteness",
    "ProviderUsageFamily",
    "ProviderUsageMetric",
    "ProviderUsageMetricName",
    "ProviderUsageMetricOrigin",
    "ProviderUsageScope",
    "ProviderUsageSnapshot",
    "aggregate_provider_usage_snapshots",
]
