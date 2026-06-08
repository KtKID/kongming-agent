"""_derive_codex v2 派生器单测。

覆盖：取最后一条非空 token_count / info=None 跳过 / total + last 1:1 映射
/ model_context_window 透传 / rate_limits 透传 / 文件不存在 / 损坏行。
"""

from __future__ import annotations

import json
from pathlib import Path

from web.usage.usage_token_v2._derive_codex import derive_from_rollout
from web.usage.usage_token_v2._models import CodexUsage


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def _token_count_event(
    *,
    info: dict | None,
    rate_limits: dict | None = None,
) -> dict:
    return {
        "timestamp": "2026-05-15T00:00:00Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": info,
            "rate_limits": rate_limits,
        },
    }


def test_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert derive_from_rollout(tmp_path / "nope.jsonl") is None


def test_returns_none_when_no_token_count_event(tmp_path: Path) -> None:
    p = tmp_path / "no-token.jsonl"
    _write_jsonl(
        p,
        [
            {"type": "session_meta", "payload": {"id": "x"}},
            {"type": "response_item", "payload": {"type": "message"}},
        ],
    )
    assert derive_from_rollout(p) is None


def test_skips_info_none_token_count(tmp_path: Path) -> None:
    """codex 偶尔 emit info=None 的占位事件，应跳过。"""
    p = tmp_path / "null-info.jsonl"
    _write_jsonl(p, [_token_count_event(info=None), _token_count_event(info=None)])
    assert derive_from_rollout(p) is None


def test_takes_last_non_null_info(tmp_path: Path) -> None:
    """多个 token_count 事件，取最后一条非空 info 的。"""
    p = tmp_path / "multi.jsonl"
    _write_jsonl(
        p,
        [
            _token_count_event(
                info={
                    "total_token_usage": {
                        "input_tokens": 999,
                        "output_tokens": 999,
                        "total_tokens": 1998,
                    },
                    "last_token_usage": {
                        "input_tokens": 999,
                        "output_tokens": 999,
                        "total_tokens": 1998,
                    },
                    "model_context_window": 100000,
                }
            ),
            _token_count_event(info=None),  # 跳过
            _token_count_event(
                info={
                    "total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 50,
                        "output_tokens": 30,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 130,
                    },
                    "last_token_usage": {
                        "input_tokens": 80,
                        "cached_input_tokens": 40,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 3,
                        "total_tokens": 100,
                    },
                    "model_context_window": 200000,
                }
            ),
        ],
    )
    result = derive_from_rollout(p)
    assert isinstance(result, CodexUsage)
    # 取最后一条（不取第一条 999）
    assert result.total.input_tokens == 100
    assert result.total.cached_input_tokens == 50
    assert result.total.output_tokens == 30
    assert result.total.reasoning_output_tokens == 5
    assert result.total.total_tokens == 130
    assert result.last.input_tokens == 80
    assert result.last.output_tokens == 20
    assert result.model_context_window == 200000


def test_maps_rate_limits_when_present(tmp_path: Path) -> None:
    p = tmp_path / "rate.jsonl"
    _write_jsonl(
        p,
        [
            _token_count_event(
                info={
                    "total_token_usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    "last_token_usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    "model_context_window": 258400,
                },
                rate_limits={
                    "primary": {
                        "used_percent": 27.0,
                        "window_minutes": 300,
                        "resets_at": 1777965554,
                    },
                    "secondary": {
                        "used_percent": 20.0,
                        "window_minutes": 10080,
                        "resets_at": 1777962127,
                    },
                    "limit_id": "codex",  # 应被忽略
                    "plan_type": "plus",
                },
            ),
        ],
    )
    result = derive_from_rollout(p)
    assert result is not None
    assert result.rate_limits is not None
    assert result.rate_limits.primary.used_percent == 27.0
    assert result.rate_limits.primary.window_minutes == 300
    assert result.rate_limits.secondary.window_minutes == 10080
    assert result.rate_limits.plan_type == "plus"


def test_rate_limits_none_when_missing(tmp_path: Path) -> None:
    p = tmp_path / "no-rate.jsonl"
    _write_jsonl(
        p,
        [
            _token_count_event(
                info={
                    "total_token_usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    "last_token_usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    "model_context_window": 100,
                }
            )
        ],
    )
    result = derive_from_rollout(p)
    assert result is not None
    assert result.rate_limits is None


def test_skips_non_token_count_events(tmp_path: Path) -> None:
    p = tmp_path / "mixed.jsonl"
    _write_jsonl(
        p,
        [
            {"type": "response_item", "payload": {"type": "message"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            _token_count_event(
                info={
                    "total_token_usage": {
                        "input_tokens": 42,
                        "output_tokens": 7,
                        "total_tokens": 49,
                    },
                    "last_token_usage": {
                        "input_tokens": 42,
                        "output_tokens": 7,
                        "total_tokens": 49,
                    },
                    "model_context_window": 99,
                }
            ),
        ],
    )
    result = derive_from_rollout(p)
    assert result is not None
    assert result.total.input_tokens == 42


def test_skips_malformed_line(tmp_path: Path) -> None:
    p = tmp_path / "malformed.jsonl"
    valid = _token_count_event(
        info={
            "total_token_usage": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
            "last_token_usage": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
            "model_context_window": 10,
        }
    )
    with p.open("w", encoding="utf-8") as fh:
        fh.write("{INVALID\n")
        fh.write(json.dumps(valid) + "\n")
    result = derive_from_rollout(p)
    assert result is not None
    assert result.total.input_tokens == 5


def test_provider_discriminator_is_openai(tmp_path: Path) -> None:
    p = tmp_path / "discrim.jsonl"
    _write_jsonl(
        p,
        [
            _token_count_event(
                info={
                    "total_token_usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    "last_token_usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    "model_context_window": 10,
                }
            )
        ],
    )
    result = derive_from_rollout(p)
    assert result is not None
    assert result.provider == "openai"
