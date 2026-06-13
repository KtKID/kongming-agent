"""_derive_generic v2 派生器单测。

覆盖：anthropic vs openai_compatible 分支 / 取最后一条 / 不累加 / 缺 usage 跳过
/ 文件不存在 / 未知 provider / model_name 透传。
"""

from __future__ import annotations

import json
from pathlib import Path

from hosts.web.usage.usage_token_v2._derive_generic import derive_from_session
from hosts.web.usage.usage_token_v2._models import (
    GenericChatAnthropicUsage,
    GenericChatOpenAIUsage,
)


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def _message_entry(usage: dict | None, model: str = "claude-opus-4") -> dict:
    """模拟 FileSession.append 的 jsonl 行。"""
    entry: dict = {
        "schema_version": 1,
        "session_id": "sid",
        "model_name": model,
        "message_id": "msg",
        "parent_message_id": None,
        "created_at": 1234567890.0,
        "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    }
    if usage is not None:
        entry["usage"] = usage
    return entry


def test_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert derive_from_session(tmp_path / "nope.jsonl", "anthropic") is None


def test_returns_none_when_no_usage_lines(tmp_path: Path) -> None:
    p = tmp_path / "no-usage.jsonl"
    _write_jsonl(p, [_message_entry(None), _message_entry(None)])
    assert derive_from_session(p, "anthropic") is None


def test_anthropic_provider_returns_anthropic_dto(tmp_path: Path) -> None:
    p = tmp_path / "anth.jsonl"
    _write_jsonl(
        p,
        [
            _message_entry({"input_tokens": 999, "output_tokens": 999}),  # 不取
            _message_entry(
                {
                    "input_tokens": 5,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 3,
                    "cache_creation": {
                        "ephemeral_1h_input_tokens": 3,
                        "ephemeral_5m_input_tokens": 0,
                    },
                },
                model="claude-sonnet-4",
            ),
        ],
    )
    result = derive_from_session(p, "anthropic")
    assert isinstance(result, GenericChatAnthropicUsage)
    assert result.provider == "claude"
    # 取最后一条不累加
    assert result.input_tokens == 5
    assert result.cache_read_input_tokens == 100
    assert result.cache_creation_input_tokens == 3
    assert result.cache_creation.ephemeral_1h_input_tokens == 3
    assert result.context_usage == 108
    assert result.model == "claude-sonnet-4"


def test_openai_provider_returns_openai_dto(tmp_path: Path) -> None:
    p = tmp_path / "oai.jsonl"
    _write_jsonl(
        p,
        [
            _message_entry(
                {
                    "input_tokens": 200,
                    "cached_input_tokens": 100,
                    "output_tokens": 50,
                    "reasoning_output_tokens": 10,
                    "total_tokens": 250,
                },
                model="gpt-4o",
            ),
        ],
    )
    result = derive_from_session(p, "openai_compatible")
    assert isinstance(result, GenericChatOpenAIUsage)
    assert result.provider == "openai"
    assert result.last.input_tokens == 200
    assert result.last.cached_input_tokens == 100
    assert result.last.output_tokens == 50
    assert result.last.reasoning_output_tokens == 10
    assert result.last.total_tokens == 250
    assert result.model == "gpt-4o"


def test_takes_last_usage_not_accumulated(tmp_path: Path) -> None:
    p = tmp_path / "multi.jsonl"
    _write_jsonl(
        p,
        [
            _message_entry({"input_tokens": 100, "output_tokens": 50}),
            _message_entry({"input_tokens": 200, "output_tokens": 80}),
            _message_entry({"input_tokens": 300, "output_tokens": 120}),
        ],
    )
    result = derive_from_session(p, "anthropic")
    assert result is not None
    # 取最后一条
    assert result.input_tokens == 300
    assert result.output_tokens == 120


def test_unknown_provider_returns_none(tmp_path: Path) -> None:
    """未知 provider → 返回 None（防御兜底）。"""
    p = tmp_path / "x.jsonl"
    _write_jsonl(p, [_message_entry({"input_tokens": 1, "output_tokens": 1})])
    # 故意传 bogus provider 类型——运行时返 None（不抛）
    result = derive_from_session(p, "unknown")  # type: ignore[arg-type]
    assert result is None


def test_skips_message_without_usage(tmp_path: Path) -> None:
    """有 message 但无 usage 的行被跳过；后续含 usage 的行被取到。"""
    p = tmp_path / "mixed.jsonl"
    _write_jsonl(
        p,
        [
            _message_entry(None),  # user message 没 usage
            _message_entry(None),  # tool result 没 usage
            _message_entry({"input_tokens": 99, "output_tokens": 11}),
        ],
    )
    result = derive_from_session(p, "anthropic")
    assert result is not None
    assert result.input_tokens == 99


def test_skips_malformed_line(tmp_path: Path) -> None:
    p = tmp_path / "malformed.jsonl"
    valid = _message_entry({"input_tokens": 7, "output_tokens": 3})
    with p.open("w", encoding="utf-8") as fh:
        fh.write("INVALID\n")
        fh.write(json.dumps(valid) + "\n")
    result = derive_from_session(p, "anthropic")
    assert result is not None
    assert result.input_tokens == 7
