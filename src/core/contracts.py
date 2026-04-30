"""跨模块共享协议真源。

这是 v1-mini 唯一可以被其他模块 ``import`` 的协议定义文件。
如果一个接口会被 ``core / tools / context / executors / safety / observability / host / cli``
中任意两个模块同时消费，它就必须收口到这里。

当前收进来的协议：

- :class:`Session`
- :class:`Tool`
- :class:`ApprovalProvider`
- :class:`LLMProvider`
- :class:`EventSink`
- :class:`PromptDebugSink`

以及一组支撑数据结构（:class:`ToolContext` / :class:`ToolResult` /
:class:`ApprovalRequest` / :class:`ApprovalDecision` / :class:`LLMRequest` /
:class:`LLMResponse` / :class:`Event`）。

:class:`ToolLookup` 是 runner 对外拿工具的抽象面：runner 不需要知道具体的
``tools/registry.py`` 类，只要拿到一个能按名查工具的对象即可。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from core.message import Message

# ---------------------------------------------------------------------------
# Tool 相关支撑类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolContext:
    """工具执行上下文。

    Attributes:
        run_id: 当前 run 的 id，便于工具在日志 / trace 里标识来源。
        session_id: 当前 session id。
        turn: 工具被调用所在的 turn，从 1 开始计数。
        call_id: 对应的 :class:`core.message.ToolCall.call_id`。
        metadata: 装配层注入的额外上下文（例如 cwd、env 快照），core 不解释内容。
    """

    run_id: str
    session_id: str
    turn: int
    call_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果。

    ``ok=False`` 表示工具自己判定失败但已有结构化信息。
    工具实现抛出的原始异常由 runner 在外层包成
    :class:`core.errors.ToolError`，不走这个字段。
    """

    ok: bool
    content: str
    data: dict[str, Any] | None = None
    error_message: str | None = None


@runtime_checkable
class Tool(Protocol):
    """统一工具协议。

    实现方通常在 ``tools/`` 下面。core 本身不提供 Tool 实现。

    Attributes:
        name: 唯一工具名，模型通过它在 tool_call 里指向具体工具。
        description: 自然语言描述，会出现在 LLM 的 tools 列表里。
        input_schema: 参数 JSON schema；provider 适配层负责转换成目标格式。
    """

    name: str
    description: str
    input_schema: dict[str, Any]

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行一次工具调用。必须是 async。"""
        ...


@runtime_checkable
class ToolLookup(Protocol):
    """工具查找面。

    runner 只依赖这一层，不依赖具体 ``tools/registry.py``。
    ``Mapping[str, Tool]`` 天然满足此 Protocol（因为 ``__getitem__`` 和
    ``__contains__`` 都在），所以测试里可以直接传 dict。
    """

    def __contains__(self, name: object) -> bool: ...
    def __getitem__(self, name: str) -> Tool: ...


# ---------------------------------------------------------------------------
# Approval 相关支撑类型
# ---------------------------------------------------------------------------


ApprovalOutcome = Literal["approved", "rejected", "cancelled"]


@dataclass(frozen=True)
class ApprovalRequest:
    """一次审批请求。

    Attributes:
        run_id / session_id / turn / call_id: 定位该次调用的运行坐标。
        tool_name: 待审批的工具名。
        arguments: 模型发起的参数。审批端可以据此展示给人看。
        reason: 装配层给出的补充理由（例如"命中 permission=ask 规则"）。
        metadata: 额外信息，例如涉及文件路径、执行命令等摘要。
    """

    run_id: str
    session_id: str
    turn: int
    call_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovalDecision:
    """一次审批结果。

    Attributes:
        outcome: approved / rejected / cancelled 之一。
        reason: 人工或策略给出的理由文本。
        metadata: 附加信息，比如操作者身份、来源 UI 等。

    **safety v0.1.4 metadata 字段约定**（不改变协议形态，仅约定 metadata 内的键名）：

    - ``decision_class``: ``hard_block`` / ``silent_allow`` / ``explicit_consent``
      —— 主决策三段式分类。
    - ``decision_source``: ``intrinsic`` / ``session`` / ``config``
      （silent_allow 用） / ``standard`` / ``elevated``（explicit_consent 用）
      —— 证据来源或审批强度。``hard_block`` 不使用此字段。
    - ``matched_rule``: 命中规则的字符串描述（如 ``"deny:~/.ssh/"``、
      ``"grant:tests/integration/"``）。
    - ``reason``: 人读理由（用于 trace / UI 展示）。
    - ``grant_scope``: ``session`` / ``config`` —— 仅 ``decision_source=session/config``
      的 silent_allow 携带，标识 grant 持久化范围。
    - ``boundary_kind``: ``host`` / ``sandbox`` —— v0.1.4 实现恒为 ``host``，
      为未来 sandbox 落地预留。
    - ``suggested_alternatives``: ``list[str]`` —— 仅 hard_block 时携带，
      给 LLM 的备选建议（如 "改写到 ~/scratch/ 后再要"）。
    - ``stage``: 兼容 v0.1.3 字段（``capability`` / ``permission`` / ``approval``），
      由 SafetyDecisionEngine 按映射规则填充。

    详见 ``docs/safety-scope-v0.1.4/04-data-and-state.md``。
    """

    outcome: ApprovalOutcome
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        """便捷判断：只有 ``outcome == "approved"`` 才视为通过。"""
        return self.outcome == "approved"


@runtime_checkable
class ApprovalProvider(Protocol):
    """审批入口协议。

    第一批默认实现是 ``tools/approval.py`` 里的 ``InteractiveApproval``；
    后续 safety 模块会基于策略返回决定。核心约束：
    **不改变协议形状，只新增实现**。
    """

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """对一次工具调用做出审批决定。"""
        ...


class ApprovalAction(StrEnum):
    """v0.1.4 安全链审批结果的结构化 action。

    ``InteractiveApproval`` 在 v0.1.3 仅返回 ``bool``；v0.1.4 升级为携带
    ``ApprovalAction`` 的结构化决策，让用户除了"允许 / 拒绝"外，还能
    选择"本会话允许"或"持久化到配置"。

    - ``ACCEPT_ONCE``：一次性允许，不缓存 grant
    - ``ACCEPT_FOR_SESSION``：允许并写入本会话 ``GrantStore``，session 结束失效
    - ``ACCEPT_PERSIST``：允许并提议写回项目 yaml 配置（CLI 应做二次确认）
    - ``REJECT``：拒绝

    映射到 :class:`ApprovalDecision`：

    - ``ACCEPT_ONCE`` → ``outcome="approved"``，``metadata.grant_scope`` 缺省
    - ``ACCEPT_FOR_SESSION`` → ``outcome="approved"``，``metadata.grant_scope="session"``
    - ``ACCEPT_PERSIST`` → ``outcome="approved"``，``metadata.grant_scope="config"``
    - ``REJECT`` → ``outcome="rejected"``

    向后兼容：v0.1.3 旧 bool 返回路径由 adapter 自动映射为
    ``ACCEPT_ONCE`` / ``REJECT``，不破坏现有 ApprovalProvider 实现。
    """

    ACCEPT_ONCE = "accept_once"
    ACCEPT_FOR_SESSION = "accept_for_session"
    ACCEPT_PERSIST = "accept_persist"
    REJECT = "reject"


# ---------------------------------------------------------------------------
# LLM Provider 相关支撑类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMRequest:
    """runner 发给 provider 的统一请求。

    Attributes:
        model: 模型名。核心协议不收模型厂商信息，只收字符串。
        messages: 对话历史片段，包含 system / user / assistant / tool。
        tools: 可用的工具协议列表；provider 负责转成自己的 schema。
        temperature / max_tokens / timeout_seconds: 采样参数；``None`` 表示由 provider
            默认或由统一配置层补齐。
        metadata: 预留给装配层附加 provider 特定字段，不进核心协议。
    """

    model: str
    messages: tuple[Message, ...]
    tools: tuple[Tool, ...] = ()
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


FinishReason = Literal["stop", "tool_calls", "length", "error", "other"]


@dataclass(frozen=True)
class LLMResponse:
    """provider 返回的统一响应。

    Attributes:
        message: 模型这一轮的 assistant 消息。允许只带 tool_calls，不带 content。
        finish_reason: 结束原因；tool_calls 表示模型要求继续执行工具。
        usage: token 使用量等运行时统计（透传给 observability）。
        provider_metadata: 厂商扩展字段（reasoning_tokens / cached_tokens /
            reasoning_content / request_id / model / system_fingerprint 等）。
            dict 结构，保留原始字段名，不强行归一化。core 不解释内容，只透传。
        raw: 原始 provider 响应的精简引用，仅用于调试，core 不读它。
    """

    message: Message
    finish_reason: FinishReason = "stop"
    usage: dict[str, Any] = field(default_factory=dict)
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    raw: Any | None = None


@runtime_checkable
class LLMProvider(Protocol):
    """LLM provider 统一协议。

    具体适配在 ``executors/llm/*.py``。
    """

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """发起一次模型调用并拿回统一响应。必须 async。"""
        ...


# ---------------------------------------------------------------------------
# LLM 流式增量（provider-agnostic）
# ---------------------------------------------------------------------------


StreamChunkKind = Literal[
    "reasoning.delta",
    "content.delta",
    "tool_call.start",
    "tool_call.arguments.delta",
    "tool_call.end",
    "message.done",
]


@dataclass(frozen=True)
class LLMStreamChunk:
    """provider-agnostic 流式增量事件。

    一次完整的流结束后，必须有且只有一个 ``kind="message.done"`` 的 chunk
    作为终态。runner 把该 chunk 的 ``message`` / ``finish_reason`` / ``usage``
    / ``provider_metadata`` 当作等价 :class:`LLMResponse` 使用，后续 tool_call
    / approval / session.append 链路与非流式完全一致。

    字段必填矩阵详见 ``docs/llm-provider-v0.2/streaming/04-data-and-state.md`` §1.1。
    核心约束（parser 必须保证）：

    - ``message.done`` 必须是流的最后一个 chunk
    - 同一 ``index`` 下 ``tool_call.start`` 必须在所有 ``tool_call.arguments.delta``
      之前；``tool_call.end`` 必须在该 index 的所有 delta 之后
    - ``tool_call.end`` 只承载定位信息（index / tool_call_id / tool_name），
      不包含 arguments 完整字符串或 extra_content；完整 tool_call（含合法 JSON
      化的 arguments 与 extra_content）只能通过 ``message.done.message.tool_calls``
      获取
    - 任何 ``*.delta`` kind 的 chunk 必须携带非空 ``delta``

    Attributes:
        kind: chunk 类型。见上方 :data:`StreamChunkKind`。
        delta: ``*.delta`` kind 的增量字符串；其他 kind 忽略。
        index: ``content.delta`` / ``tool_call.*`` 的槽位。``content.delta`` V1
            始终为 0（单正文 block）。
        tool_call_id: ``tool_call.start`` / ``tool_call.end`` 必填；
            ``tool_call.arguments.delta`` 可选。
        tool_name: ``tool_call.start`` / ``tool_call.end`` 必填。
        message: 仅 ``message.done`` 必填，其他 kind 忽略；等价非流式
            :attr:`LLMResponse.message`。
        finish_reason: 仅 ``message.done`` 必填。
        usage: 仅 ``message.done`` 必填（可为空 dict），其他 kind 忽略。
        provider_metadata: 仅 ``message.done`` 必填（可为空 dict），其他 kind 忽略。
    """

    kind: StreamChunkKind
    delta: str = ""
    index: int = 0
    tool_call_id: str | None = None
    tool_name: str | None = None
    message: Message | None = None
    finish_reason: FinishReason | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    provider_metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SupportsLLMStream(Protocol):
    """流式能力 Protocol，与 :class:`LLMProvider` 正交。

    具备该能力的 provider 必须同时满足 :class:`LLMProvider`；runner 用
    ``isinstance(llm, SupportsLLMStream)`` 做能力探测，避免用
    :class:`NotImplementedError` 作控制流。

    调用约定：

    - runner 不 ``await stream()`` 本身，直接拿 :class:`AsyncIterator`
    - provider 在 iterator 内部处理 HTTP / SSE / 累积
    - runner 消费完所有 chunk（直到见到 ``kind="message.done"`` 的终态）
    - 非 ``message.done`` 结束 = 流中断 = 由 provider 抛
      :class:`core.errors.ProviderError`；runner 不靠 iterator 耗尽来推断终态

    现有不具备流式能力的 provider / stub 不需要改：结构上没有 ``stream()``
    方法，``isinstance(..., SupportsLLMStream)`` 自动为 False。
    """

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        """对一次请求启动流式响应。返回可迭代的 :class:`LLMStreamChunk` 流。"""
        ...


# ---------------------------------------------------------------------------
# Session 协议
# ---------------------------------------------------------------------------


@runtime_checkable
class Session(Protocol):
    """会话历史存储协议。

    - :mod:`core.session` 提供第一批 ``InMemorySession`` 默认实现。
    - ``context/session_store.py`` 以后会提供工程化实现，继续遵守此协议，
      不得再定义一份新 Session 接口。
    """

    session_id: str

    async def append(self, message: Message) -> None:
        """追加一条消息到会话末尾。"""
        ...

    async def history(self) -> list[Message]:
        """返回当前完整历史，顺序与 append 顺序一致。"""
        ...

    async def clear(self) -> None:
        """清空当前会话历史。"""
        ...

    async def advance_run_index(self) -> int:
        """递增并返回新的 run 编号；递增后立即持久化（如有持久化层）。

        语义：
        - 调用一次 → +1 → 返回新值
        - 持久化后端（FileSession / 未来 SQLite）必须把递增后的值落盘
        - 内存后端（InMemorySession）只更新实例字段
        - sqlite 后端当前不维护，可抛 ``NotImplementedError``

        非事务原子约定（v0.x 简化）：
        - 调用方通常在 ``session.append(user_msg)`` 之后立刻调用本方法，
          以把"用户消息入历史"和"run 编号递增"绑到同一时机
        - 严格讲两步应单事务原子提交，但 v0.x 阶段不做事务化：
          - append 成功 + advance 失败 → 下次启动 run_count 不变，新 run_index
            复用上次值；本 run 已崩溃未生成 run_id 记录，新一轮取此值仍唯一
            不撞，不会丢消息也不会让 run_id 重复
          - advance 成功 + append 失败（极罕见）→ jsonl 少一条，run_count 跳号，
            仍唯一不撞
        - 事务化留给 v0.2+
        """
        ...


# ---------------------------------------------------------------------------
# EventSink / Event
# ---------------------------------------------------------------------------


EventKind = Literal[
    "run.start",
    "run.end",
    "turn.start",
    "turn.end",
    "llm.request",
    "llm.response",
    "tool.call.start",
    "tool.call.end",
    "approval.request",
    "approval.decision",
    "error",
    # v0.2 流式（llm-streaming-v1）新增：
    # - content.delta / reasoning.delta：runner 消费 LLMStreamChunk 后按每片增量
    #   emit，payload={"delta": str, "index": int?}
    # - llm.chunk.first：首个非空 chunk 抵达时 emit 一次，
    #   payload={"elapsed_ms": int, "model": str | None}；用于 TTFT 度量
    # - llm.stream.end：流结束汇总 emit 一次，
    #   payload={"chunk_count": int, "finish_reason": str, "content_chars": int,
    #            "reasoning_chars": int, "tool_call_count": int, "truncated_args": bool}
    "content.delta",
    "reasoning.delta",
    "llm.chunk.first",
    "llm.stream.end",
    # Memory 模块事件（self-evolution-memory v0.1.3+）：
    # - memory.write.success：safety_write.execute_write 成功落盘 (status="written")
    # - memory.write.rejected：内容扫描拦截（prompt injection / secret / 不可见 Unicode 等）
    # - memory.write.error：写入失败（error / not_found / skipped）
    # - memory.snapshot.refreshed：history.compact 后 MemoryRefreshSink 重新
    #   load_from_disk 得到新 snapshot 时 emit；和启动时一次性的 snapshot.captured
    #   并列（前者可多次，后者只在装配阶段出现）
    "memory.write.success",
    "memory.write.rejected",
    "memory.write.error",
    "memory.snapshot.refreshed",
    # safety v0.1.4 决策事件（safety-scope-v0.1.4 模块 8 DecisionTrace）：
    # - tool.denied：HardBlock 命中 / ConsentResolver 用户拒绝。payload 含
    #   decision_class / decision_source / matched_rule / reason / boundary_kind
    #   / tool_name / path_or_command / request_id / outcome
    # - tool.approval_required：ConsentResolver 进入 standard / elevated ask 时
    #   触发，发起 InteractiveApproval 之前 emit 一次（先于 decision）
    # - tool.silently_allowed：TrustResolver 命中 intrinsic / session / config
    #   或 ConsentResolver 用户允许时触发。read 类工具的 silently_allowed
    #   默认不写盘（受 config.safety.log_silent_reads 控制，避免 jsonl 膨胀）
    "tool.denied",
    "tool.approval_required",
    "tool.silently_allowed",
    # Skill loader 装配期事件（skill-loader-v0.1.6）：
    # - skill.discovered：每成功解析一个 SkillSpec
    # - skill.shadowed：workspace 覆盖 home 时
    # - skill.parse_failed：frontmatter YAML 解析失败 / 必填字段缺失
    # - skill.skipped：符号链接逃逸 skill 目录
    "skill.discovered",
    "skill.shadowed",
    "skill.parse_failed",
    "skill.skipped",
    # Skill 运行时事件（skill-tool-v0.1.6）：
    # - skill.invoked：SkillTool.execute 入口（unknown skill 不 emit）。
    #   payload={"name", "source", "body_path", "args", "body_chars": 0}
    # - skill.completed：body 读取 + 变量替换成功后 emit。
    #   payload={"name", "source", "body_chars", "elapsed_ms"}
    # - skill.failed：read 失败 / `!command` 拒绝 / 任何派生异常。
    #   payload={"name", "source", "error_kind", "message", "elapsed_ms"}
    "skill.invoked",
    "skill.completed",
    "skill.failed",
]


@dataclass(frozen=True)
class Event:
    """统一事件结构。

    runner 在关键节点构造 Event，fan-out 到所有注册的 :class:`EventSink`。
    v1-mini 只有一个 sink：``observability/trace_sink.py`` 的 ``JsonlTraceSink``。
    v0.2+ 追加 usage / audit sink 时仍然走同一个协议，不新增事件协议。
    """

    kind: str
    run_id: str
    turn: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))


@runtime_checkable
class EventSink(Protocol):
    """事件落地协议。

    实现方保证 ``emit`` 是幂等写入或至少不会抛异常污染主链路。
    fan-out 职责在 runner，不是 sink 自己的事。
    """

    async def emit(self, event: Event) -> None:
        """处理一个事件。"""
        ...


# ---------------------------------------------------------------------------
# MessageCompactor
# ---------------------------------------------------------------------------


@runtime_checkable
class MessageCompactor(Protocol):
    """runner 在每个 turn 把 history 送给 LLM 之前的加工钩子。

    实现类在 ``context.history_compactor.HistoryCompactor``；core 只定义接口，
    不持有实现。命名刻意和实现类错开（Protocol = ``MessageCompactor``，实现 =
    ``HistoryCompactor``），避免 ``from core.contracts import HistoryCompactor``
    和 ``from context import HistoryCompactor`` 同名歧义。

    典型实现：压缩超长 history（裁剪空白消息、截断长 tool_result）。未来如果要做
    敏感字段 redact / few-shot 注入，也走同一个 Protocol，不新增协议。
    """

    async def compact(self, history: Sequence[Message]) -> list[Message]:
        """给定原始 history，返回加工后的 messages 列表。

        约定：
        - 永远返回**新** list，不就地修改入参
        - 空输入 → 空 list
        - 不涉及阈值时可以直接返回 ``list(history)``（原样拷贝）
        """
        ...


# ---------------------------------------------------------------------------
# InputAssembler 协议
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssembledInput:
    """InputAssembler.assemble() 的返回类型契约。

    core 在此定义完整数据结构（协议单一真源），context 层 InputAssembler
    直接使用此类型，不再重定义。

    Attributes:
        messages: 要发给 provider 的完整 messages（system 在最前，compact 后）。
        metadata: 装配元数据，runner 用于构造 ``history.compact`` 事件。
            最少含 ``original_count`` 和 ``compacted_count`` 两个 int 字段。
        system_message: 本次新追加的 system 消息；``None`` 表示没有追加。
    """

    messages: list[Message]
    metadata: dict[str, Any] = field(default_factory=dict)
    system_message: Message | None = None


class PromptSource(Protocol):
    """指令来源的最小 Protocol，runner 用于传给 assembler。

    具体实现（``context.instruction_loader.InstructionSource``）是 frozen
    dataclass，满足此 Protocol。runner 只需知道 origin 和 content 两个属性。
    """

    @property
    def origin(self) -> str: ...

    @property
    def content(self) -> str: ...


class PromptAssembler(Protocol):
    """prompt build 的统一入口协议（runner 对 InputAssembler 的视图）。

    实现类在 ``context.input_assembler.InputAssembler``；core 只定义接口，
    不持有实现。runner 只调用 :meth:`assemble`，不关心内部如何做 compact /
    system 注入。

    注意：返回值类型声明为 :class:`AssembledInput`（core 定义的最小契约），
    实现方可以返回更丰富的子类型（例如 context 里带 ``system_message`` 字段
    的版本），runner 只读 ``messages`` 和 ``metadata``，不受影响。
    """

    async def assemble(
        self,
        history: Sequence[Message],
        instructions: Sequence[Any] = (),
    ) -> AssembledInput:
        """装配最终输入。

        Args:
            history: 当前 session 的原始历史。
            instructions: 静态指令来源列表；空列表则不注入 system。

        Returns:
            满足 :class:`AssembledInput` 结构的装配结果（``messages`` + ``metadata``）。
        """
        ...


class PromptDebugSink(Protocol):
    """prompt debug dump 输出协议。

    实现方通常在 ``observability/``，runner 只负责在 LLM request 前把
    当前 turn 的 prompt build 快照交出去。
    """

    def dump(
        self,
        *,
        session_id: str,
        run_id: str,
        turn: int,
        model: str,
        instruction_origins: Sequence[str],
        history_before_assemble: Sequence[Message],
        assembled_messages: Sequence[Message],
        metadata: Mapping[str, Any],
        added_system_prompt: str | None,
    ) -> str:
        """写出 prompt debug 快照，返回输出路径字符串。"""
        ...


__all__ = [
    # Tool
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
