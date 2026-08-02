"""generic_chat usage 派生器单元测试。

覆盖 canonical snapshot 的 family 路由、最后一条选择、unknown 保真、坏行跳过
和 model_name 透传。FileSession 之外的 provider 数据由其他派生器测试负责。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.contracts import ProviderUsageFamily
from hosts.web.usage.usage_token_v2._derive_generic import derive_from_session
from hosts.web.usage.usage_token_v2._models import (
    GenericChatAnthropicUsage,
    GenericChatOpenAIUsage,
)
from infrastructure.llm_providers.usage import ProviderUsageManager


def _write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    """写入测试 JSONL，输入为路径和记录，输出为空。"""
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def _message_entry(
    usage: dict[str, Any] | None,
    model: str = "claude-opus-4",
) -> dict[str, Any]:
    """构造 FileSession 消息记录，输入为 usage/model，输出为 JSON 行对象。"""
    entry: dict[str, Any] = {
        "schema_version": 1,
        "session_id": "sid",
        "model_name": model,
        "message_id": "msg",
        "parent_message_id": None,
        "created_at": 1234567890.0,
        "message": {"role": "assistant", "content": "hi"},
    }
    if usage is not None:
        entry["usage"] = usage
    return entry


def _snapshot(
    family: ProviderUsageFamily,
    raw_usage: dict[str, Any],
) -> dict[str, Any]:
    """经真实 ProviderUsageManager 构造 snapshot payload。"""
    return (
        ProviderUsageManager()
        .normalize(
            family=family,
            raw_usage=raw_usage,
        )
        .to_payload()
    )


def test_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert derive_from_session(tmp_path / "nope.jsonl") is None


def test_returns_none_when_no_usage_lines(tmp_path: Path) -> None:
    path = tmp_path / "no-usage.jsonl"
    _write_jsonl(path, [_message_entry(None), _message_entry(None)])
    assert derive_from_session(path) is None


def test_anthropic_snapshot_family_routes_and_preserves_cache(tmp_path: Path) -> None:
    path = tmp_path / "anthropic.jsonl"
    first = _snapshot(
        ProviderUsageFamily.ANTHROPIC_MESSAGES,
        {
            "input_tokens": 999,
            "output_tokens": 999,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    )
    last = _snapshot(
        ProviderUsageFamily.ANTHROPIC_MESSAGES,
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
    )
    _write_jsonl(
        path,
        [
            _message_entry(first),
            _message_entry(last, model="claude-sonnet-4"),
        ],
    )

    result = derive_from_session(path)

    assert isinstance(result, GenericChatAnthropicUsage)
    assert result.input_tokens == 5
    assert result.cache_read_input_tokens == 100
    assert result.cache_creation_input_tokens == 3
    assert result.cache_creation.ephemeral_1h_input_tokens == 3
    assert result.context_usage == 108
    assert result.model == "claude-sonnet-4"


def test_openai_snapshot_family_routes_without_provider_hint(tmp_path: Path) -> None:
    path = tmp_path / "openai.jsonl"
    usage = _snapshot(
        ProviderUsageFamily.OPENAI_CHAT_COMPLETIONS,
        {
            "prompt_tokens": 200,
            "completion_tokens": 50,
            "total_tokens": 250,
            "prompt_tokens_details": {"cached_tokens": 100},
            "completion_tokens_details": {"reasoning_tokens": 10},
        },
    )
    _write_jsonl(path, [_message_entry(usage, model="gpt-4o")])

    result = derive_from_session(path)

    assert isinstance(result, GenericChatOpenAIUsage)
    assert result.last.input_tokens == 200
    assert result.last.cached_input_tokens == 100
    assert result.last.output_tokens == 50
    assert result.last.reasoning_output_tokens == 10
    assert result.last.total_tokens == 250
    assert result.model == "gpt-4o"


def test_unknown_metric_reaches_web_as_none(tmp_path: Path) -> None:
    path = tmp_path / "unknown.jsonl"
    usage = _snapshot(
        ProviderUsageFamily.ANTHROPIC_MESSAGES,
        {"input_tokens": 5, "output_tokens": 2, "cache_read_input_tokens": 9},
    )
    _write_jsonl(path, [_message_entry(usage)])

    result = derive_from_session(path)

    assert isinstance(result, GenericChatAnthropicUsage)
    assert result.cache_read_input_tokens == 9
    assert result.cache_creation_input_tokens is None
    assert result.context_usage is None


def test_skips_invalid_usage_and_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "malformed.jsonl"
    valid = _snapshot(
        ProviderUsageFamily.OPENAI_RESPONSES,
        {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
    )
    with path.open("w", encoding="utf-8") as fh:
        fh.write("INVALID\n")
        fh.write(json.dumps(_message_entry({"provider_kind": "legacy"})) + "\n")
        fh.write(json.dumps(_message_entry(valid)) + "\n")

    result = derive_from_session(path)

    assert isinstance(result, GenericChatOpenAIUsage)
    assert result.last.total_tokens == 10
