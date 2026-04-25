"""unit：reasoning adapter 核心逻辑覆盖。

覆盖范围：
- match_profile：exact / prefix 命中、exact 优先于 prefix、无命中返回 None
- normalize_effort：直接命中、降级到最近档位、supported_efforts=None 直通
- resolve_reasoning_plan：enabled=False / effort=None、无 profile 命中、
  adapter=none / glm_thinking_budget / anthropic_compatible_reasoning
- ReasoningProfile schema 验证：glm_thinking_budget 缺 effort_map、
  anthropic_compatible_reasoning 缺 supported_efforts、合法 none adapter
- request.model != model_config.name 路径（通过 _build_payload 测试）
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config_loader.models import ModelConfig, ReasoningProfile
from core.contracts import LLMRequest
from core.message import Message
from executors.llm.openai_responses import OpenAIResponsesProvider
from executors.llm.reasoning import (
    ReasoningConfig,
    match_profile,
    normalize_effort,
    resolve_reasoning_plan,
)

# ---------------------------------------------------------------------------
# 测试夹具 helpers
# ---------------------------------------------------------------------------


def _glm_profile(match: str = "prefix") -> ReasoningProfile:
    return ReasoningProfile(
        match=match,  # type: ignore[arg-type]
        adapter="glm_thinking_budget",
        effort_map={"low": 1024, "medium": 8192, "high": 32768},
    )


def _anthropic_profile(match: str = "prefix") -> ReasoningProfile:
    return ReasoningProfile(
        match=match,  # type: ignore[arg-type]
        adapter="anthropic_compatible_reasoning",
        supported_efforts=["low", "high"],
    )


def _none_profile(match: str = "exact") -> ReasoningProfile:
    return ReasoningProfile(
        match=match,  # type: ignore[arg-type]
        adapter="none",
    )


# ---------------------------------------------------------------------------
# match_profile
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMatchProfile:
    def test_exact_match(self) -> None:
        """key 与 model_name 完全相等时命中 exact profile。"""
        profiles = {"glm-z1-flash": _glm_profile(match="exact")}
        result = match_profile("glm-z1-flash", profiles)
        assert result is not None
        assert result.adapter == "glm_thinking_budget"

    def test_exact_match_case_insensitive(self) -> None:
        """大小写不敏感 exact 匹配。"""
        profiles = {"GLM-Z1-FLASH": _glm_profile(match="exact")}
        result = match_profile("glm-z1-flash", profiles)
        assert result is not None

    def test_prefix_match(self) -> None:
        """model_name 以 key 开头时命中 prefix profile。"""
        profiles = {"glm-z": _glm_profile(match="prefix")}
        result = match_profile("glm-z1-flash", profiles)
        assert result is not None
        assert result.adapter == "glm_thinking_budget"

    def test_prefix_match_case_insensitive(self) -> None:
        """大小写不敏感 prefix 匹配。"""
        profiles = {"GLM-Z": _glm_profile(match="prefix")}
        result = match_profile("glm-z1-air", profiles)
        assert result is not None

    def test_exact_beats_prefix(self) -> None:
        """同时有 exact 和 prefix 两个 profile 时，exact 优先。"""
        exact_profile = _none_profile(match="exact")
        prefix_profile = _glm_profile(match="prefix")
        profiles = {
            "glm-z1-flash": exact_profile,
            "glm-z": prefix_profile,
        }
        result = match_profile("glm-z1-flash", profiles)
        assert result is not None
        assert result.adapter == "none"  # exact 赢

    def test_no_match_returns_none(self) -> None:
        """无命中返回 None。"""
        profiles = {"o1": _none_profile(match="exact")}
        result = match_profile("llama-3", profiles)
        assert result is None

    def test_empty_profiles_returns_none(self) -> None:
        """空 profiles 返回 None。"""
        result = match_profile("anything", {})
        assert result is None


# ---------------------------------------------------------------------------
# normalize_effort
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNormalizeEffort:
    @pytest.mark.parametrize("effort", ["low", "medium", "high"])
    def test_effort_in_supported_returns_directly(self, effort: str) -> None:
        """effort 在 supported_efforts 中直接返回原值。"""
        supported = ["low", "medium", "high"]
        result = normalize_effort(effort, supported)  # type: ignore[arg-type]
        assert result == effort

    def test_none_supported_efforts_passthrough(self) -> None:
        """supported_efforts=None 时直接返回原值。"""
        result = normalize_effort("medium", None)
        assert result == "medium"

    def test_medium_not_supported_picks_nearest(self) -> None:
        """请求 medium，支持 [low, high]，取距离最近的档位。"""
        result = normalize_effort("medium", ["low", "high"])
        # medium=1, low=0, high=2；距离相同时取较低档位（索引更小）
        assert result in ("low", "high")

    def test_high_not_supported_falls_to_medium(self) -> None:
        """请求 high，支持 [low, medium]，取最近的 medium。"""
        result = normalize_effort("high", ["low", "medium"])
        assert result == "medium"

    def test_low_not_supported_falls_to_medium(self) -> None:
        """请求 low，支持 [medium, high]，取最近的 medium。"""
        result = normalize_effort("low", ["medium", "high"])
        assert result == "medium"

    def test_empty_supported_efforts_passthrough(self) -> None:
        """supported_efforts=[] 时直接返回原值（等同 None）。"""
        result = normalize_effort("medium", [])
        assert result == "medium"


# ---------------------------------------------------------------------------
# resolve_reasoning_plan
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveReasoningPlan:
    def _profiles(self) -> dict[str, ReasoningProfile]:
        return {
            "glm-z": _glm_profile(match="prefix"),
            "claude": _anthropic_profile(match="prefix"),
        }

    def test_disabled_config_returns_no_send(self) -> None:
        """config.enabled=False → send_reasoning=False, payload_patch={}。"""
        config = ReasoningConfig(enabled=False, effort="high")
        plan = resolve_reasoning_plan("glm-z1-flash", config, self._profiles())
        assert plan.send_reasoning is False
        assert plan.payload_patch == {}

    def test_none_effort_returns_no_send(self) -> None:
        """effort=None → send_reasoning=False, payload_patch={}。"""
        config = ReasoningConfig(enabled=True, effort=None)
        plan = resolve_reasoning_plan("glm-z1-flash", config, self._profiles())
        assert plan.send_reasoning is False
        assert plan.payload_patch == {}

    def test_no_matching_profile_returns_no_send(self) -> None:
        """无 profile 命中 → send_reasoning=False, payload_patch={}。"""
        config = ReasoningConfig(enabled=True, effort="medium")
        plan = resolve_reasoning_plan("llama-3.1-8b", config, self._profiles())
        assert plan.send_reasoning is False
        assert plan.payload_patch == {}

    def test_adapter_none_returns_no_send(self) -> None:
        """adapter=none → send_reasoning=False, payload_patch={}。"""
        profiles = {"gemma": _none_profile(match="prefix")}
        config = ReasoningConfig(enabled=True, effort="high")
        plan = resolve_reasoning_plan("gemma-4-8b", config, profiles)
        assert plan.send_reasoning is False
        assert plan.payload_patch == {}

    def test_gemma_4e4b_exact_match_does_not_send_reasoning(self) -> None:
        """spec 要求：gemma-4-e4b-it 用 exact match + adapter=none，稳定不发 reasoning 参数。"""
        profiles = {"gemma-4-e4b-it": ReasoningProfile(match="exact", adapter="none")}
        config = ReasoningConfig(enabled=True, effort="high")
        plan = resolve_reasoning_plan("gemma-4-e4b-it", config, profiles)
        assert plan.send_reasoning is False
        assert plan.payload_patch == {}
        assert plan.adapter_name == "none"

    def test_gemma_4e4b_exact_does_not_match_other_gemma(self) -> None:
        """gemma-4-e4b-it exact profile 不命中其他 gemma 变体，返回 send_reasoning=False（无 profile）。"""
        profiles = {"gemma-4-e4b-it": ReasoningProfile(match="exact", adapter="none")}
        config = ReasoningConfig(enabled=True, effort="high")
        plan = resolve_reasoning_plan("gemma-4-8b", config, profiles)
        # 无命中 → 不发
        assert plan.send_reasoning is False

    @pytest.mark.parametrize(
        "effort, expected_budget",
        [
            ("low", 1024),
            ("medium", 8192),
            ("high", 32768),
        ],
    )
    def test_glm_thinking_budget_adapter(self, effort: str, expected_budget: int) -> None:
        """adapter=glm_thinking_budget → thinking.budget_tokens 来自 effort_map。"""
        config = ReasoningConfig(enabled=True, effort=effort)  # type: ignore[arg-type]
        plan = resolve_reasoning_plan("glm-z1-flash", config, self._profiles())
        assert plan.send_reasoning is True
        assert plan.payload_patch == {
            "thinking": {"type": "enabled", "budget_tokens": expected_budget}
        }
        assert plan.adapter_name == "glm_thinking_budget"

    @pytest.mark.parametrize("effort", ["low", "high"])
    def test_anthropic_compatible_reasoning_adapter(self, effort: str) -> None:
        """adapter=anthropic_compatible_reasoning → reasoning_effort 字段。"""
        config = ReasoningConfig(enabled=True, effort=effort)  # type: ignore[arg-type]
        plan = resolve_reasoning_plan("claude-3-sonnet", config, self._profiles())
        assert plan.send_reasoning is True
        assert plan.payload_patch == {"reasoning_effort": effort}
        assert plan.adapter_name == "anthropic_compatible_reasoning"

    def test_anthropic_effort_normalized_when_not_supported(self) -> None:
        """anthropic profile 只支持 [low, high]，请求 medium 时归一化。"""
        config = ReasoningConfig(enabled=True, effort="medium")
        plan = resolve_reasoning_plan("claude-3-sonnet", config, self._profiles())
        assert plan.send_reasoning is True
        assert plan.normalized_effort in ("low", "high")

    def test_model_name_preserved_in_plan(self) -> None:
        """plan.model_name 等于传入的 model_name。"""
        config = ReasoningConfig(enabled=True, effort="high")
        plan = resolve_reasoning_plan("glm-z1-air", config, self._profiles())
        assert plan.model_name == "glm-z1-air"


# ---------------------------------------------------------------------------
# ReasoningProfile schema 验证
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReasoningProfileValidation:
    def test_glm_thinking_budget_missing_effort_map_raises(self) -> None:
        """glm_thinking_budget 缺少 effort_map → ValidationError。"""
        with pytest.raises(ValidationError, match="effort_map"):
            ReasoningProfile(adapter="glm_thinking_budget")

    def test_anthropic_compatible_missing_supported_efforts_raises(self) -> None:
        """anthropic_compatible_reasoning 缺少 supported_efforts → ValidationError。"""
        with pytest.raises(ValidationError, match="supported_efforts"):
            ReasoningProfile(adapter="anthropic_compatible_reasoning")

    def test_none_adapter_valid_without_extra_fields(self) -> None:
        """adapter=none 不需要 effort_map 或 supported_efforts，应合法构造。"""
        profile = ReasoningProfile(adapter="none")
        assert profile.adapter == "none"
        assert profile.effort_map is None
        assert profile.supported_efforts is None

    def test_glm_profile_valid_with_effort_map(self) -> None:
        """完整 glm_thinking_budget profile 合法。"""
        profile = ReasoningProfile(
            adapter="glm_thinking_budget",
            effort_map={"low": 512, "medium": 4096, "high": 16384},
        )
        assert profile.effort_map["medium"] == 4096

    def test_anthropic_profile_valid_with_supported_efforts(self) -> None:
        """完整 anthropic_compatible_reasoning profile 合法。"""
        profile = ReasoningProfile(
            adapter="anthropic_compatible_reasoning",
            supported_efforts=["low", "high"],
        )
        assert "low" in profile.supported_efforts  # type: ignore[operator]


# ---------------------------------------------------------------------------
# request.model != model_config.name 路径
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRequestModelDiffersFromConfigName:
    """验证 _build_payload 以 payload["model"]（request.model）而非 model_config.name 匹配 profile。"""

    def _make_provider_with_glm_prefix_profile(
        self, config_model_name: str
    ) -> OpenAIResponsesProvider:
        return OpenAIResponsesProvider(
            model_config=ModelConfig(
                name=config_model_name,
                base_url="http://127.0.0.1:1234/v1",
                api_key="",
                reasoning_effort="medium",
                reasoning_profiles={
                    "glm-z": ReasoningProfile(
                        match="prefix",
                        adapter="glm_thinking_budget",
                        effort_map={"low": 1024, "medium": 8192, "high": 32768},
                    )
                },
            )
        )

    def test_request_model_used_for_profile_matching(self) -> None:
        """request.model='glm-z1-air' 应命中 glm-z prefix profile，即使 config.name 不同。"""
        provider = self._make_provider_with_glm_prefix_profile("glm-z1-flash")
        request = LLMRequest(
            model="glm-z1-air",  # 不同于 config.name
            messages=(Message.user("hi"),),
        )
        payload = provider._build_payload(request)
        # 应根据 request.model="glm-z1-air" 匹配到 glm-z prefix → thinking patch
        assert "thinking" in payload
        assert payload["thinking"]["type"] == "enabled"
        assert isinstance(payload["thinking"]["budget_tokens"], int)

    def test_config_name_not_used_when_request_model_provided(self) -> None:
        """当 request.model 与 config.name 不同时，不能按 config.name 匹配。"""
        # config.name 是 glm-z1-flash（命中 prefix），但 request.model 是 llama-3（不命中）
        provider = self._make_provider_with_glm_prefix_profile("glm-z1-flash")
        request = LLMRequest(
            model="llama-3-8b",  # 不命中 glm-z prefix
            messages=(Message.user("hi"),),
        )
        payload = provider._build_payload(request)
        # llama-3-8b 不命中任何 profile → 不注入 thinking
        assert "thinking" not in payload

    def test_payload_model_key_reflects_request_model(self) -> None:
        """payload['model'] 应等于 request.model，而非 model_config.name。"""
        provider = self._make_provider_with_glm_prefix_profile("glm-z1-flash")
        request = LLMRequest(
            model="glm-z1-air",
            messages=(Message.user("hi"),),
        )
        payload = provider._build_payload(request)
        assert payload["model"] == "glm-z1-air"
