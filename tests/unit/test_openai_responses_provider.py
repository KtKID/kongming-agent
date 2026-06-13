"""unit：OpenAIResponsesProvider 响应解析 + provider_metadata 提取。"""

from __future__ import annotations

from typing import Any

import pytest

from infrastructure.config.models import ModelConfig
from infrastructure.llm_providers.openai_responses import OpenAIResponsesProvider


def _make_provider() -> OpenAIResponsesProvider:
    return OpenAIResponsesProvider(
        model_config=ModelConfig(
            name="gpt-4o",
            base_url="http://127.0.0.1:1234/v1",
            api_key="",
        )
    )


def _minimal_response(content: str = "Hello") -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


# ---------------------------------------------------------------------------
# 基础解析
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_response_basic_usage() -> None:
    """基础 usage 字段解析正确。"""
    provider = _make_provider()
    resp = provider._parse_response(_minimal_response("hi"))
    assert resp.message.content == "hi"
    assert resp.finish_reason == "stop"
    assert resp.usage["prompt_tokens"] == 10
    assert resp.usage["completion_tokens"] == 5
    assert resp.usage["total_tokens"] == 15


# ---------------------------------------------------------------------------
# provider_metadata 提取
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_response_metadata_empty_when_no_extras() -> None:
    """无扩展字段时 provider_metadata 为空 dict。"""
    provider = _make_provider()
    resp = provider._parse_response(_minimal_response())
    assert isinstance(resp.provider_metadata, dict)
    assert "reasoning_tokens" not in resp.provider_metadata
    assert "cached_tokens" not in resp.provider_metadata


@pytest.mark.unit
def test_parse_response_metadata_reasoning_tokens() -> None:
    """reasoning_tokens 从 completion_tokens_details 提取。"""
    provider = _make_provider()
    data: dict[str, Any] = {
        "id": "chatcmpl-xyz",
        "model": "o1-mini",
        "choices": [
            {
                "message": {"role": "assistant", "content": "answer"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 200,
            "total_tokens": 250,
            "completion_tokens_details": {"reasoning_tokens": 150},
        },
    }
    resp = provider._parse_response(data)
    assert resp.provider_metadata["reasoning_tokens"] == 150
    assert resp.provider_metadata["id"] == "chatcmpl-xyz"
    assert resp.provider_metadata["model"] == "o1-mini"


@pytest.mark.unit
def test_parse_response_metadata_cached_tokens() -> None:
    """cached_tokens 从 prompt_tokens_details 提取。"""
    provider = _make_provider()
    data: dict[str, Any] = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
            "prompt_tokens_details": {"cached_tokens": 80},
        },
    }
    resp = provider._parse_response(data)
    assert resp.provider_metadata["cached_tokens"] == 80


@pytest.mark.unit
def test_parse_response_metadata_reasoning_content_truncated() -> None:
    """reasoning_content 超过 500 字符时截断，并记录原始长度。"""
    provider = _make_provider()
    long_reasoning = "x" * 600
    data: dict[str, Any] = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "done",
                    "reasoning_content": long_reasoning,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    resp = provider._parse_response(data)
    assert len(resp.provider_metadata["reasoning_content"]) == 500
    assert resp.provider_metadata["reasoning_content_length"] == 600


@pytest.mark.unit
def test_parse_response_metadata_system_fingerprint() -> None:
    """system_fingerprint 字段被收集。"""
    provider = _make_provider()
    data: dict[str, Any] = {
        "system_fingerprint": "fp_abc123",
        "choices": [
            {
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    resp = provider._parse_response(data)
    assert resp.provider_metadata["system_fingerprint"] == "fp_abc123"
