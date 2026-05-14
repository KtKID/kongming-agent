"""通道 DTO 真源（v2）。

⚠️ **架构边界**：本模块是 ``web.usage_token_v2`` 包私有，外部禁止 import
（``.importlinter`` Contract 9 强制）；只能通过 ``UsageTokenManager`` 接收
DTO 实例，**不可主动构造**。

4 个 channel-specific DTO + 嵌套子结构：

- ``ClaudeUsage``（claude_code / generic_chat-anthropic）
- ``CodexUsage``（codex / generic_chat-openai 的 codex 来源场景）
- ``GenericChatAnthropicUsage``（我们 LLMProvider 走 Anthropic 系）
- ``GenericChatOpenAIUsage``（我们 LLMProvider 走 OpenAI 系）

每个 DTO 自带 ``provider: Literal[...]`` 字段作 **discriminator**，前端按它
分支 narrowing。

设计依据：[`docs/usage-token-v2/04-data-and-state.md`](../../../docs/usage-token-v2/04-data-and-state.md)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# =============================================================================
# Claude 系（claude_code / generic_chat-anthropic）
# =============================================================================


class ClaudeCacheCreation(BaseModel):
    """Anthropic prompt cache TTL 细分（cache_creation 子结构）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ephemeral_1h_input_tokens: int = Field(default=0, ge=0)
    """写入 1 小时 TTL cache 的 input tokens（1h cache 单价比 5m 贵，保留更久）。"""

    ephemeral_5m_input_tokens: int = Field(default=0, ge=0)
    """写入 5 分钟 TTL cache 的 input tokens（默认档）。"""


class ClaudeUsage(BaseModel):
    """Claude 系 token 用量。

    映射 SDK ``message.usage`` 1:1。**取最后一条** assistant message 的 usage，
    不做累加（Anthropic 的 input_tokens 字段是"纯新增"语义，最后一条 input +
    cache_read + cache_creation = 当前 context 占用）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["claude"] = "claude"
    """前端用此字段做 narrowing。"""

    input_tokens: int = Field(default=0, ge=0)
    """**纯新增** input tokens（不含 cache 部分）。Anthropic 语义。"""

    output_tokens: int = Field(default=0, ge=0)
    """这次 API call 的 output（含 thinking 内部 reasoning）。"""

    cache_read_input_tokens: int = Field(default=0, ge=0)
    """命中 prompt cache 的 input tokens（标准 input 价的 10%）。"""

    cache_creation_input_tokens: int = Field(default=0, ge=0)
    """新写入 cache 的 input tokens（标准 input 价的 125%）。"""

    cache_creation: ClaudeCacheCreation = Field(default_factory=ClaudeCacheCreation)
    """cache_creation_input_tokens 按 TTL 细分（1h vs 5m，价格不同）。"""

    context_usage: int = Field(default=0, ge=0)
    """派生：当前 context 占用 = input + cache_read + cache_creation。"""

    model: str = ""
    """模型名（最后一条 assistant message 的 message.model）。"""

    context_window: int = Field(default=0, ge=0)
    """模型 context 上限（派生器查 ``_model_context_table`` 填）。0=未知。"""


# =============================================================================
# Codex / OpenAI 系（codex 通道独有结构）
# =============================================================================


class CodexTokenBreakdown(BaseModel):
    """Codex token 用量 5 字段（OpenAI 语义）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    """**总** input tokens（**含** cached_input 子集，OpenAI 语义）。"""

    cached_input_tokens: int = Field(default=0, ge=0)
    """命中 cache 的 input tokens（**是 input_tokens 的子集，不要相加**）。"""

    output_tokens: int = Field(default=0, ge=0)
    """**总** output（**含** reasoning_output 子集，OpenAI 语义）。"""

    reasoning_output_tokens: int = Field(default=0, ge=0)
    """o1/o3 内部推理 tokens（**是 output_tokens 的子集**）。"""

    total_tokens: int = Field(default=0, ge=0)
    """codex 自带的总和（≈ input + output）。"""


class CodexRateLimitWindow(BaseModel):
    """Codex 速率限制单窗口（5h 或 7d）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    used_percent: float = Field(default=0.0, ge=0.0)
    """该窗口已用百分比（0-100）。"""

    window_minutes: int = Field(default=0, ge=0)
    """窗口长度（分钟）。"""

    resets_at: int = Field(default=0, ge=0)
    """重置时间戳（Unix epoch 秒）。"""


class CodexRateLimits(BaseModel):
    """Codex 速率限制（两个窗口 + 计划等级）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    primary: CodexRateLimitWindow
    """主要窗口（一般是 5h / 300min）。"""

    secondary: CodexRateLimitWindow
    """次要窗口（一般是 7d / 10080min）。"""

    plan_type: str = ""
    """计划类型 plus / team / pro / enterprise 等。"""


class CodexUsage(BaseModel):
    """Codex（OpenAI 系）token 用量。

    映射 codex rollout ``event_msg.payload(token_count).info`` 1:1。
    codex **已自带累加**——``total_token_usage`` 是 thread 级累计，
    ``last_token_usage`` 是最后一次 API call，直接用，**不再自己加**。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["openai"] = "openai"

    total: CodexTokenBreakdown
    """thread 级累计（codex 自己算好的）。"""

    last: CodexTokenBreakdown
    """最近一次 API call 的用量。"""

    model_context_window: int = Field(default=0, ge=0)
    """codex 报的模型 context 上限。"""

    rate_limits: CodexRateLimits | None = None
    """codex 速率限制状态（5h 窗口 + 7d 窗口）。None=codex 没报（本地模型等）。"""


# =============================================================================
# generic_chat 系（我们自家 LLMProvider 走 anthropic / openai）
# =============================================================================


class GenericChatAnthropicUsage(BaseModel):
    """generic_chat 通道（底层 LLMProvider 是 Anthropic 系）的 token 用量。

    字段跟 ClaudeUsage 平行（不复用是为了 backend 派生路径独立 + 未来字段分叉余地）。
    discriminator ``provider="claude"`` 跟 ClaudeUsage 同——前端**复用** StatusLineClaude
    组件渲染。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["claude"] = "claude"

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cache_creation: ClaudeCacheCreation = Field(default_factory=ClaudeCacheCreation)

    context_usage: int = Field(default=0, ge=0)
    model: str = ""
    context_window: int = Field(default=0, ge=0)


class GenericChatOpenAIUsage(BaseModel):
    """generic_chat 通道（底层 LLMProvider 是 OpenAI 系）的 token 用量。

    形态比 CodexUsage 简化：没有 codex 自带的 rate_limits；total 由派生器自己算
    （这里仍声明字段为 ``last``，对外契约一致）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["openai"] = "openai"

    last: CodexTokenBreakdown
    """最近一次 API call 的用量。"""

    model: str = ""
    context_window: int = Field(default=0, ge=0)
    # 没有 total / rate_limits（generic_chat-openai 不像 codex 自带累加）


# =============================================================================
# Union（manager 公共 API 返回类型）
# =============================================================================


ThreadUsage = ClaudeUsage | CodexUsage | GenericChatAnthropicUsage | GenericChatOpenAIUsage
"""manager.get_thread_usage 返回类型。前端按 provider discriminator 分支。"""


__all__ = [
    "ClaudeCacheCreation",
    "ClaudeUsage",
    "CodexRateLimitWindow",
    "CodexRateLimits",
    "CodexTokenBreakdown",
    "CodexUsage",
    "GenericChatAnthropicUsage",
    "GenericChatOpenAIUsage",
    "ThreadUsage",
]
