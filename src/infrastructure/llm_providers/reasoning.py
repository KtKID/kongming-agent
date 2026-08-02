"""统一 reasoning capability 到 provider payload patch 的适配层。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, cast

from infrastructure.config.model_provider_catalog import (
    ModelCatalogErrorCode,
    ModelProviderCatalogError,
    ReasoningAdapter,
    ReasoningCapability,
)

EffortLevel = Literal["low", "medium", "high", "max"]
ReasoningInput = str
_EFFORT_VALUES: frozenset[str] = frozenset({"low", "medium", "high", "max"})


@dataclass(frozen=True)
class ReasoningConfig:
    """一次请求的 reasoning 选择。"""

    enabled: bool
    effort: ReasoningInput | None


@dataclass(frozen=True)
class ResolvedReasoningPlan:
    """provider 消费的不可变 reasoning 执行计划。"""

    model_name: str
    requested_effort: str | None
    send_reasoning: bool
    normalized_effort: EffortLevel | None
    adapter_name: str
    payload_patch: dict[str, Any]


def _copy_patch(patch: dict[str, Any] | None) -> dict[str, Any]:
    """复制 catalog patch，防止请求处理改写 frozen 配置的嵌套值。"""
    return {} if patch is None else deepcopy(patch)


def _set_patch_path(patch: dict[str, Any], path: str, value: object) -> None:
    """按点分路径写入 payload patch。"""
    parts = [part.strip() for part in path.split(".") if part.strip()]
    if not parts:
        return
    cursor = patch
    for part in parts[:-1]:
        next_value = cursor.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            cursor[part] = next_value
        cursor = cast(dict[str, Any], next_value)
    cursor[parts[-1]] = value


def _unsupported(
    *,
    model_name: str,
    effort: str,
    capability: ReasoningCapability | None,
) -> ModelProviderCatalogError:
    """构造统一的 reasoning_unsupported 错误。"""
    return ModelProviderCatalogError(
        f"reasoning effort {effort!r} is unsupported by model {model_name!r}",
        code=ModelCatalogErrorCode.REASONING_UNSUPPORTED,
        details={
            "model": model_name,
            "effort": effort,
            "adapter": capability.adapter.value if capability is not None else None,
            "supported_efforts": (capability.supported_efforts if capability is not None else ()),
            "supports_disabled": (
                capability.supports_disabled if capability is not None else False
            ),
        },
    )


def _normalize_effort(
    model_name: str,
    requested: str,
    capability: ReasoningCapability,
) -> EffortLevel:
    """应用 alias 与 effort_map，并验证最终档位。"""
    lowered = requested.lower()
    aliased = (capability.effort_aliases or {}).get(lowered, lowered)
    if aliased not in _EFFORT_VALUES:
        raise _unsupported(model_name=model_name, effort=requested, capability=capability)
    canonical = cast(EffortLevel, aliased)
    mapped = (capability.effort_map or {}).get(canonical, canonical)
    if isinstance(mapped, int):
        normalized = canonical
    elif mapped in _EFFORT_VALUES:
        normalized = cast(EffortLevel, mapped)
    else:
        raise _unsupported(model_name=model_name, effort=requested, capability=capability)
    if capability.supported_efforts and normalized not in capability.supported_efforts:
        raise _unsupported(model_name=model_name, effort=requested, capability=capability)
    return normalized


def _enabled_patch(
    effort: EffortLevel,
    capability: ReasoningCapability,
) -> dict[str, Any]:
    """按 adapter 构造启用 reasoning 的 payload patch。"""
    adapter = capability.adapter
    if adapter is ReasoningAdapter.NONE:
        return {}
    if adapter is ReasoningAdapter.DEEPSEEK_OPENAI_THINKING:
        return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}
    if adapter is ReasoningAdapter.DEEPSEEK_ANTHROPIC_THINKING:
        return {"thinking": {"type": "enabled"}, "output_config": {"effort": effort}}
    if adapter is ReasoningAdapter.GLM_THINKING_TOGGLE:
        return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}
    if adapter is ReasoningAdapter.ANTHROPIC_THINKING_TOGGLE:
        return {"thinking": {"type": "enabled"}}
    if adapter is ReasoningAdapter.GLM_THINKING_BUDGET:
        budget = (capability.effort_map or {}).get(effort, 8192)
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}
    if adapter is ReasoningAdapter.ANTHROPIC_COMPATIBLE_REASONING:
        return {"reasoning_effort": effort}
    if adapter is ReasoningAdapter.CONFIGURABLE_PATCH:
        patch = _copy_patch(capability.enabled_patch)
        if capability.effort_path is not None:
            _set_patch_path(patch, capability.effort_path, effort)
        return patch
    return {}


def _disabled_patch(capability: ReasoningCapability) -> dict[str, Any]:
    """按 capability 构造显式关闭 payload。"""
    if capability.adapter is ReasoningAdapter.CONFIGURABLE_PATCH:
        return _copy_patch(capability.disabled_patch)
    return {"thinking": {"type": "disabled"}}


def resolve_reasoning_plan(
    model_name: str,
    config: ReasoningConfig,
    capability: ReasoningCapability | None,
) -> ResolvedReasoningPlan:
    """解析请求级 effort；无效档位在 provider I/O 前返回 typed error。"""
    requested = config.effort
    if not config.enabled or requested is None:
        return ResolvedReasoningPlan(
            model_name=model_name,
            requested_effort=requested,
            send_reasoning=False,
            normalized_effort=None,
            adapter_name="none",
            payload_patch={},
        )
    if capability is None or capability.adapter is ReasoningAdapter.NONE:
        raise _unsupported(model_name=model_name, effort=requested, capability=capability)
    if requested.lower() == "none":
        if not capability.supports_disabled:
            raise _unsupported(model_name=model_name, effort=requested, capability=capability)
        patch = _disabled_patch(capability)
        return ResolvedReasoningPlan(
            model_name=model_name,
            requested_effort=requested,
            send_reasoning=True,
            normalized_effort=None,
            adapter_name=capability.adapter.value,
            payload_patch=patch,
        )

    effort = _normalize_effort(model_name, requested, capability)
    patch = _enabled_patch(effort, capability)
    return ResolvedReasoningPlan(
        model_name=model_name,
        requested_effort=requested,
        send_reasoning=bool(patch),
        normalized_effort=effort,
        adapter_name=capability.adapter.value,
        payload_patch=patch,
    )


__all__ = [
    "EffortLevel",
    "ReasoningConfig",
    "ResolvedReasoningPlan",
    "resolve_reasoning_plan",
]
