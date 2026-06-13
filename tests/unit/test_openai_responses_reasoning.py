"""unit：OpenAIResponsesProvider._apply_reasoning_effort 配置驱动路径。

验证 reasoning_effort 经 reasoning_profiles 匹配后正确注入 payload：
- glm_thinking_budget adapter：thinking.budget_tokens
- 无 profile 命中（profiles 为空或模型不在白名单）：skip
- reasoning_effort=None：不进入 payload
"""

from __future__ import annotations

from typing import Any

import pytest

from core.contracts import LLMRequest
from core.message import Message
from infrastructure.config.models import ModelConfig, ReasoningProfile
from infrastructure.llm_providers.openai_responses import OpenAIResponsesProvider


def _make_provider(
    name: str,
    effort: str | None = "medium",
    reasoning_profiles: dict | None = None,
) -> OpenAIResponsesProvider:
    return OpenAIResponsesProvider(
        model_config=ModelConfig(
            name=name,
            base_url="http://127.0.0.1:1234/v1",
            api_key="",
            reasoning_effort=effort,  # type: ignore[arg-type]
            reasoning_profiles=reasoning_profiles or {},
        )
    )


def _glm_profiles() -> dict[str, ReasoningProfile]:
    """GLM 深度思考系列的标准 profile 配置。"""
    return {
        "glm-z": ReasoningProfile(
            match="prefix",
            adapter="glm_thinking_budget",
            effort_map={"low": 1024, "medium": 8192, "high": 32768},
        ),
        "glm-zero": ReasoningProfile(
            match="prefix",
            adapter="glm_thinking_budget",
            effort_map={"low": 1024, "medium": 8192, "high": 32768},
        ),
        "glm-5": ReasoningProfile(
            match="prefix",
            adapter="glm_thinking_budget",
            effort_map={"low": 1024, "medium": 8192, "high": 32768},
        ),
    }


def _payload_with_effort(
    name: str,
    effort: str,
    profiles: dict | None = None,
) -> dict[str, Any]:
    provider = _make_provider(name, effort, profiles)
    payload: dict[str, Any] = {"model": name}
    provider._apply_reasoning_effort(payload, effort)
    return payload


# ---------------------------------------------------------------------------
# GLM 系列（profile 驱动）
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "model_name",
    ["glm-z1", "glm-z1-flash", "glm-zero-preview", "glm-5", "glm-5.1"],
)
def test_glm_thinking_series_uses_thinking_field(model_name: str) -> None:
    """GLM 深度思考系列经 profile 匹配 → thinking.type=enabled + budget_tokens。"""
    payload = _payload_with_effort(model_name, "medium", _glm_profiles())
    assert "reasoning_effort" not in payload
    assert payload.get("thinking", {}).get("type") == "enabled"
    assert isinstance(payload.get("thinking", {}).get("budget_tokens"), int)


@pytest.mark.unit
def test_glm_low_effort_has_smaller_budget() -> None:
    """low < medium < high 的 budget_tokens 顺序。"""
    profiles = _glm_profiles()
    low = _payload_with_effort("glm-z1", "low", profiles)["thinking"]["budget_tokens"]
    med = _payload_with_effort("glm-z1", "medium", profiles)["thinking"]["budget_tokens"]
    high = _payload_with_effort("glm-z1", "high", profiles)["thinking"]["budget_tokens"]
    assert low < med < high


@pytest.mark.unit
def test_glm4_standard_not_in_profiles_skips_thinking() -> None:
    """普通 GLM-4 系列不在 profile 白名单内 → skip。"""
    profiles = _glm_profiles()  # 只含 glm-z/glm-zero/glm-5 前缀
    for model_name in ["glm-4-flash", "glm-4-long", "glm-4"]:
        payload = _payload_with_effort(model_name, "high", profiles)
        assert "reasoning_effort" not in payload, model_name
        assert "thinking" not in payload, model_name


# ---------------------------------------------------------------------------
# 无 profile / 不命中 → skip
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("model_name", ["gemma-4-e4b-it", "llama-3.1-8b", "mistral-7b"])
def test_no_profiles_configured_skips_reasoning(model_name: str) -> None:
    """reasoning_profiles 未配置时，任何模型都跳过 reasoning 注入。"""
    payload = _payload_with_effort(model_name, "high", {})
    assert "reasoning_effort" not in payload
    assert "thinking" not in payload


@pytest.mark.unit
def test_model_not_in_profiles_skips_reasoning() -> None:
    """profiles 有值但模型名不命中任何 profile → skip。"""
    payload = _payload_with_effort("llama-3.1-8b", "medium", _glm_profiles())
    assert "reasoning_effort" not in payload
    assert "thinking" not in payload


# ---------------------------------------------------------------------------
# reasoning_effort=None → 不进入 payload
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_none_reasoning_effort_not_applied() -> None:
    """reasoning_effort=None 时 _build_payload 不调用 _apply_reasoning_effort，payload 干净。"""
    provider = _make_provider("glm-z1", effort=None, reasoning_profiles=_glm_profiles())
    request = LLMRequest(model="glm-z1", messages=(Message.user("hi"),))
    payload = provider._build_payload(request)
    assert "reasoning_effort" not in payload
    assert "thinking" not in payload
