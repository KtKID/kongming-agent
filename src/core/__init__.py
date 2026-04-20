"""kongming-agent core runtime.

这里只放宿主无关的 agent 运行语义。core 是跨模块共享语言的唯一真源：

- 跨模块协议定义集中在 :mod:`core.contracts`。
- 核心数据结构（消息、运行状态、结果、错误）分别放在
  :mod:`core.message` / :mod:`core.run_state` / :mod:`core.result` / :mod:`core.errors`。
- :mod:`core.runner` 是唯一 run loop 真身。
- :mod:`core.session` 提供默认 :class:`InMemorySession` 实现。

core 不允许 import 任何 sibling 模块（tools / context / executors / host / cli / safety /
observability）。其他模块可以自由 import core，但必须消费这里定义的协议和模型，
而不是在自己的目录里重定义一份同名接口。
"""

from __future__ import annotations

from core.agent_spec import AgentSpec
from core.contracts import (
    ApprovalDecision,
    ApprovalProvider,
    ApprovalRequest,
    Event,
    EventSink,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Session,
    Tool,
    ToolContext,
    ToolLookup,
    ToolResult,
)
from core.errors import (
    AgentError,
    ApprovalRejected,
    CapabilityDenied,
    ConfigError,
    MaxTurnsExceededError,
    PermissionDenied,
    ProviderError,
    ToolError,
)
from core.lifecycle import LifecycleHook
from core.message import Message, ToolCall
from core.result import Result
from core.run_state import RunState, RunStatus
from core.runner import Runner
from core.session import InMemorySession

__all__ = [
    # agent spec
    "AgentSpec",
    # contracts
    "ApprovalDecision",
    "ApprovalProvider",
    "ApprovalRequest",
    "Event",
    "EventSink",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "Session",
    "Tool",
    "ToolContext",
    "ToolLookup",
    "ToolResult",
    # errors
    "AgentError",
    "ApprovalRejected",
    "CapabilityDenied",
    "ConfigError",
    "MaxTurnsExceededError",
    "PermissionDenied",
    "ProviderError",
    "ToolError",
    # lifecycle
    "LifecycleHook",
    # message
    "Message",
    "ToolCall",
    # result
    "Result",
    # run state
    "RunState",
    "RunStatus",
    # runner
    "Runner",
    # session
    "InMemorySession",
]
