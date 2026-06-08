"""_derive_claude v2 派生器单测。

覆盖：取最后一条 / 不累加 / cache_creation 嵌套 / context_usage 派生 /
malformed line / 缺 usage / 文件不存在 / 流式大文件。
"""

from __future__ import annotations

import json
from pathlib import Path

# tests/ 不受 importlinter Contract 9 约束，允许 import 私有模块
from hosts.web.usage.usage_token_v2._derive_claude import derive_from_jsonl
from hosts.web.usage.usage_token_v2._models import ClaudeUsage


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def _assistant(usage: dict, model: str = "claude-opus-4") -> dict:
    return {
        "type": "assistant",
        "sessionId": "sid",
        "uuid": "u",
        "timestamp": "2026-05-15T00:00:00Z",
        "message": {
            "model": model,
            "id": "msg",
            "type": "message",
            "role": "assistant",
            "usage": usage,
        },
    }


def test_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert derive_from_jsonl(tmp_path / "nope.jsonl") is None


def test_returns_none_for_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    assert derive_from_jsonl(p) is None


def test_returns_none_when_no_assistant_entry(tmp_path: Path) -> None:
    p = tmp_path / "no-assistant.jsonl"
    _write_jsonl(
        p,
        [
            {"type": "user", "sessionId": "sid", "message": {"content": "hi"}},
            {"type": "system", "subtype": "init", "sessionId": "sid"},
        ],
    )
    assert derive_from_jsonl(p) is None


def test_takes_last_assistant_not_accumulated(tmp_path: Path) -> None:
    """多条 assistant，取**最后一条**，不累加（v2 核心语义）。"""
    p = tmp_path / "multi.jsonl"
    _write_jsonl(
        p,
        [
            _assistant({"input_tokens": 999, "output_tokens": 999}),  # 不取
            _assistant({"input_tokens": 999, "output_tokens": 999}),  # 不取
            _assistant(
                {
                    "input_tokens": 5,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 3,
                }
            ),  # 取这条
        ],
    )
    result = derive_from_jsonl(p)
    assert isinstance(result, ClaudeUsage)
    assert result.input_tokens == 5
    assert result.output_tokens == 10
    assert result.cache_read_input_tokens == 100
    assert result.cache_creation_input_tokens == 3
    # context_usage = 5 + 100 + 3
    assert result.context_usage == 108


def test_cache_creation_ttl_breakdown(tmp_path: Path) -> None:
    p = tmp_path / "ttl.jsonl"
    _write_jsonl(
        p,
        [
            _assistant(
                {
                    "input_tokens": 6,
                    "output_tokens": 881,
                    "cache_creation_input_tokens": 431,
                    "cache_creation": {
                        "ephemeral_1h_input_tokens": 431,
                        "ephemeral_5m_input_tokens": 0,
                    },
                }
            ),
        ],
    )
    result = derive_from_jsonl(p)
    assert result is not None
    assert result.cache_creation.ephemeral_1h_input_tokens == 431
    assert result.cache_creation.ephemeral_5m_input_tokens == 0


def test_skips_malformed_line(tmp_path: Path) -> None:
    p = tmp_path / "mixed.jsonl"
    valid = _assistant({"input_tokens": 7, "output_tokens": 3})
    with p.open("w", encoding="utf-8") as fh:
        fh.write("{NOT JSON\n")  # 损坏行
        fh.write(json.dumps(valid) + "\n")
    result = derive_from_jsonl(p)
    assert result is not None
    assert result.input_tokens == 7


def test_skips_assistant_without_usage(tmp_path: Path) -> None:
    p = tmp_path / "no-usage.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "assistant",
                "sessionId": "sid",
                "message": {"model": "x", "content": []},  # 缺 usage
            },
            _assistant({"input_tokens": 8, "output_tokens": 2}),
        ],
    )
    result = derive_from_jsonl(p)
    assert result is not None
    assert result.input_tokens == 8


def test_streaming_handles_large_file(tmp_path: Path) -> None:
    p = tmp_path / "large.jsonl"
    entries = [_assistant({"input_tokens": 1, "output_tokens": 1}) for _ in range(2000)]
    # 最后一条故意改大
    entries.append(_assistant({"input_tokens": 99, "output_tokens": 88}))
    _write_jsonl(p, entries)
    result = derive_from_jsonl(p)
    assert result is not None
    # 最后一条不累加：input_tokens == 99，不是 2001 * 1
    assert result.input_tokens == 99
    assert result.output_tokens == 88


def test_model_context_window_looked_up(tmp_path: Path) -> None:
    """model 字段映射到 context_window 表。"""
    p = tmp_path / "model.jsonl"
    _write_jsonl(p, [_assistant({"input_tokens": 1, "output_tokens": 1}, model="claude-opus-4")])
    result = derive_from_jsonl(p)
    assert result is not None
    assert result.model == "claude-opus-4"
    assert result.context_window == 1_000_000  # 已知模型表里的值


def test_provider_discriminator_is_claude(tmp_path: Path) -> None:
    p = tmp_path / "discrim.jsonl"
    _write_jsonl(p, [_assistant({"input_tokens": 1, "output_tokens": 1})])
    result = derive_from_jsonl(p)
    assert result is not None
    assert result.provider == "claude"
