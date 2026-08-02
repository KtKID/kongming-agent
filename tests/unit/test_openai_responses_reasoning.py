"""OpenAI-compatible provider 的 catalog reasoning capability 测试。"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from core.contracts import LLMRequest
from core.message import Message
from infrastructure.config.model_provider_catalog import (
    ReasoningAdapter,
    ReasoningCapability,
)
from infrastructure.config.models import ReasoningEffortInput
from infrastructure.llm_providers.openai_responses import OpenAIResponsesProvider
from tests._helpers.model_runtime import make_model_runtime


def _glm_capability() -> ReasoningCapability:
    """返回 GLM 双字段 reasoning 合同。"""
    return ReasoningCapability(
        adapter=ReasoningAdapter.GLM_THINKING_TOGGLE,
        supported_efforts=("high",),
        default_effort="high",
        supports_disabled=True,
        effort_map={"low": "high", "medium": "high", "high": "high", "max": "high"},
    )


def _deepseek_capability() -> ReasoningCapability:
    """返回 DeepSeek OpenAI-compatible reasoning 合同。"""
    return ReasoningCapability(
        adapter=ReasoningAdapter.DEEPSEEK_OPENAI_THINKING,
        supported_efforts=("high", "max"),
        default_effort="high",
        supports_disabled=True,
        effort_map={"low": "high", "medium": "high", "high": "high", "max": "max"},
    )


def _make_provider(
    name: str,
    *,
    effort: ReasoningEffortInput | None,
    capability: ReasoningCapability | None,
) -> OpenAIResponsesProvider:
    """以 immutable runtime snapshot 构造 provider。"""
    runtime, credential = make_model_runtime(
        name=name,
        reasoning=capability,
        reasoning_effort=effort,
    )
    return OpenAIResponsesProvider(model_config=runtime, credential=credential)


def _payload_with_effort(
    name: str,
    effort: ReasoningEffortInput,
    capability: ReasoningCapability | None,
) -> dict[str, Any]:
    """直接执行 capability 到 payload 的映射。"""
    provider = _make_provider(name, effort=effort, capability=capability)
    payload: dict[str, Any] = {"model": name}
    provider._apply_reasoning_effort(payload, effort)
    return payload


@pytest.mark.unit
@pytest.mark.parametrize("model_name", ["glm-z1", "glm-5.1", "glm-5.2"])
def test_glm_capability_uses_thinking_and_effort(model_name: str) -> None:
    """GLM capability 独立于模型前缀，medium 按 catalog map 归一为 high。"""
    payload = _payload_with_effort(model_name, "medium", _glm_capability())
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"


@pytest.mark.unit
def test_deepseek_capability_preserves_max() -> None:
    payload = _payload_with_effort("deepseek-v4-pro", "max", _deepseek_capability())
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "max"


@pytest.mark.unit
def test_request_effort_overrides_runtime_default() -> None:
    provider = _make_provider("glm-5.2", effort="medium", capability=_glm_capability())
    payload = provider._build_payload(
        LLMRequest(
            model="glm-5.2",
            messages=(Message.user("hi"),),
            reasoning_effort="none",
        )
    )
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload


@pytest.mark.unit
def test_runtime_default_high_is_applied() -> None:
    provider = _make_provider("glm-5.2", effort="high", capability=_glm_capability())
    payload = provider._build_payload(LLMRequest(model="glm-5.2", messages=(Message.user("hi"),)))
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"


@pytest.mark.unit
def test_missing_capability_skips_reasoning_payload() -> None:
    provider = _make_provider("local-model", effort=None, capability=None)
    payload = provider._build_payload(
        LLMRequest(model="local-model", messages=(Message.user("hi"),))
    )
    assert "reasoning_effort" not in payload
    assert "thinking" not in payload


@pytest.mark.unit
def test_build_payload_logs_catalog_reasoning_decision(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _make_provider("glm-5.2", effort="high", capability=_glm_capability())
    request = LLMRequest(model="glm-5.2", messages=(Message.user("hi"),))

    with caplog.at_level(logging.INFO, logger="infrastructure.llm_providers.openai_responses"):
        provider._build_payload(request)

    assert "catalog_source=user" in caplog.text
    assert "preset_id=test-preset" in caplog.text
    assert "switch=enabled" in caplog.text
    assert "requested_effort=high" in caplog.text
    assert "normalized_effort=high" in caplog.text
    assert "payload_keys=['reasoning_effort', 'thinking']" in caplog.text
