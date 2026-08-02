"""catalog reasoning capability 到 provider 中立计划的单元测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from infrastructure.config.model_provider_catalog import (
    ModelProviderCatalogError,
    ReasoningAdapter,
    ReasoningCapability,
)
from infrastructure.llm_providers.reasoning import ReasoningConfig, resolve_reasoning_plan


def _glm_capability() -> ReasoningCapability:
    """返回 GLM-5.2 三档与显式关闭合同。"""
    return ReasoningCapability(
        adapter=ReasoningAdapter.GLM_THINKING_TOGGLE,
        supported_efforts=("low", "medium", "high"),
        default_effort="high",
        supports_disabled=True,
    )


@pytest.mark.unit
def test_disabled_config_produces_empty_plan() -> None:
    plan = resolve_reasoning_plan(
        "glm-5.2",
        ReasoningConfig(enabled=False, effort="high"),
        _glm_capability(),
    )
    assert plan.send_reasoning is False
    assert plan.payload_patch == {}


@pytest.mark.unit
def test_none_effort_produces_explicit_disabled_patch() -> None:
    plan = resolve_reasoning_plan(
        "glm-5.2",
        ReasoningConfig(enabled=True, effort="none"),
        _glm_capability(),
    )
    assert plan.send_reasoning is True
    assert plan.normalized_effort is None
    assert plan.payload_patch == {"thinking": {"type": "disabled"}}


@pytest.mark.unit
def test_glm_high_produces_toggle_and_effort() -> None:
    plan = resolve_reasoning_plan(
        "glm-5.2",
        ReasoningConfig(enabled=True, effort="high"),
        _glm_capability(),
    )
    assert plan.normalized_effort == "high"
    assert plan.payload_patch == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }


@pytest.mark.unit
def test_effort_alias_and_map_are_applied() -> None:
    capability = ReasoningCapability(
        adapter=ReasoningAdapter.DEEPSEEK_ANTHROPIC_THINKING,
        supported_efforts=("high", "max"),
        supports_disabled=True,
        effort_aliases={"xhigh": "max"},
        effort_map={"low": "high", "medium": "high", "high": "high", "max": "max"},
    )
    plan = resolve_reasoning_plan(
        "deepseek-v4-pro",
        ReasoningConfig(enabled=True, effort="xhigh"),
        capability,
    )
    assert plan.normalized_effort == "max"
    assert plan.payload_patch == {
        "thinking": {"type": "enabled"},
        "output_config": {"effort": "max"},
    }


@pytest.mark.unit
def test_configurable_patch_is_copied_and_receives_effort_path() -> None:
    capability = ReasoningCapability(
        adapter=ReasoningAdapter.CONFIGURABLE_PATCH,
        supported_efforts=("high",),
        supports_disabled=True,
        enabled_patch={"thinking": {"type": "adaptive"}},
        disabled_patch={"thinking": {"type": "disabled"}},
        effort_path="output_config.effort",
    )
    plan = resolve_reasoning_plan(
        "MiniMax-M3",
        ReasoningConfig(enabled=True, effort="high"),
        capability,
    )
    assert plan.payload_patch == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
    }
    assert capability.enabled_patch == {"thinking": {"type": "adaptive"}}


@pytest.mark.unit
@pytest.mark.parametrize("effort", ["max", "unknown"])
def test_unsupported_effort_returns_typed_error(effort: str) -> None:
    with pytest.raises(ModelProviderCatalogError) as exc_info:
        resolve_reasoning_plan(
            "glm-5.2",
            ReasoningConfig(enabled=True, effort=effort),
            _glm_capability(),
        )
    assert exc_info.value.code.value == "reasoning_unsupported"


@pytest.mark.unit
def test_missing_capability_rejects_explicit_effort() -> None:
    with pytest.raises(ModelProviderCatalogError) as exc_info:
        resolve_reasoning_plan(
            "local-model",
            ReasoningConfig(enabled=True, effort="high"),
            None,
        )
    assert exc_info.value.details["adapter"] is None


@pytest.mark.unit
def test_default_none_requires_disable_support() -> None:
    with pytest.raises(ValidationError, match="supports_disabled"):
        ReasoningCapability(
            adapter=ReasoningAdapter.GLM_THINKING_TOGGLE,
            default_effort="none",
        )


@pytest.mark.unit
def test_configurable_patch_requires_a_patch() -> None:
    with pytest.raises(ValidationError, match="configurable_patch"):
        ReasoningCapability(adapter=ReasoningAdapter.CONFIGURABLE_PATCH)
