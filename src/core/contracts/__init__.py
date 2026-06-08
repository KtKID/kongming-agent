"""跨模块共享协议真源 package。

调用方继续通过 ``from core.contracts import ...`` 消费公共协议；各子模块只承载
协议分组实现，不作为外部调用方的 import 入口。
"""

from __future__ import annotations

from core.contracts.approval import (
    ApprovalAction,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalProvider,
    ApprovalRequest,
)
from core.contracts.event_sink import Event, EventKind, EventSink
from core.contracts.llm_provider import FinishReason, LLMProvider, LLMRequest, LLMResponse
from core.contracts.prompt_assembly import (
    AssembledInput,
    MessageCompactor,
    PromptAssembler,
    PromptDebugSink,
    PromptSource,
)
from core.contracts.session import Session
from core.contracts.streaming import LLMStreamChunk, StreamChunkKind, SupportsLLMStream
from core.contracts.tool_runtime import Tool, ToolContext, ToolLookup, ToolResult

__all__ = [
    "ApprovalAction",
    "ApprovalDecision",
    "ApprovalOutcome",
    "ApprovalProvider",
    "ApprovalRequest",
    "AssembledInput",
    "Event",
    "EventKind",
    "EventSink",
    "FinishReason",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LLMStreamChunk",
    "MessageCompactor",
    "PromptAssembler",
    "PromptDebugSink",
    "PromptSource",
    "Session",
    "StreamChunkKind",
    "SupportsLLMStream",
    "Tool",
    "ToolContext",
    "ToolLookup",
    "ToolResult",
]
