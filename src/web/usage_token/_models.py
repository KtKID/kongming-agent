"""TokenUsage 内部数据结构（manager 私有）+ 对外公共 DTO。

⚠️ **架构边界**：本模块的 `TokenUsage` / `_AnthropicTokenUsage` / `_OpenAITokenUsage`
是 `web.usage_token` 包的**私有内部类型**——外部模块禁止直接 import，由
`.importlinter` Contract `usage-token-encapsulation` 机器强制（运行时违规会被
lint-imports 拒绝）。

外部模块要拿 token 数据只能：

- ``from web.usage_token import UsageTokenManager`` → 调 manager 方法
- ``from web.usage_token import UsageTokenSnapshot, ThreadUsageSummary, UsageFrame``
  → 接收 manager 返回的 DTO，**但不可主动构造 TokenUsage 实例**

设计依据：[`docs/usage-token/04-data-and-state.md`](../../../docs/usage-token/04-data-and-state.md)
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# =============================================================================
# 私有：TokenUsage discriminated union
# =============================================================================


class _AnthropicTokenUsage(BaseModel):
    """Anthropic 系（claude_code 或 generic_chat-anthropic 通道）一轮 token 用量。

    SDK 原生字段，**不做 lossy 归一化**。

    ⚠️ Anthropic 字段语义关键差异（跟 OpenAI 不同）：

    - ``input_tokens`` **不含** cache 部分（cache_read / cache_creation 独立计数）
    - ``context_usage`` 派生公式 = ``input + cache_read + cache_creation``（manager 内部算）
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: Literal["anthropic"] = "anthropic"
    """通道标识，恒为 ``"anthropic"``；discriminated union 的 tag 字段。"""

    input_tokens: int = Field(default=0, ge=0)
    """本轮纯新输入 token 数（不含 cache_read / cache_creation 部分）。"""

    cache_read_input_tokens: int = Field(default=0, ge=0)
    """本轮命中 prompt cache 的输入 token 数（独立计数，不在 ``input_tokens`` 里）。

    Anthropic prompt caching 命中折扣计费。"""

    cache_creation_input_tokens: int = Field(default=0, ge=0)
    """本轮首次写 prompt cache 的输入 token 数（独立计数，不在 ``input_tokens`` 里）。

    Anthropic prompt cache 首次写入比正常 input 贵（约 125%）。"""

    output_tokens: int = Field(default=0, ge=0)
    """本轮模型输出 token 数。Claude 不在 SDK usage 里区分 reasoning 子项。"""


class _OpenAITokenUsage(BaseModel):
    """OpenAI 系（codex 或 generic_chat-openai 通道）一轮 token 用量。

    SDK 原生字段。

    ⚠️ OpenAI 字段语义关键差异（跟 Anthropic 不同）：

    - ``input_tokens`` **含** ``cached_input_tokens`` 子集（不要相加）
    - ``output_tokens`` **含** ``reasoning_output_tokens`` 子集（不要相加）
    - ``context_usage`` 派生公式 = ``input_tokens``（已含 cache，不再加；manager 内部算）
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: Literal["openai"] = "openai"
    """通道标识，恒为 ``"openai"``；discriminated union 的 tag 字段。"""

    input_tokens: int = Field(default=0, ge=0)
    """本轮总输入 token 数（含 ``cached_input_tokens`` 子集；OpenAI 语义）。"""

    cached_input_tokens: int = Field(default=0, ge=0)
    """本轮命中 prompt cache 的输入子集（**是 ``input_tokens`` 的子集，不要相加**）。

    OpenAI prompt caching 自动开启（≥1024 tokens），命中折扣 25%~50%。"""

    output_tokens: int = Field(default=0, ge=0)
    """本轮总输出 token 数（含 ``reasoning_output_tokens`` 子集；OpenAI 语义）。"""

    reasoning_output_tokens: int = Field(default=0, ge=0)
    """本轮推理思考用 token 数（**是 ``output_tokens`` 的子集，不要相加**）。

    o1/o3 系列内部推理（用户不可见）；普通模型为 0。"""


TokenUsage = Annotated[
    _AnthropicTokenUsage | _OpenAITokenUsage,
    Field(discriminator="channel"),
]
"""manager 私有 union 类型。外部禁止 import / 实例化（``.importlinter`` 强制）。

Pydantic v2 标准 discriminated union；按 ``channel`` 字段路由到具体 variant。
"""


# =============================================================================
# 公共：UsageTokenSnapshot（一轮快照）
# =============================================================================


class UsageTokenSnapshot(BaseModel):
    """一轮 token 用量快照（**外部 DTO**，从 ``TokenUsage`` 派生）。

    供 ``ws_event_sink`` emit / 前端 StatusLine 实时显示用。

    ``extras`` 字段装 channel-specific 字段（key 是 SDK 原生字段名），避免外部代码
    依赖 channel 分支判断。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: Literal["anthropic", "openai"]
    """通道标识；外部可读但不应据此做硬分支（用 extras 字段名拿值即可）。"""

    input_tokens: int = 0
    """本轮输入 token 数（注意：Anthropic 不含 cache，OpenAI 含 cache 子集）。"""

    output_tokens: int = 0
    """本轮输出 token 数（注意：OpenAI 含 reasoning 子集）。"""

    extras: dict[str, int] = Field(default_factory=dict)
    """channel-specific 字段补充：

    - Anthropic：``{"cache_read_input_tokens": N, "cache_creation_input_tokens": M}``
    - OpenAI：``{"cached_input_tokens": N, "reasoning_output_tokens": M}``
    """

    context_usage: int = 0
    """当前 context 占用（已按 channel 派生好；外部直接显示即可）。

    - Anthropic：input + cache_read + cache_creation
    - OpenAI：input_tokens（已含 cache）
    """

    turn: int
    """本轮 turn 编号（runner state.turn）。"""

    run_id: str
    """本轮 run id（runner state.run_id；空字符串=未携带）。"""


# =============================================================================
# 公共：ThreadUsageSummary（thread 级累计 summary）
# =============================================================================


class ThreadUsageSummary(BaseModel):
    """thread 级累计 token 用量 summary（**外部 DTO**）。

    供 ``GET /api/threads`` 返回 / 前端 ``hydrateUsageFromThreads`` / StatusLine 渲染。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: Literal["anthropic", "openai"]
    """通道标识（同 thread 永远不变）。"""

    cumulative_input_tokens: int = 0
    """thread 级累计输入 token 总量。"""

    cumulative_output_tokens: int = 0
    """thread 级累计输出 token 总量。"""

    extras: dict[str, int] = Field(default_factory=dict)
    """channel-specific 累计字段（同 ``UsageTokenSnapshot.extras`` 但是累计值）。"""

    last_run_context_usage: int = 0
    """最近一轮的 ``context_usage``（取自 last_run_snapshot；用于 StatusLine 显示）。"""

    model_name: str = ""
    """thread 当前绑定的模型名（用来查 context_window 上限）。

    空字符串=未知；前端可显示 "未知模型" 或回退为 None 百分比。"""

    model_context_window: int | None = None
    """对应模型的 context window 上限（来自 manager 内置表或外部注入）；
    ``None`` 表示未命中模型表，前端不显示百分比。"""

    context_usage_pct: float | None = None
    """``last_run_context_usage / model_context_window``；
    ``None`` 表示 ``model_context_window`` 未命中。"""


# =============================================================================
# 公共：UsageFrame（WS S2C 帧）
# =============================================================================
#
# 注意：本类**取代**旧的 ``src/web/protocol/ws_frames.py::UsageFrame``——后者在
# task#3（cross-module wiring）中删除并迁移到本处。task#1 阶段两者并存（旧 frame
# 仍在 protocol/ws_frames.py），task#3 完成后才删旧 frame。

# 为避免 task#1 阶段就破坏 protocol/ws_frames.py，本文件**不**从 protocol._base
# 继承 _S2CFrameBase；改成轻量 BaseModel，由 task#3 wiring 时确定继承关系。

# =============================================================================


class UsageFrame(BaseModel):
    """WS S2C 帧：一轮 token 用量推送（嵌套 ``UsageTokenSnapshot``）。

    供 ``ws_event_sink._build_frame(kind="usage")`` 通过 ``manager.to_ws_frame``
    构造（task#3 wiring）。

    替代旧的 ``web.protocol.ws_frames.UsageFrame``（task#3 删旧用新）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["usage"] = "usage"
    """WS 帧 discriminator。"""

    turn: int
    """本轮 turn 编号。"""

    run_id: str = ""
    """本轮 run id（空字符串=未携带）。"""

    timestamp_ms: int = 0
    """帧产生时间戳（ms epoch）；0 表示 task#3 wiring 时再填。"""

    snapshot: UsageTokenSnapshot
    """嵌套快照——所有 token 字段在这里（不在 frame 平铺）。"""


__all__ = [
    "ThreadUsageSummary",
    # 私有 union（**外部禁止 import**，仅 manager 内部用）
    "TokenUsage",
    "UsageFrame",
    # 公共 DTO（外部可 import）
    "UsageTokenSnapshot",
    "_AnthropicTokenUsage",
    "_OpenAITokenUsage",
]
