"""Agent 静态规格。

描述"这个 agent 是什么"——名称、system prompt、默认模型、启用的工具名、
默认的 turn 上限。运行态（当前 turn、当前消息、运行状态）不放这里，
那是 :class:`core.run_state.RunState` 的事。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from core.contracts import ReasoningEffort

_REASONING_EFFORT_VALUES: frozenset[str] = frozenset({"none", "low", "medium", "high", "max"})


def coerce_reasoning_effort(value: str | None) -> ReasoningEffort | None:
    """把外部字符串收窄为 AgentSpec 支持的 reasoning effort。"""
    if value is None:
        return None
    if value not in _REASONING_EFFORT_VALUES:
        return None
    return cast(ReasoningEffort, value)


@dataclass(frozen=True)
class AgentSpec:
    """Agent 静态规格描述。

    Attributes:
        name: agent 名。装配层用它区分多 agent，但 v1-mini 只有单 agent。
        instructions: system prompt 文本；runner 在组装 LLMRequest 时把它放到最前面。
        default_model: 默认模型名。最终调用时 provider 可能会被配置层覆写。
        tool_names: 从工具查找面启用的工具名白名单。空列表表示不启用任何工具。
        max_turns: 默认最大轮数。调用方可以在 runner.run() 时覆盖。
        metadata: 预留给装配层或 infrastructure.tracing 的自由字段，core 不解释。
    """

    name: str
    instructions: str
    default_model: str
    tool_names: tuple[str, ...] = ()
    max_turns: int = 10
    metadata: dict[str, str] = field(default_factory=dict)
    reasoning_effort: ReasoningEffort | None = None

    def __post_init__(self) -> None:
        if self.max_turns <= 0:
            raise ValueError("AgentSpec.max_turns must be positive")
        if not self.name:
            raise ValueError("AgentSpec.name must not be empty")
        if not self.default_model:
            raise ValueError("AgentSpec.default_model must not be empty")


__all__ = ["AgentSpec", "coerce_reasoning_effort"]
