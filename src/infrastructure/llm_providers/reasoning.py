"""Reasoning adapter 层：统一语义 → profile 匹配 → payload patch。

本模块提供 reasoning 参数的结构化解析和翻译，provider 只需消费
:func:`resolve_reasoning_plan` 返回的 :class:`ResolvedReasoningPlan`
来决定是否及如何向 payload 中注入 reasoning 字段。

核心设计：
- 输入始终是**最终请求模型名**（``payload["model"]``），而非 ``model_config.name``
- ``exact`` 匹配优先于 ``prefix`` 匹配
- adapter 负责把统一 effort 枚举翻译成厂商特定 payload patch
- provider 不再维护自己的硬编码前缀白名单
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from infrastructure.config.models import ReasoningProfile

# ---------------------------------------------------------------------------
# 类型定义
# ---------------------------------------------------------------------------

EffortLevel = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class ReasoningConfig:
    """统一的 reasoning 语义描述，由装配层从 model_config 构建。

    Attributes:
        enabled: 是否启用 reasoning。当 effort 不为 None 时为 True。
        effort: 请求的推理深度档位。None 表示不发送 reasoning 参数。
    """

    enabled: bool
    effort: EffortLevel | None


@dataclass(frozen=True)
class ResolvedReasoningPlan:
    """resolver 的输出：给定模型名的最终 reasoning 执行计划。

    Attributes:
        model_name: 最终请求模型名（resolver 的输入）。
        send_reasoning: 是否向 payload 注入 reasoning 字段。
        normalized_effort: 归一化后的 effort 值（可能和输入不同，
            如 effort 不在 supported_efforts 中时降级到最近可用档位）。
        adapter_name: 命中的 adapter 名称。
        payload_patch: 需要合并到请求 payload 的字段 dict。
    """

    model_name: str
    send_reasoning: bool
    normalized_effort: EffortLevel | None
    adapter_name: str
    payload_patch: dict[str, Any]


# ---------------------------------------------------------------------------
# Profile 匹配
# ---------------------------------------------------------------------------


def match_profile(
    model_name: str,
    profiles: dict[str, ReasoningProfile],
) -> ReasoningProfile | None:
    """按模型名从 profiles 中找到最佳匹配。

    匹配规则：
    1. 先遍历所有 ``match=exact`` 的 profile，检查 key 是否等于 model_name
    2. 如无 exact 命中，再遍历 ``match=prefix`` 的 profile，检查 model_name
       是否以 key 开头（大小写不敏感）
    3. 都没命中则返回 None

    **顺序约定**：当多个 ``match=prefix`` 的 key 同时命中同一模型名时，
    返回 profiles dict 中最先出现的那个（YAML 声明顺序即为优先级）。
    Python 3.7+ 保证 dict 保持插入顺序，YAML 加载器同样保持顺序，
    因此"先声明者优先"的行为是确定性的。

    Args:
        model_name: 最终请求模型名（全小写比较）。
        profiles: 从 :attr:`ModelConfig.reasoning_profiles` 取出的配置 map。

    Returns:
        命中的 :class:`ReasoningProfile`，或 None。
    """
    model_lower = model_name.lower()

    # Phase 1: exact match
    for key, profile in profiles.items():
        if profile.match == "exact" and key.lower() == model_lower:
            return profile

    # Phase 2: prefix match
    for key, profile in profiles.items():
        if profile.match == "prefix" and model_lower.startswith(key.lower()):
            return profile

    return None


# ---------------------------------------------------------------------------
# Effort 归一化
# ---------------------------------------------------------------------------


def normalize_effort(
    effort: EffortLevel,
    supported_efforts: list[EffortLevel] | None,
) -> EffortLevel:
    """将请求 effort 归一化到模型支持的档位。

    - ``supported_efforts`` 为 None 或空时，不做限制，直接返回原值
    - effort 在 supported_efforts 中时，直接返回
    - 不在时，按 low < medium < high 顺序取最近的可用档位

    Args:
        effort: 请求的 effort 值。
        supported_efforts: 模型支持的 effort 档位列表。

    Returns:
        归一化后的 effort 值。
    """
    if not supported_efforts:
        return effort
    if effort in supported_efforts:
        return effort

    # 按强度排序
    order: list[EffortLevel] = ["low", "medium", "high"]
    effort_idx = order.index(effort)
    # 取最接近的可用档位
    best: EffortLevel | None = None
    best_dist = len(order)
    for s in supported_efforts:
        dist = abs(order.index(s) - effort_idx)
        if dist < best_dist or (
            dist == best_dist and (best is None or order.index(s) < order.index(best))
        ):
            best = s
            best_dist = dist
    return best if best is not None else effort


# ---------------------------------------------------------------------------
# Adapter 实现
# ---------------------------------------------------------------------------


def _build_patch_none() -> dict[str, Any]:
    """adapter=none：明确不发送 reasoning 参数。"""
    return {}


def _build_patch_glm_thinking_budget(
    effort: EffortLevel,
    profile: ReasoningProfile,
) -> dict[str, Any]:
    """adapter=glm_thinking_budget：智谱 GLM 的 thinking 格式。

    返回 ``{"thinking": {"type": "enabled", "budget_tokens": N}}``。
    """
    effort_map = profile.effort_map or {}
    budget = effort_map.get(effort, 8192)
    return {"thinking": {"type": "enabled", "budget_tokens": budget}}


def _build_patch_anthropic_compatible_reasoning(
    effort: EffortLevel,
) -> dict[str, Any]:
    """adapter=anthropic_compatible_reasoning：Anthropic 兼容 reasoning 格式。

    V1 简化实现：返回 ``{"reasoning_effort": effort}``。
    后续版本可扩展为带 thinking block 配置的更复杂格式。
    """
    return {"reasoning_effort": effort}


# ---------------------------------------------------------------------------
# 主入口：resolve_reasoning_plan
# ---------------------------------------------------------------------------


def resolve_reasoning_plan(
    model_name: str,
    config: ReasoningConfig,
    profiles: dict[str, ReasoningProfile],
) -> ResolvedReasoningPlan:
    """解析给定模型名的 reasoning 执行计划。

    这是 provider 层调用的唯一入口。provider 在 ``_build_payload`` 中
    用 ``payload["model"]`` 调用此函数，拿到 patch 后 merge 到 payload。

    Args:
        model_name: 最终请求模型名（来自 ``payload["model"]``）。
        config: 统一 reasoning 配置。
        profiles: 模型能力 profile map。

    Returns:
        :class:`ResolvedReasoningPlan`，即使不发送 reasoning 也会返回
        有效 plan（``send_reasoning=False``）。
    """
    if not config.enabled or config.effort is None:
        return ResolvedReasoningPlan(
            model_name=model_name,
            send_reasoning=False,
            normalized_effort=None,
            adapter_name="none",
            payload_patch={},
        )

    profile = match_profile(model_name, profiles)
    if profile is None:
        # 没有匹配的 profile → 不发送 reasoning
        return ResolvedReasoningPlan(
            model_name=model_name,
            send_reasoning=False,
            normalized_effort=config.effort,
            adapter_name="none",
            payload_patch={},
        )

    effort = normalize_effort(config.effort, profile.supported_efforts)

    if profile.adapter == "none":
        patch = _build_patch_none()
    elif profile.adapter == "glm_thinking_budget":
        patch = _build_patch_glm_thinking_budget(effort, profile)
    elif profile.adapter == "anthropic_compatible_reasoning":
        patch = _build_patch_anthropic_compatible_reasoning(effort)
    else:
        patch = {}

    return ResolvedReasoningPlan(
        model_name=model_name,
        send_reasoning=bool(patch),
        normalized_effort=effort,
        adapter_name=profile.adapter,
        payload_patch=patch,
    )


__all__ = [
    "EffortLevel",
    "ReasoningConfig",
    "ResolvedReasoningPlan",
    "match_profile",
    "normalize_effort",
    "resolve_reasoning_plan",
]
