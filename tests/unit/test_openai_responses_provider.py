"""unit：OpenAIResponsesProvider 响应解析 + provider_metadata 提取。"""

from __future__ import annotations

from typing import Any

import pytest

from core.contracts import LLMRequest
from core.message import Message
from infrastructure.llm_providers.openai_responses import OpenAIResponsesProvider
from tests._helpers.model_runtime import make_model_runtime


def _make_provider(**model_overrides: object) -> OpenAIResponsesProvider:
    model_config = {
        "name": "gpt-4o",
        "base_url": "http://127.0.0.1:1234/v1",
        **model_overrides,
    }
    runtime, credential = make_model_runtime(**model_config)  # type: ignore[arg-type]
    return OpenAIResponsesProvider(
        model_config=runtime,
        credential=credential,
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


@pytest.mark.unit
def test_build_headers_defaults_to_bearer_for_remote_openai_compatible() -> None:
    provider = _make_provider(
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key="glm-key",
    )

    headers = provider._build_headers()

    assert headers["Authorization"] == "Bearer glm-key"
    assert "x-api-key" not in headers


@pytest.mark.unit
def test_build_headers_respects_x_api_key_override() -> None:
    provider = _make_provider(
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key="custom-key",
        api_key_header="x-api-key",
    )

    headers = provider._build_headers()

    assert headers["x-api-key"] == "custom-key"
    assert "Authorization" not in headers


@pytest.mark.unit
def test_build_payload_uses_max_completion_tokens_for_minimax_openai_runtime() -> None:
    """MiniMax OpenAI 兼容路径使用 max_completion_tokens，输入为 runtime metadata，输出为 payload 字段断言。"""
    provider = _make_provider(max_tokens=4096, temperature=0.7)
    request = LLMRequest(
        model="minimax-m3",
        messages=(Message.user("hi"),),
        max_tokens=131072,
        temperature=0.2,
        metadata={
            "token_parameter_name": "max_completion_tokens",
            "provider_extra": {"top_p": 0.95},
        },
    )

    payload = provider._build_payload(request)

    assert payload["max_completion_tokens"] == 131072
    assert "max_tokens" not in payload
    assert payload["temperature"] == 0.2
    assert payload["top_p"] == 0.95


@pytest.mark.unit
def test_build_payload_keeps_max_tokens_for_glm_runtime() -> None:
    """GLM OpenAI 兼容路径使用 max_tokens，输入为 runtime metadata，输出为 payload 字段断言。"""
    provider = _make_provider(max_tokens=4096)
    request = LLMRequest(
        model="glm-4.5",
        messages=(Message.user("hi"),),
        max_tokens=131072,
        metadata={"token_parameter_name": "max_tokens"},
    )

    payload = provider._build_payload(request)

    assert payload["max_tokens"] == 131072
    assert "max_completion_tokens" not in payload


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
