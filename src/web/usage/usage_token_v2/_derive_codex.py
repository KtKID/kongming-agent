"""Codex 通道派生器：从 rollout jsonl 取最后一条非空 token_count → CodexUsage。

⚠️ **架构边界**：本模块是 ``web.usage.usage_token_v2`` 包私有，外部禁止 import
（``.importlinter`` Contract 9 强制）；只能通过 ``UsageTokenManager`` 间接消费。

设计要点：

- 扫 codex rollout jsonl 的 ``event_msg`` 行，过滤 ``payload.type == "token_count"``
- **跳过 ``info=None`` 的 token_count 事件**（codex 在某些时机 emit 占位）
- 取**最后一条** ``info`` 非空的 token_count 事件
- codex **自带累加**：``info.total_token_usage`` / ``info.last_token_usage``
  / ``info.model_context_window`` 直接 1:1 映射，**不再自己加**
- ``rate_limits`` 透传（v2 后端透传字段，前端 v3 决定怎么渲染）
- 文件不存在 / 不可读 / 没有任何非空 token_count → 返回 None
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from web.usage.usage_token_v2._models import (
    CodexRateLimits,
    CodexRateLimitWindow,
    CodexTokenBreakdown,
    CodexUsage,
)

logger = logging.getLogger(__name__)

__all__ = ["derive_from_rollout"]


def derive_from_rollout(rollout_path: Path) -> CodexUsage | None:
    """扫 Codex rollout jsonl 派生 CodexUsage。

    Args:
        rollout_path: ``~/.codex/sessions/YYYY/MM/DD/rollout-...-<uuid>.jsonl``。
            由调用方（manager 注入的 locator）拼好；本函数不感知路径规则。

    Returns:
        ``CodexUsage``，或 ``None``（文件不存在 / 不可读 / 无非空 token_count）
    """
    if not rollout_path.is_file():
        return None

    last_token_event: dict[str, Any] | None = None

    try:
        fh = rollout_path.open("r", encoding="utf-8")
    except OSError as exc:
        logger.warning("derive_from_rollout: cannot open %s: %s", rollout_path, exc)
        return None

    try:
        with fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (ValueError, json.JSONDecodeError) as exc:
                    logger.warning(
                        "derive_from_rollout: skip malformed line in %s: %s",
                        rollout_path,
                        exc,
                    )
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "event_msg":
                    continue
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") != "token_count":
                    continue
                if payload.get("info") is None:
                    # codex 偶尔 emit info=None 的占位，跳过
                    continue
                last_token_event = payload
    except OSError as exc:
        logger.warning("derive_from_rollout: read error on %s: %s", rollout_path, exc)
        return None

    if last_token_event is None:
        return None

    return _map_to_codex_usage(last_token_event)


def _map_to_codex_usage(payload: dict[str, Any]) -> CodexUsage:
    """codex token_count event payload → CodexUsage DTO（1:1 映射）。"""
    info = payload.get("info") or {}

    return CodexUsage(
        total=_breakdown(info.get("total_token_usage")),
        last=_breakdown(info.get("last_token_usage")),
        model_context_window=_safe_int(info.get("model_context_window")),
        rate_limits=_rate_limits(payload.get("rate_limits")),
    )


def _breakdown(raw: Any) -> CodexTokenBreakdown:
    """codex token usage 5 字段 → CodexTokenBreakdown（缺失填 0）。"""
    if not isinstance(raw, dict):
        return CodexTokenBreakdown()
    return CodexTokenBreakdown(
        input_tokens=_safe_int(raw.get("input_tokens")),
        cached_input_tokens=_safe_int(raw.get("cached_input_tokens")),
        output_tokens=_safe_int(raw.get("output_tokens")),
        reasoning_output_tokens=_safe_int(raw.get("reasoning_output_tokens")),
        total_tokens=_safe_int(raw.get("total_tokens")),
    )


def _rate_limits(raw: Any) -> CodexRateLimits | None:
    """rate_limits dict → CodexRateLimits 或 None（缺 primary/secondary 则 None）。"""
    if not isinstance(raw, dict):
        return None
    primary_raw = raw.get("primary")
    secondary_raw = raw.get("secondary")
    if not isinstance(primary_raw, dict) or not isinstance(secondary_raw, dict):
        return None
    return CodexRateLimits(
        primary=_window(primary_raw),
        secondary=_window(secondary_raw),
        plan_type=str(raw.get("plan_type") or ""),
    )


def _window(raw: dict[str, Any]) -> CodexRateLimitWindow:
    """rate_limits.primary / .secondary dict → CodexRateLimitWindow。"""
    return CodexRateLimitWindow(
        used_percent=_safe_float(raw.get("used_percent")),
        window_minutes=_safe_int(raw.get("window_minutes")),
        resets_at=_safe_int(raw.get("resets_at")),
    )


def _safe_int(value: Any) -> int:
    """把任意 value 转 int；None / 非数字 / 负数 → 0。"""
    if value is None:
        return 0
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, n)


def _safe_float(value: Any) -> float:
    """把任意 value 转 float；None / 非数字 / 负数 → 0.0。"""
    if value is None:
        return 0.0
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, n)
