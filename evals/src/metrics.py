"""Harness Eval 成本 / 轮数指标聚合。

# 从 RecordingEventSink 采集的事件流中抽取 usage（kind=="usage" 的事件 payload，
# 由 core Runner 透传 LLMResponse.usage 产生），归一化 token 口径后做三层聚合：
# 单次 LLM 调用 → 单 trial → 单题（跨 repeat trial）→ 整套 run。
# 关键函数：
# - usage_totals_from_events：事件流 → 归一化 token 总量（支持 anthropic/openai 两种字段口径）
# - trial_metrics：单 trial 的轮数 + 时长 + token 指标
# - aggregate_task_metrics：跨 trial 汇总（token 求和、轮数均值/最大值、per_trial 明细）
# - merge_token_totals：token 字典逐 key 相加，供全局汇总复用
# - compute_cost：按 environments.yaml 可选 pricing（每 MTok 单价）换算成本，
#   未配置 pricing 时返回 None（只报 token 量，不臆造单价）。
# 本模块只依赖标准库，属于 eval harness 层，不 import src/core。
"""

from __future__ import annotations

from typing import Any

# token 汇总字典的固定 key 集合，三层聚合共用同一口径。
TOKEN_KEYS = (
    "prompt",
    "uncached_prompt",
    "cache_read",
    "cache_write",
    "completion",
    "total",
)


def _as_int(value: Any) -> int:
    """把 usage 字段安全转成非负 int，输入任意值，输出 int（非法值归 0）。"""

    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    return 0


def _normalize_usage_payload(payload: dict[str, Any]) -> dict[str, int]:
    """归一化单次 usage payload，输入事件 payload，输出统一口径 token 字典。

    兼容两种口径：
    - anthropic 风格：input_tokens（未命中）+ cache_read_input_tokens +
      cache_creation_input_tokens + output_tokens，prompt_tokens 为提交总量。
    - openai 风格：prompt_tokens / completion_tokens（可选 cached_tokens）。
    缺失字段按可推导关系补齐：prompt = 未命中 + cache读 + cache写。
    """

    cache_read = _as_int(payload.get("cache_read_input_tokens", payload.get("cached_tokens", 0)))
    cache_write = _as_int(payload.get("cache_creation_input_tokens", 0))
    completion = _as_int(payload.get("completion_tokens", payload.get("output_tokens", 0)))
    raw_uncached = payload.get("input_tokens")
    raw_prompt = payload.get("prompt_tokens")
    if raw_prompt is None:
        prompt = _as_int(raw_uncached) + cache_read + cache_write
    else:
        prompt = _as_int(raw_prompt)
    if raw_uncached is None:
        uncached = max(prompt - cache_read - cache_write, 0)
    else:
        uncached = _as_int(raw_uncached)
    total = _as_int(payload.get("total_tokens", prompt + completion))
    return {
        "prompt": prompt,
        "uncached_prompt": uncached,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "completion": completion,
        "total": total,
    }


def empty_token_totals() -> dict[str, int]:
    """输出全 0 的 token 汇总字典，输入为空，供聚合起点使用。"""

    return {key: 0 for key in TOKEN_KEYS}


def merge_token_totals(base: dict[str, int], extra: dict[str, int]) -> dict[str, int]:
    """逐 key 相加两个 token 字典，输入 base 与 extra，输出新字典（不改入参）。"""

    return {key: _as_int(base.get(key)) + _as_int(extra.get(key)) for key in TOKEN_KEYS}


def usage_totals_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """从事件流抽取 usage 汇总，输入 RecordingEventSink 事件列表，输出 llm_calls + tokens。

    只消费 kind=="usage" 的事件（core Runner 每次 LLM 响应 emit 一条）；
    fixture 模式伪 LLM usage 为空 dict，llm_calls 照计、token 记 0。
    """

    llm_calls = 0
    tokens = empty_token_totals()
    for event in events:
        if event.get("kind") != "usage":
            continue
        llm_calls += 1
        payload = event.get("payload")
        if isinstance(payload, dict):
            tokens = merge_token_totals(tokens, _normalize_usage_payload(payload))
    return {"llm_calls": llm_calls, "tokens": tokens}


def trial_metrics(
    events: list[dict[str, Any]],
    *,
    turn_count: int,
    duration_ms: int,
) -> dict[str, Any]:
    """构造单 trial 指标，输入事件流 + 轮数 + 时长，输出 trial 级指标字典。"""

    usage = usage_totals_from_events(events)
    return {
        "turns": max(int(turn_count), 0),
        "llm_calls": usage["llm_calls"],
        "duration_ms": max(int(duration_ms), 0),
        "tokens": usage["tokens"],
    }


def aggregate_task_metrics(trials: list[dict[str, Any]]) -> dict[str, Any]:
    """跨 trial 聚合单题指标，输入 trial 指标列表，输出题级 metrics。

    token / 调用数 / 时长为跨 trial 总和（本题总花费）；轮数报均值与最大值；
    per_trial 保留逐 trial 明细，供方差分析（findings 工作流）复用。
    """

    if not trials:
        return {
            "trials": 0,
            "turns_total": 0,
            "turns_mean": 0.0,
            "turns_max": 0,
            "llm_calls": 0,
            "duration_ms_total": 0,
            "tokens": empty_token_totals(),
            "per_trial": [],
        }
    turns_total = sum(trial["turns"] for trial in trials)
    tokens = empty_token_totals()
    for trial in trials:
        tokens = merge_token_totals(tokens, trial["tokens"])
    return {
        "trials": len(trials),
        "turns_total": turns_total,
        "turns_mean": round(turns_total / len(trials), 2),
        "turns_max": max(trial["turns"] for trial in trials),
        "llm_calls": sum(trial["llm_calls"] for trial in trials),
        "duration_ms_total": sum(trial["duration_ms"] for trial in trials),
        "tokens": tokens,
        "per_trial": [dict(trial) for trial in trials],
    }


def compute_cost(
    tokens: dict[str, int],
    pricing: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """按每 MTok 单价换算成本，输入 token 汇总与 pricing，输出成本明细或 None。

    计费口径：未命中 prompt 按 input 单价、cache 读/写按各自单价、completion 按
    output 单价。pricing 为 None（未配置）时返回 None——不臆造单价。
    """

    if not pricing:
        return None
    per_mtok = {
        "uncached_prompt": float(pricing["input_per_mtok"]),
        "cache_read": float(pricing["cache_read_per_mtok"]),
        "cache_write": float(pricing["cache_write_per_mtok"]),
        "completion": float(pricing["output_per_mtok"]),
    }
    breakdown = {
        bucket: round(_as_int(tokens.get(bucket)) / 1_000_000 * price, 6)
        for bucket, price in per_mtok.items()
    }
    return {
        "currency": str(pricing["currency"]),
        "total": round(sum(breakdown.values()), 6),
        "breakdown": breakdown,
    }
