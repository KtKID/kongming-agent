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
    InteractiveApprovalRebinder,
)
from core.contracts.event_sink import Event, EventKind, EventSink
from core.contracts.llm_provider import (
    FinishReason,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMToolCallContract,
    LLMToolCallContractMode,
    LLMToolCallViolationKind,
    ReasoningEffort,
    ReasoningLevel,
)
from core.contracts.media import (
    IMAGE_EXT_BY_MIME,
    AssetBytesReader,
    AttachmentKind,
    AttachmentStatus,
    ImageMediaPart,
    MediaPart,
    build_media_part_from_metadata,
    collect_media_parts_from_messages,
)
from core.contracts.model_catalog import ModelCatalogResolver
from core.contracts.prompt_assembly import (
    AssembledInput,
    MessageCompactor,
    PromptAssembler,
    PromptDebugSink,
    PromptSource,
)
from core.contracts.provider_usage import (
    ProviderUsageAnomaly,
    ProviderUsageAnomalyCode,
    ProviderUsageCompleteness,
    ProviderUsageFamily,
    ProviderUsageMetric,
    ProviderUsageMetricName,
    ProviderUsageMetricOrigin,
    ProviderUsageScope,
    ProviderUsageSnapshot,
    aggregate_provider_usage_snapshots,
)
from core.contracts.run_execution import RunExecutionOverrides
from core.contracts.session import Session
from core.contracts.steer import SteerRequest
from core.contracts.streaming import LLMStreamChunk, StreamChunkKind, SupportsLLMStream
from core.contracts.tool_runtime import (
    PreparedToolCall,
    Tool,
    ToolCallPreparer,
    ToolContext,
    ToolExecutionScope,
    ToolLookup,
    ToolResult,
)

__all__ = [
    "ApprovalAction",
    "ApprovalDecision",
    "ApprovalOutcome",
    "ApprovalProvider",
    "ApprovalRequest",
    "InteractiveApprovalRebinder",
    "AssembledInput",
    "AssetBytesReader",
    "AttachmentKind",
    "AttachmentStatus",
    "Event",
    "EventKind",
    "EventSink",
    "FinishReason",
    "IMAGE_EXT_BY_MIME",
    "ImageMediaPart",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LLMStreamChunk",
    "LLMToolCallContract",
    "LLMToolCallContractMode",
    "LLMToolCallViolationKind",
    "MediaPart",
    "MessageCompactor",
    "ModelCatalogResolver",
    "PromptAssembler",
    "PromptDebugSink",
    "PromptSource",
    "ProviderUsageAnomaly",
    "ProviderUsageAnomalyCode",
    "ProviderUsageCompleteness",
    "ProviderUsageFamily",
    "ProviderUsageMetric",
    "ProviderUsageMetricName",
    "ProviderUsageMetricOrigin",
    "ProviderUsageScope",
    "ProviderUsageSnapshot",
    "aggregate_provider_usage_snapshots",
    "PreparedToolCall",
    "ReasoningEffort",
    "ReasoningLevel",
    "RunExecutionOverrides",
    "Session",
    "SteerRequest",
    "StreamChunkKind",
    "SupportsLLMStream",
    "Tool",
    "ToolCallPreparer",
    "ToolContext",
    "ToolExecutionScope",
    "ToolLookup",
    "ToolResult",
    "build_media_part_from_metadata",
    "collect_media_parts_from_messages",
]
