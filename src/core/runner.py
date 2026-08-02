"""唯一 run loop 真身。

这里是 v1-mini 里 **仅有的** turn 推进入口。``session_engine.py``、
``session.py``、``run_state.py`` 都不允许再长出第二套 loop。

职责边界：

- 负责 turn 推进、tool_call 回填、停止条件、结果收口
- 负责把关键节点事件 fan-out 到 ``list[EventSink]``
- 负责把异常包成 :class:`core.errors.AgentError` 子类

不负责：

- 装配 provider / tools / session（那是 ``runtime_assembly/session_engine.py`` 的事）
- safety 判定（capability / permission 由装配层预先串好，runner 只消费 ApprovalProvider）
- 具体 provider 调用或工具执行（通过协议委托）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from core.agent_spec import AgentSpec
from core.contracts import (
    ApprovalProvider,
    ApprovalRequest,
    Event,
    EventSink,
    FinishReason,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMToolCallContract,
    LLMToolCallContractMode,
    LLMToolCallViolationKind,
    MessageCompactor,
    PreparedToolCall,
    PromptAssembler,
    PromptDebugSink,
    PromptSource,
    ProviderUsageScope,
    ProviderUsageSnapshot,
    Session,
    SteerRequest,
    SupportsLLMStream,
    Tool,
    ToolCallPreparer,
    ToolContext,
    ToolExecutionScope,
    ToolLookup,
    ToolResult,
    aggregate_provider_usage_snapshots,
)
from core.errors import (
    AgentError,
    LLMToolCallContractError,
    MaxTurnsExceededError,
    ProviderError,
    ToolError,
)
from core.lifecycle import LifecycleHook
from core.message import Message, ToolCall
from core.result import Result, compute_run_end_reason
from core.run_state import RunState

_RUN_EVENT_SINKS: ContextVar[tuple[EventSink, ...]] = ContextVar(
    "_RUN_EVENT_SINKS",
    default=(),
)
"""当前 asyncio task 绑定的单次 run 临时 EventSink 列表。"""

_RUN_AGENT_ID: ContextVar[str] = ContextVar(
    "_RUN_AGENT_ID",
    default="",
)
"""当前 asyncio task 绑定的单次 run 的 agent 归属（agent-tree-v0.1 模块 G）。

在 :meth:`Runner._run_with_seed` 入口按 run 参数 ``agent_id`` 设置，
:meth:`Runner._emit` fan-out 前读取并注入到每条 Event 的 ``agent_id`` 坐标
字段（用 :func:`dataclasses.replace`，因为 Event 是 frozen dataclass）。
这样 runner 内 20+ emit 点无需逐处透传 ``agent_id``，统一在 ``_emit`` 收口。
turn 边界事件的 task_id / conversation_id 由 ``event_context`` 显式填充。
"""


_logger = logging.getLogger(__name__)
_STREAM_CLOSE_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class _RunInstructionSource:
    """单次 run 的指令来源，输入为 origin/content，输出满足 PromptSource。"""

    origin: str
    content: str


@dataclass(frozen=True)
class _ToolCallContractViolation:
    """一次 assistant 响应的工具调用合同违规坐标。"""

    kind: LLMToolCallViolationKind
    tool_name: str | None
    tool_call_id: str | None
    tool_index: int | None
    observed_tool_call_count: int


class _ToolCallContractViolationSignal(Exception):
    """在流消费阶段把结构化合同违规交回 turn 驱动层。"""

    def __init__(self, violation: _ToolCallContractViolation) -> None:
        super().__init__(violation.kind.value)
        self.violation = violation
        self.event_emitted = False


@runtime_checkable
class _SupportsAsyncClose(Protocol):
    """可显式关闭的异步响应迭代器。"""

    async def aclose(self) -> None:
        """释放当前 provider 响应流持有的资源。"""
        ...


class _TurnEventPhase(StrEnum):
    """turn 边界事件 phase 枚举，序列化为字符串。"""

    START = "start"
    END = "end"


@dataclass(frozen=True)
class _RunEventContext:
    """单次 run 的事件上下文，来源于宿主/mailbox 层。

    字段保持稳定值对象语义，Runner 只消费，不原地修改。session_id / agent_id /
    run_id / turn 来自 Runner 自身，mailbox 相关字段从宿主传入。
    """

    run_epoch: int | None = None
    mail_kind: str = ""
    mail_task_id: str = ""
    conversation_id: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> _RunEventContext:
        """从可选 mapping 构造事件上下文，输入为宿主参数，输出为冻结值对象。"""
        if not raw:
            return cls()
        return cls(
            run_epoch=_optional_int(raw.get("run_epoch")),
            mail_kind=_optional_str(raw.get("mail_kind")),
            mail_task_id=_optional_str(raw.get("mail_task_id")),
            conversation_id=_optional_str(raw.get("conversation_id")),
        )

    def conversation_or_session(self, session_id: str) -> str:
        """返回会话树 id；未传 conversation_id 时退化为 session_id。"""
        return self.conversation_id or session_id


@dataclass
class _SteerBuffer:
    """单个活跃 run 的补充输入（steer）缓冲。

    职责：承接"run 进行中"到达的第二条及后续 :class:`SteerRequest`，等
    :meth:`Runner._drive_turns` 在下一个 turn 边界一次性 drain 注入当前 run。

    关键字段：
    - ``items``：待注入 SteerRequest 的 FIFO 列表，:meth:`Runner.steer` 追加、
      drain 时取走。每项携带 text（内容真源）+ pending_input_id（消账主键）。
    - ``closed``：run 收尾时置 True；置 True 后 :meth:`Runner.steer` 拒收（返回 False），
      调用方据此回落排队。可变 dataclass —— 状态需原地更新，不用 frozen。
    """

    items: list[SteerRequest] = field(default_factory=list)
    closed: bool = False


def _llm_request_to_event_payload(request: LLMRequest) -> dict[str, Any]:
    """把真实 ``LLMRequest`` 按同名字段序列化为事件 payload。"""

    return request.to_audit_dict()


def _llm_response_to_event_payload(response: LLMResponse) -> dict[str, Any]:
    """把真实 ``LLMResponse`` 按同名字段序列化为事件 payload。"""

    return response.to_audit_dict()


def _optional_str(value: Any) -> str:
    """把可选值归一成字符串，None 归一为空串。"""
    return "" if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    """把可选值归一成 int；无法安全转换时返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _build_turn_event(
    *,
    kind: str,
    phase: _TurnEventPhase,
    state: RunState,
    session: Session,
    event_context: _RunEventContext,
    assistant_message: Message | None = None,
    finish_reason: str | None = None,
    history_index: int | None = None,
) -> Event:
    """构造 turn.start / turn.end 事件，集中维护 trace payload 字段。"""
    conversation_id = event_context.conversation_or_session(session.session_id)
    payload: dict[str, Any] = {
        "session_id": session.session_id,
        "agent_id": state.agent_id,
        "run_epoch": event_context.run_epoch,
        "mail_kind": event_context.mail_kind,
        "mail_task_id": event_context.mail_task_id,
        "conversation_id": conversation_id,
        "phase": phase.value,
    }
    if assistant_message is not None:
        tool_calls = assistant_message.tool_calls or ()
        payload.update(
            {
                "has_tool_calls": bool(tool_calls),
                "tool_call_count": len(tool_calls),
                "finish_reason": finish_reason,
            }
        )
    if history_index is not None:
        payload["history_index"] = history_index
    return Event(
        kind=kind,
        run_id=state.run_id,
        turn=state.turn,
        payload=payload,
        agent_id=state.agent_id,
        task_id=event_context.mail_task_id,
        conversation_id=conversation_id,
    )


class Runner:
    """唯一 run loop。

    构造时只拿 ``event_sinks`` 和可选 ``lifecycle_hooks``；每次 :meth:`run`
    调用接 per-run 的依赖。装配层负责替每次调用拼齐参数。

    这个类是无状态的（run 相关状态都放在 per-call 的 :class:`RunState` 里），
    因此可以安全地被复用于多次 run。
    """

    def __init__(
        self,
        *,
        event_sinks: Sequence[EventSink] | None = None,
        lifecycle_hooks: Sequence[LifecycleHook] | None = None,
        message_compactor: MessageCompactor | None = None,
        input_assembler: PromptAssembler | None = None,
        instruction_sources: Sequence[PromptSource] | None = None,
        prompt_debug_sink: PromptDebugSink | None = None,
        instruction_origins: Sequence[str] | None = None,
        stream_enabled: bool = False,
        suppress_content_after_tool_call: bool = True,
        tool_context_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._event_sinks: list[EventSink] = list(event_sinks or [])
        self._lifecycle_hooks: list[LifecycleHook] = list(lifecycle_hooks or [])
        # 可选：把 history 送给 LLM 之前过一道 compactor；None 表示原样透传。
        self._message_compactor: MessageCompactor | None = message_compactor
        # 可选：PromptAssembler 接管 prompt build（system 注入 + compact）。
        # None 时退化到旧路径（_prepare_messages + _seed_messages 的 system 注入）。
        self._input_assembler: PromptAssembler | None = input_assembler
        # 静态指令来源列表，随 assembler 一起传入；None 时等价于空序列。
        self._instruction_sources: Sequence[PromptSource] = list(instruction_sources or [])
        self._prompt_debug_sink: PromptDebugSink | None = prompt_debug_sink
        self._instruction_origins: list[str] = list(instruction_origins or [])
        # 流式开关（v0.2 接入阶段）。装配层从 cfg.stream.enabled 注入；
        # provider 须满足 SupportsLLMStream Protocol 才会实际走流式（runner 用
        # isinstance 探测，不靠 NotImplementedError 控制流）。
        self._stream_enabled: bool = stream_enabled
        # 流中出现 tool_call 后，是否屏蔽继续到达的 content.delta（避免 CLI 在
        # 工具调用前打印夹带的乱文本）。仅影响事件 emit；message.done 中的
        # content 不受影响（runner 只在事件层做屏蔽，session 仍记录完整 message）。
        self._suppress_content_after_tool_call: bool = suppress_content_after_tool_call
        self._tool_context_metadata: dict[str, Any] = dict(tool_context_metadata or {})
        # steer 缓冲区：key = session_id，value = 该 session 当前活跃 run 的补充输入缓冲。
        # 由 _run_with_seed 入口注册、finally 按身份比对移除；steer()/_drive_turns 读写。
        # mailbox 串行消费保证同一 session 同一时刻至多一个活跃 run，故一个 key 一个 buffer。
        self._steer_buffers: dict[str, _SteerBuffer] = {}

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------

    def steer(self, session_id: str, request: SteerRequest) -> bool:
        """把补充输入（steer）追加到该 session 当前活跃 run 的缓冲区。

        **同步方法**（无 await）：与 run 收尾（``_run_with_seed`` 里 close buffer 并
        收残留那一段）同在事件循环的同步段内执行，两者天然互斥——不会出现"steer 追加
        到一半 run 已 close"的撕裂。调用方（SessionEngine.steer → HostDispatcher
        send-now）据返回值决定命中还是回落排队。

        关键输入：
        - ``session_id``：目标 session；用来定位活跃 run 的 buffer。
        - ``request``：待注入的补充输入（:class:`SteerRequest`），含 text 真源 +
          pending_id 消账主键。

        关键输出：
        - ``True``：buffer 存在且未 closed，已追加；下一个 turn 边界会被 drain 注入。
        - ``False``：buffer 不存在（无活跃 run）或已 closed（run 正在/已经收尾），
          调用方应回落到排队路径。
        """
        buffer = self._steer_buffers.get(session_id)
        if buffer is None or buffer.closed:
            return False
        buffer.items.append(request)
        return True

    async def run(
        self,
        user_input: str,
        *,
        session: Session,
        agent_spec: AgentSpec,
        llm: LLMProvider,
        tools: ToolLookup,
        approval: ApprovalProvider,
        max_turns: int | None = None,
        run_id: str | None = None,
        enabled_tools: Sequence[Tool] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
        lifecycle_hooks: Sequence[LifecycleHook] | None = None,
        event_sinks: Sequence[EventSink] | None = None,
        tool_context_metadata: Mapping[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
        llm_request_metadata: Mapping[str, Any] | None = None,
        event_context: Mapping[str, Any] | None = None,
        thread_id: str | None = None,
        agent_id: str = "",
        llm_tool_call_contract: LLMToolCallContract | None = None,
    ) -> Result:
        """执行一次完整 run。

        Args:
            user_input: 用户输入文本；会被包成 user Message 追加到 session。
            session: 会话存储。runner 不假设实现，只要满足 Session Protocol。
            agent_spec: 本次运行使用的 agent 规格，提供 system prompt / 默认 model。
            llm: LLM provider，负责真实模型调用。
            tools: 工具查找面，按 ``agent_spec.tool_names`` 解析。
                也接受 ``Mapping[str, Tool]``（如 dict），因为结构上满足 ToolLookup。
            approval: 审批 provider。所有工具调用都走它，即使策略是 auto-approve。
            max_turns: 覆盖 agent_spec.max_turns；None 表示使用 spec 默认。
            run_id: 外部可以注入 run_id 便于跨进程关联；None 时 runner 自生成。
            enabled_tools: 允许装配层直接传"已解析好的 Tool 列表"绕过 tool_names 查询。
                若提供，runner 不再按 spec.tool_names 去 tools 里查，避免重复。
            attachments: 用户输入附件 ref 列表（``UserInputAttachment.model_dump()``
                输出的 dict 形态）。非 None 时会写到首条 user :class:`Message`
                的 ``metadata["attachments"]``，供 InputAssembler / provider
                组装多模态输入。CLI 路径默认 None，不影响纯文本对话。
                详见 ``dev-pipeline/tasks/claude-image-paste-e2e/README.md`` §1 / §4。
            references: 用户输入 conversation reference 列表。非 None 时写到首条
                user :class:`Message` 的 ``metadata["conversation_references"]``，
                供 InputAssembler 注入本轮显式引用上下文。
            lifecycle_hooks: 本次 run 的临时 lifecycle hook，只作用于当前 run。
            event_sinks: 本次 run 的临时 EventSink，只接收当前 run 发出的事件。
            max_tokens / temperature / timeout_seconds: 本次 run 的请求级模型参数，
                写入 LLMRequest 并覆盖 provider 配置默认值。
            llm_request_metadata: 本次 run 的 provider 元数据，供适配层读取字段映射等
                provider 级参数。
            event_context: 本次 run 的观测上下文，供 turn.start / turn.end payload
                写入 mailbox epoch、mail kind、task id、conversation id 等信息。
            thread_id: 本次 run 所属顶层 thread。子 agent 传父级 root thread，
                未传时回落 session id；该值写入审批、工具和 LLM request metadata。
            agent_id: 本次 run 的 agent 归属（agent-tree-v0.1 模块 G）。默认 ``""``
                兼容现有调用；runner 将其写入 :attr:`RunState.agent_id`，各 Event
                emit 点的坐标字段 ``agent_id`` 和 ToolContext ``agent_id`` 均从此
                读取。单 agent 场景可由调用方传入固定值（如 ``"main"``）。

        Returns:
            :class:`Result`：运行结束的统一结果。

        Raises:
            不直接抛异常；所有异常都收口到 :class:`Result`：

            - :class:`AgentError` / 意外 :class:`Exception` → ``status="failed"``，
              ``error`` 字段承载具体异常
            - :class:`asyncio.CancelledError` → ``status="cancelled"``（v0.1
              interrupt-run-v0.1 起），不向外 re-raise；上游（HostDispatcher /
              CommandService / Web ws.py）只需要按 ``Result.status`` 分支即可，
              不需要自己 ``except CancelledError``。

            这是为了让 host / cli 只关心 Result 即可。
        """

        # run_id 不再在入口生成 uuid；改为 _seed_messages 内 advance_run_index 后拼装
        # (`run-{session_id}-{n}`)。外部传入的 run_id 仍然兜底优先（测试注入等场景）。
        # state.run_id 起始为 "" 占位，_seed_messages 写真值后再 emit run.start。
        run_id = run_id or ""
        effective_max_turns = max_turns if max_turns is not None else agent_spec.max_turns
        effective_lifecycle_hooks = [
            *self._lifecycle_hooks,
            *(lifecycle_hooks or ()),
        ]

        async def seed_messages(state: RunState) -> None:
            await self._seed_messages(
                session,
                agent_spec,
                user_input,
                state,
                attachments=attachments,
                references=references,
            )

        return await self._run_with_seed(
            session=session,
            agent_spec=agent_spec,
            llm=llm,
            tools=tools,
            approval=approval,
            run_id=run_id,
            effective_max_turns=effective_max_turns,
            enabled_tools=enabled_tools,
            effective_lifecycle_hooks=effective_lifecycle_hooks,
            event_sinks=event_sinks,
            tool_context_metadata=tool_context_metadata,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            llm_request_metadata=llm_request_metadata,
            event_context=event_context,
            thread_id=thread_id,
            seed_messages=seed_messages,
            agent_id=agent_id,
            llm_tool_call_contract=llm_tool_call_contract,
        )

    async def continue_from_last_user_message(
        self,
        *,
        session: Session,
        agent_spec: AgentSpec,
        llm: LLMProvider,
        tools: ToolLookup,
        approval: ApprovalProvider,
        max_turns: int | None = None,
        run_id: str | None = None,
        enabled_tools: Sequence[Tool] | None = None,
        lifecycle_hooks: Sequence[LifecycleHook] | None = None,
        event_sinks: Sequence[EventSink] | None = None,
        tool_context_metadata: Mapping[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
        llm_request_metadata: Mapping[str, Any] | None = None,
        event_context: Mapping[str, Any] | None = None,
        thread_id: str | None = None,
        agent_id: str = "",
        llm_tool_call_contract: LLMToolCallContract | None = None,
    ) -> Result:
        """Drive a run from the existing trailing user message.

        This entrypoint is for hosts that have already persisted the user
        message as the run boundary. It validates that the latest session
        message is a user message, claims a fresh run id, then reuses the same
        turn loop as :meth:`run` without appending another user message.

        ``thread_id`` / ``agent_id`` 语义同 :meth:`run`，透传给
        :meth:`_run_with_seed`。
        """

        run_id = run_id or ""
        effective_max_turns = max_turns if max_turns is not None else agent_spec.max_turns
        effective_lifecycle_hooks = [
            *self._lifecycle_hooks,
            *(lifecycle_hooks or ()),
        ]

        async def seed_messages(state: RunState) -> None:
            await self._claim_last_user_message(session, state)

        return await self._run_with_seed(
            session=session,
            agent_spec=agent_spec,
            llm=llm,
            tools=tools,
            approval=approval,
            run_id=run_id,
            effective_max_turns=effective_max_turns,
            enabled_tools=enabled_tools,
            effective_lifecycle_hooks=effective_lifecycle_hooks,
            event_sinks=event_sinks,
            tool_context_metadata=tool_context_metadata,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            llm_request_metadata=llm_request_metadata,
            event_context=event_context,
            thread_id=thread_id,
            seed_messages=seed_messages,
            agent_id=agent_id,
            llm_tool_call_contract=llm_tool_call_contract,
        )

    async def _run_with_seed(
        self,
        *,
        session: Session,
        agent_spec: AgentSpec,
        llm: LLMProvider,
        tools: ToolLookup,
        approval: ApprovalProvider,
        run_id: str,
        effective_max_turns: int,
        enabled_tools: Sequence[Tool] | None,
        effective_lifecycle_hooks: Sequence[LifecycleHook],
        event_sinks: Sequence[EventSink] | None,
        tool_context_metadata: Mapping[str, Any] | None,
        max_tokens: int | None,
        temperature: float | None,
        timeout_seconds: float | None,
        llm_request_metadata: Mapping[str, Any] | None,
        event_context: Mapping[str, Any] | None,
        thread_id: str | None,
        seed_messages: Callable[[RunState], Awaitable[None]],
        agent_id: str = "",
        llm_tool_call_contract: LLMToolCallContract | None = None,
    ) -> Result:
        """Run the shared turn loop after a caller-specific seed step."""

        state = RunState(run_id=run_id, session_id=session.session_id, agent_id=agent_id)
        state.mark_running()
        resolved_event_context = _RunEventContext.from_mapping(event_context)

        # steer buffer 注册：以 session_id 为 key。mailbox 串行消费下同一 session
        # 同一时刻至多一个活跃 run，故此处理论上不会撞到已有 buffer；万一撞到（并发
        # 逻辑被破坏）记 warning 并覆盖，避免旧 buffer 泄漏。finally 里按身份比对
        # （只 pop 到 my_buffer 自身）移除，防止误删并发新 run 注册的同 key buffer。
        sid = session.session_id
        my_buffer = _SteerBuffer()
        if sid in self._steer_buffers:
            _logger.warning(
                "steer buffer for session %r already exists at run start; overwriting "
                "(unexpected under serial mailbox consumption)",
                sid,
            )
        self._steer_buffers[sid] = my_buffer

        event_sink_token = _RUN_EVENT_SINKS.set(tuple(event_sinks or ()))
        # 绑定本次 run 的 agent_id 到当前 asyncio task，供 _emit 统一注入到
        # 每条 Event 的坐标字段（agent-tree-v0.1 模块 G）。与 _RUN_EVENT_SINKS
        # 同生命周期，在 finally 里一并 reset。
        agent_id_token = _RUN_AGENT_ID.set(agent_id)

        try:
            try:
                resolved_tools = self._resolve_tools(agent_spec, tools, enabled_tools)
                await seed_messages(state)
                parent_agent = _parent_agent_snapshot(
                    state=state,
                    session=session,
                    agent_spec=agent_spec,
                    effective_max_turns=effective_max_turns,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout_seconds=timeout_seconds,
                )
                effective_tool_context_metadata = self._resolve_tool_context_metadata(
                    tool_context_metadata,
                    parent_agent=parent_agent,
                    thread_id=thread_id,
                    session_id=session.session_id,
                )
                await self._emit(
                    Event(
                        kind="run.start",
                        run_id=state.run_id,
                        payload={
                            "session_id": session.session_id,
                            "agent": agent_spec.name,
                            "model": agent_spec.default_model,
                            "max_turns": effective_max_turns,
                            "reasoning_effort": agent_spec.reasoning_effort,
                            "max_tokens": max_tokens,
                            "temperature": temperature,
                            "timeout_seconds": timeout_seconds,
                        },
                    )
                )
                final_message, accumulated_usage = await self._drive_turns(
                    state=state,
                    session=session,
                    agent_spec=agent_spec,
                    llm=llm,
                    tools_by_name={t.name: t for t in resolved_tools},
                    resolved_tools=resolved_tools,
                    approval=approval,
                    max_turns=effective_max_turns,
                    lifecycle_hooks=effective_lifecycle_hooks,
                    tool_context_metadata=effective_tool_context_metadata,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout_seconds=timeout_seconds,
                    llm_request_metadata=llm_request_metadata,
                    event_context=resolved_event_context,
                    llm_tool_call_contract=llm_tool_call_contract,
                )
                state.mark_completed()
                result = Result(
                    run_id=state.run_id,
                    session_id=session.session_id,
                    status="completed",
                    final_message=final_message,
                    turn_count=state.turn,
                    metadata=(
                        {"usage": accumulated_usage.to_payload()}
                        if accumulated_usage is not None
                        else {}
                    ),
                )
            except asyncio.CancelledError:
                # interrupt-run-v0.1：外部 task.cancel()（典型 = 用户点 Stop）。
                # 不向外 re-raise，统一收口到 Result(status="cancelled")，与
                # AgentError / Exception 分支语义对齐（runner 不抛，host 只看 Result）。
                #
                # 此前 _execute_tool_calls 已对当前未配对 tool_use 合成占位 tool_result，
                # 保证 session jsonl 里 tool_use ↔ tool_result 一一对应（Anthropic 协议
                # 要求）；这里只负责状态机收尾 + emit 事件。
                state.mark_cancelled()
                # state.metadata 是 dict[str, str]；空字符串视为"未在 tool 阶段"。
                _cancelled_id_raw = state.metadata.get("cancelled_tool_call_id", "")
                cancelled_tool_call_id: str | None = (
                    _cancelled_id_raw if _cancelled_id_raw else None
                )
                cancel_meta: dict[str, object] = {
                    "cancelled_at_turn": state.turn,
                    "cancelled_tool_call_id": cancelled_tool_call_id,
                    "cancel_reason": "user_interrupt",
                }
                await self._emit(
                    Event(
                        kind="run.cancelled",
                        run_id=state.run_id,
                        turn=state.turn,
                        payload=cancel_meta,
                    )
                )
                result = Result(
                    run_id=state.run_id,
                    session_id=session.session_id,
                    status="cancelled",
                    final_message=None,
                    turn_count=state.turn,
                    error=None,
                    metadata=cancel_meta,
                )
            except AgentError as exc:
                state.mark_failed(exc)
                await self._emit(
                    Event(
                        kind="error",
                        run_id=state.run_id,
                        turn=state.turn,
                        payload={"type": type(exc).__name__, "message": exc.message},
                    )
                )
                result = Result(
                    run_id=state.run_id,
                    session_id=session.session_id,
                    status="failed",
                    final_message=None,
                    turn_count=state.turn,
                    error=exc,
                )
            except Exception as exc:  # pragma: no cover - 意外异常兜底
                wrapped = AgentError(
                    f"unexpected error in runner: {exc!r}",
                    details={"exception_type": type(exc).__name__},
                )
                state.mark_failed(wrapped)
                await self._emit(
                    Event(
                        kind="error",
                        run_id=state.run_id,
                        turn=state.turn,
                        payload={"type": "UnexpectedError", "message": str(exc)},
                    )
                )
                result = Result(
                    run_id=state.run_id,
                    session_id=session.session_id,
                    status="failed",
                    final_message=None,
                    turn_count=state.turn,
                    error=wrapped,
                )

            await self._run_lifecycle_after_run(
                state=state,
                session=session,
                result=result,
                lifecycle_hooks=effective_lifecycle_hooks,
            )
            await self._emit(
                Event(
                    kind="run.end",
                    run_id=state.run_id,
                    turn=state.turn,
                    payload={
                        "status": result.status,
                        "turn_count": result.turn_count,
                        # run-end-reason-bitmask：结束原因 bitmask（错误分类器真源）。
                        # 消费侧（thread-status / 队列 drain / 前端按钮）读此 int，
                        # 不再各自 isinstance(result.error) 推导。详见 RunEndReason。
                        "run_end_reason": int(compute_run_end_reason(result)),
                    },
                )
            )

            # 收尾吐回：completed / cancelled / failed 三条分支在此汇合。close buffer
            # 拒绝后续 steer（同步段内，与 steer() 互斥，无撕裂）；drain 后仍残留的
            # 文本（run 已进入最后 turn 不再 drain，或收尾竞态注不进）写回 Result.metadata。
            # Result 是 frozen dataclass，但 metadata 是可变 dict，直接赋值合法。
            # steer_undelivered 每项带 pending_input_id，消费端按 id 精确复用 claim，
            # 不再用 content 字符串撞匹配。
            my_buffer.closed = True
            if my_buffer.items:
                result.metadata["steer_undelivered"] = [
                    {"text": item.text, "pending_input_id": item.pending_input_id}
                    for item in my_buffer.items
                ]
            return result
        finally:
            _RUN_EVENT_SINKS.reset(event_sink_token)
            _RUN_AGENT_ID.reset(agent_id_token)
            # 身份比对移除：只有当 dict 里当前 key 仍是 my_buffer 本身时才 pop，
            # 避免误删并发新 run（若真发生）注册的同 key buffer。
            if self._steer_buffers.get(sid) is my_buffer:
                del self._steer_buffers[sid]

    # ------------------------------------------------------------------
    # 内部流程
    # ------------------------------------------------------------------

    def _resolve_tool_context_metadata(
        self,
        override: Mapping[str, Any] | None,
        *,
        parent_agent: Mapping[str, Any] | None = None,
        thread_id: str | None,
        session_id: str,
    ) -> dict[str, Any]:
        """合并上下文并冻结审批本子所属的顶层 thread 键。"""
        merged = dict(self._tool_context_metadata)
        if override:
            merged.update(dict(override))
        if thread_id is not None and thread_id.strip():
            merged["thread_id"] = thread_id
        else:
            # scheduler 等旧调用方可能已在装配 metadata 中提供专属 thread；
            # 普通主链缺省时使用稳定 session id。
            merged.setdefault("thread_id", session_id)
        if parent_agent is not None:
            merged["parent_agent"] = dict(parent_agent)
        return merged

    def _resolve_tools(
        self,
        agent_spec: AgentSpec,
        tools: ToolLookup,
        enabled_tools: Sequence[Tool] | None,
    ) -> list[Tool]:
        """按 AgentSpec.tool_names 从 ToolLookup 中解析 Tool 列表。"""
        if enabled_tools is not None:
            return list(enabled_tools)
        resolved: list[Tool] = []
        for name in agent_spec.tool_names:
            if name not in tools:
                raise AgentError(
                    f"tool {name!r} declared in agent_spec.tool_names is not available in the "
                    "provided ToolLookup",
                    details={"tool_name": name, "agent": agent_spec.name},
                )
            resolved.append(tools[name])
        return resolved

    async def _seed_messages(
        self,
        session: Session,
        agent_spec: AgentSpec,
        user_input: str,
        state: RunState,
        *,
        attachments: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
    ) -> None:
        """如果 session 里还没有 system 指令，就先把 AgentSpec.instructions 注入。

        当 ``input_assembler`` 已注入时，system 注入职责移交给 assembler，
        这里只写 user 首条消息，避免双重 system 注入。

        本方法承担两件事：
        1. user message append（必做）
        2. ``session.advance_run_index()`` 拼装 ``state.run_id``（仅当外部未注入 run_id 时）

        ``attachments`` / ``references`` 非 None 时透传到 ``Message.metadata``；
        全链路保持 dict 形态，避免 provider / assembler 处理 BaseModel 与 dict
        双形态。
        """
        if self._input_assembler is None:
            # 旧路径：由 _seed_messages 自己写 system 消息（兼容 fallback）。
            existing = await session.history()
            has_system = any(m.role == "system" for m in existing)
            if not has_system and agent_spec.instructions:
                system_msg = Message.system(agent_spec.instructions)
                await session.append(system_msg)
                state.record(system_msg)
        # 始终写入 user 消息。结构化输入非 None 时写到 metadata。
        user_metadata: dict[str, Any] = {}
        if attachments:
            user_metadata["attachments"] = attachments
        if references:
            user_metadata["conversation_references"] = references
        user_metadata_or_none = user_metadata or None
        user_msg = Message.user(user_input, metadata=user_metadata_or_none)
        await session.append(user_msg)
        # 紧跟 user message append 调 advance_run_index，把"用户消息入历史"和
        # "run 编号递增"绑到同一时机。
        #
        # 非事务原子约定（v0.x 简化）：append + advance 分两步执行，不构成单事务。
        # 失败模式：
        #   - append 成功 + advance 失败 → 下次启动 manifest run_count 不变，
        #     新一轮 advance 拿到的 run_index 复用上次值；但本 run 已崩溃未生成
        #     run_id 持久记录，新一轮 run_id 仍唯一不撞，不丢消息也不重号。
        #   - advance 成功 + append 失败（极罕见） → jsonl 少一条，run_count 跳号，
        #     仍唯一不撞。
        # 不会丢消息也不让 run_id 重复。事务化留 v0.2+。
        #
        # 外部注入 run_id（state.run_id 非空，多见于测试 fixture）时跳过 advance，
        # 保持外部传入的标识不变。
        if not state.run_id:
            run_index = await session.advance_run_index()
            state.run_id = f"run-{session.session_id}-{run_index}"
        state.record(user_msg)

    async def _claim_last_user_message(self, session: Session, state: RunState) -> None:
        """Claim an already persisted trailing user message as this run's input."""
        history = await session.history()
        if not history:
            raise AgentError(
                "cannot continue run without an existing user message",
                details={"session_id": session.session_id},
            )
        user_msg = history[-1]
        if user_msg.role != "user":
            raise AgentError(
                "cannot continue run because the latest message is not a user message",
                details={
                    "session_id": session.session_id,
                    "latest_role": user_msg.role,
                },
            )
        if not state.run_id:
            run_index = await session.advance_run_index()
            state.run_id = f"run-{session.session_id}-{run_index}"
        state.record(user_msg)

    async def _drive_turns(
        self,
        *,
        state: RunState,
        session: Session,
        agent_spec: AgentSpec,
        llm: LLMProvider,
        tools_by_name: dict[str, Tool],
        resolved_tools: list[Tool],
        approval: ApprovalProvider,
        max_turns: int,
        lifecycle_hooks: Sequence[LifecycleHook],
        tool_context_metadata: Mapping[str, Any],
        max_tokens: int | None,
        temperature: float | None,
        timeout_seconds: float | None,
        llm_request_metadata: Mapping[str, Any] | None,
        event_context: _RunEventContext,
        llm_tool_call_contract: LLMToolCallContract | None,
    ) -> tuple[Message | None, ProviderUsageSnapshot | None]:
        """核心 turn 循环。返回 (最终 assistant 消息, 累计 usage)。"""
        final_assistant: Message | None = None
        request_usage_snapshots: list[ProviderUsageSnapshot] = []

        while True:
            if state.turn >= max_turns:
                raise MaxTurnsExceededError(
                    f"exceeded max_turns={max_turns} without reaching a terminal response",
                    details={"run_id": state.run_id, "max_turns": max_turns},
                )

            state.advance_turn()
            await self._emit(
                _build_turn_event(
                    kind="turn.start",
                    phase=_TurnEventPhase.START,
                    state=state,
                    session=session,
                    event_context=event_context,
                )
            )
            await self._run_lifecycle_before_turn(state, lifecycle_hooks)

            # steer 注入点：drain 本 session 的补充输入 buffer，逐条 append 成 user
            # 消息落进 session 后再读 history。此点位于上一 turn 的 tool_result 全部
            # 落盘之后（tool 执行、回填都在上一轮循环尾部完成），因此注入 user 消息
            # 不会插到某个 tool_use 与其 tool_result 之间，天然不破坏 tool_use↔
            # tool_result 配对（约束16 的协议要求）。
            await self._drain_steer_buffer(state, session)

            history = await session.history()

            if self._input_assembler is not None:
                # 新路径：InputAssembler 接管 compact + system 注入。
                instruction_sources = self._instruction_sources_for(agent_spec)
                assembled = await self._input_assembler.assemble(
                    history,
                    instruction_sources,
                )
                prepared_messages = assembled.messages
                if self._prompt_debug_sink is not None:
                    system_message = getattr(assembled, "system_message", None)
                    self._prompt_debug_sink.dump(
                        session_id=session.session_id,
                        run_id=state.run_id,
                        turn=state.turn,
                        model=agent_spec.default_model,
                        instruction_origins=self._instruction_origins,
                        history_before_assemble=history,
                        assembled_messages=prepared_messages,
                        metadata=assembled.metadata,
                        added_system_prompt=(
                            system_message.content if system_message is not None else None
                        ),
                    )
                # 把 assembler metadata 映射成 compact_meta 格式（若有压缩）。
                original_count = assembled.metadata.get("original_count", len(history))
                compacted_count = assembled.metadata.get("compacted_count", len(prepared_messages))
                if compacted_count != original_count:
                    compact_meta: dict[str, Any] | None = {
                        "original_count": original_count,
                        "compacted_count": compacted_count,
                        "dropped_count": original_count - compacted_count,
                    }
                else:
                    compact_meta = None
            else:
                # 旧路径：_prepare_messages 做 compact，system 已由 _seed_messages 注入。
                prepared_messages, compact_meta = await self._prepare_messages(history)

            if compact_meta is not None:
                await self._emit(
                    Event(
                        kind="history.compact",
                        run_id=state.run_id,
                        turn=state.turn,
                        payload=compact_meta,
                    )
                )
            request_messages = list(prepared_messages)
            # claude-image-paste-e2e §5：透传顶层 ``thread_id`` 到 provider，供
            # :class:`infrastructure.llm_providers.anthropic_messages.AnthropicMessagesProvider`
            # 还原附件物理路径(``.kongming/web/uploads/images/<thread_id>/<asset_id>.<ext>``)。
            # CLI 缺省时 thread_id 回落稳定 session id，且无附件路径副作用。
            request_metadata: dict[str, Any] = {}
            if llm_request_metadata:
                request_metadata.update(dict(llm_request_metadata))
            # thread_id 已在 run 边界冻结；provider 参数不得把子 session 覆盖回来。
            request_metadata["thread_id"] = str(tool_context_metadata["thread_id"])
            base_llm_request = LLMRequest(
                model=agent_spec.default_model,
                messages=tuple(request_messages),
                tools=tuple(resolved_tools),
                temperature=temperature,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
                metadata=request_metadata,
                reasoning_effort=agent_spec.reasoning_effort,
            )
            response: LLMResponse
            correction_message: Message | None = None
            max_attempts = 1 + (
                llm_tool_call_contract.correction_retries
                if llm_tool_call_contract is not None
                else 0
            )
            for attempt_index in range(max_attempts):
                llm_request = base_llm_request
                if correction_message is not None:
                    llm_request = replace(
                        base_llm_request,
                        messages=(*base_llm_request.messages, correction_message),
                    )
                request_payload = {
                    "request": _llm_request_to_event_payload(llm_request),
                    "model": llm_request.model,
                    "message_count": len(llm_request.messages),
                    "tool_count": len(llm_request.tools),
                    "original_message_count": len(history),
                    "tool_contract_attempt": attempt_index + 1,
                }
                await self._emit(
                    Event(
                        kind="llm.request",
                        run_id=state.run_id,
                        turn=state.turn,
                        payload=request_payload,
                    )
                )

                violation: _ToolCallContractViolation | None = None
                violation_event_emitted = False
                try:
                    if self._stream_enabled and isinstance(llm, SupportsLLMStream):
                        response = await self._consume_stream(
                            llm,
                            llm_request,
                            run_id=state.run_id,
                            turn=state.turn,
                            llm_tool_call_contract=llm_tool_call_contract,
                            contract_attempt=attempt_index + 1,
                            contract_should_retry=attempt_index + 1 < max_attempts,
                        )
                    else:
                        response = await self._safe_llm_complete(llm, llm_request)
                        violation = self._validate_tool_call_contract(
                            response.message,
                            llm_request,
                            llm_tool_call_contract,
                        )
                except _ToolCallContractViolationSignal as exc:
                    violation = exc.violation
                    violation_event_emitted = exc.event_emitted

                if violation is not None:
                    should_retry = attempt_index + 1 < max_attempts
                    if not violation_event_emitted:
                        await self._emit_tool_call_contract_violation(
                            run_id=state.run_id,
                            turn=state.turn,
                            request=llm_request,
                            violation=violation,
                            attempt=attempt_index + 1,
                            should_retry=should_retry,
                        )
                    if should_retry:
                        correction_message = self._build_tool_call_contract_correction(
                            request=llm_request,
                            violation=violation,
                        )
                        continue
                    raise LLMToolCallContractError(
                        "LLM response violated the run tool-call contract",
                        details={
                            "violation_kind": violation.kind.value,
                            "tool_name": violation.tool_name,
                            "tool_call_id": violation.tool_call_id,
                            "tool_index": violation.tool_index,
                            "attempt": attempt_index + 1,
                        },
                    )

                # 非流式路径：合同预校验通过后才允许对外发内容。
                if (
                    not (self._stream_enabled and isinstance(llm, SupportsLLMStream))
                    and response.message.content
                ):
                    await self._emit(
                        Event(
                            kind="content.delta",
                            run_id=state.run_id,
                            turn=state.turn,
                            payload={"delta": response.message.content, "seq": 0},
                        )
                    )
                break
            assistant_message = response.message
            await session.append(assistant_message, usage=response.usage)
            state.record(assistant_message)

            response_payload = {
                "response": _llm_response_to_event_payload(response),
                "finish_reason": response.finish_reason,
                "has_tool_calls": bool(assistant_message.tool_calls),
                "usage": (response.usage.to_payload() if response.usage is not None else None),
                "provider_metadata": dict(response.provider_metadata),
            }
            await self._emit(
                Event(
                    kind="llm.response",
                    run_id=state.run_id,
                    turn=state.turn,
                    payload=response_payload,
                )
            )

            if response.usage is not None:
                await self._emit(
                    Event(
                        kind="usage",
                        run_id=state.run_id,
                        turn=state.turn,
                        payload=response.usage.to_payload(),
                    )
                )
                request_usage_snapshots.append(response.usage)

            tool_calls = assistant_message.tool_calls or ()
            await self._run_lifecycle_after_turn(state, assistant_message, lifecycle_hooks)
            await self._emit(
                _build_turn_event(
                    kind="turn.end",
                    phase=_TurnEventPhase.END,
                    state=state,
                    session=session,
                    event_context=event_context,
                    assistant_message=assistant_message,
                    finish_reason=response.finish_reason,
                    history_index=len(history),
                )
            )

            if not tool_calls:
                # 视为终止：无论 finish_reason 是 stop / length / other，核心只看 tool_calls。
                final_assistant = assistant_message
                break

            # 有 tool_calls：依次执行、回填
            await self._execute_tool_calls(
                state=state,
                session=session,
                approval=approval,
                tool_calls=tool_calls,
                tools_by_name=tools_by_name,
                lifecycle_hooks=lifecycle_hooks,
                tool_context_metadata=tool_context_metadata,
            )
            if (
                llm_tool_call_contract is not None
                and llm_tool_call_contract.mode is LLMToolCallContractMode.DECLARED_EXACTLY_ONCE
            ):
                final_assistant = assistant_message
                break
            # 回填后继续下一个 turn

        return final_assistant, aggregate_provider_usage_snapshots(
            tuple(request_usage_snapshots),
            scope=ProviderUsageScope.RUN,
        )

    async def _drain_steer_buffer(self, state: RunState, session: Session) -> None:
        """把本 session steer buffer 里的补充输入全部注入当前 run。

        由 :meth:`_drive_turns` 在每个 turn 开头、``session.history()`` 之前调用。
        取走 buffer 全部 SteerRequest（不删 buffer 本身，run 期间持续复用），逐条：
        1. ``session.append(Message.user(item.text))`` —— 落进历史，下一次 LLM 请求可见；
        2. ``state.record(msg)`` —— 同步进 RunState 记录；
        3. emit ``steer.injected`` 事件（payload 带 ``pending_input_id`` 消账主键 +
           ``content_length`` 纯观测字段，不带明文）。

        关键输入：``state``（提供 run_id/turn）、``session``（提供 session_id 定位
        buffer + append 落盘）。无返回值。buffer 不存在（不该发生，run 期间必存在）
        或为空时直接返回，无副作用。
        """
        buffer = self._steer_buffers.get(session.session_id)
        if buffer is None or not buffer.items:
            return
        # 一次性取走当前所有 items，清空 buffer（不动 closed，run 未收尾仍可继续 steer）。
        pending = buffer.items
        buffer.items = []
        for item in pending:
            msg = Message.user(item.text)
            await session.append(msg)
            state.record(msg)
            await self._emit(
                Event(
                    kind="steer.injected",
                    run_id=state.run_id,
                    turn=state.turn,
                    payload={
                        "pending_input_id": item.pending_input_id,
                        "content_length": len(item.text),
                    },
                )
            )

    async def _prepare_messages(
        self,
        history: Sequence[Message],
    ) -> tuple[list[Message], dict[str, Any] | None]:
        """把 history 过一道 ``MessageCompactor``；没有则原样透传。

        Returns:
            ``(messages, meta)``。``meta`` 只在真正发生压缩（原始长度 != 压缩后
            长度）时为 non-None，便于 runner 决定是否 emit ``history.compact``
            事件；原样透传时返回 ``None`` 保持事件流安静。
        """
        if self._message_compactor is None:
            return list(history), None

        try:
            compacted = await self._message_compactor.compact(history)
        except Exception as exc:
            # compactor 失败不应拖垮主链路；记一条错误事件，然后原样透传。
            await self._emit(
                Event(
                    kind="error",
                    run_id="unknown",
                    payload={
                        "source": "message_compactor",
                        "exception_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
            )
            return list(history), None

        original = len(history)
        compacted_count = len(compacted)
        if compacted_count == original:
            return compacted, None
        meta: dict[str, Any] = {
            "original_count": original,
            "compacted_count": compacted_count,
            "dropped_count": original - compacted_count,
        }
        return compacted, meta

    def _instruction_sources_for(self, agent_spec: AgentSpec) -> Sequence[PromptSource]:
        """解析单次 run 指令，输入为 agent spec，输出 assembler 使用的来源列表。"""
        instructions = agent_spec.instructions.strip()
        if not instructions:
            return self._instruction_sources
        if not self._instruction_sources:
            return [_RunInstructionSource(origin="", content=agent_spec.instructions)]
        if (
            len(self._instruction_sources) == 1
            and self._instruction_sources[0].origin == ""
            and self._instruction_sources[0].content != agent_spec.instructions
        ):
            return [_RunInstructionSource(origin="", content=agent_spec.instructions)]
        return self._instruction_sources

    async def _safe_llm_complete(self, llm: LLMProvider, request: LLMRequest) -> LLMResponse:
        """包装 provider 调用异常为 :class:`ProviderError`。"""
        try:
            return await llm.complete(request)
        except AgentError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"LLM provider call failed: {exc!r}",
                details={"exception_type": type(exc).__name__},
            ) from exc

    def _validate_tool_call_contract(
        self,
        message: Message,
        request: LLMRequest,
        contract: LLMToolCallContract | None,
    ) -> _ToolCallContractViolation | None:
        """在任何 session/tool 副作用前校验完整 assistant 响应。"""
        if contract is None:
            return None
        calls = tuple(message.tool_calls or ())
        allowed_tool_names = {tool.name for tool in request.tools}
        for index, call in enumerate(calls):
            if call.tool_name not in allowed_tool_names:
                return _ToolCallContractViolation(
                    kind=LLMToolCallViolationKind.UNDECLARED_TOOL,
                    tool_name=call.tool_name,
                    tool_call_id=call.call_id,
                    tool_index=index,
                    observed_tool_call_count=len(calls),
                )
            if index >= 1:
                return _ToolCallContractViolation(
                    kind=LLMToolCallViolationKind.TOOL_CALL_LIMIT_EXCEEDED,
                    tool_name=call.tool_name,
                    tool_call_id=call.call_id,
                    tool_index=index,
                    observed_tool_call_count=len(calls),
                )
        if not calls:
            return _ToolCallContractViolation(
                kind=LLMToolCallViolationKind.MISSING_REQUIRED_TOOL_CALL,
                tool_name=None,
                tool_call_id=None,
                tool_index=None,
                observed_tool_call_count=0,
            )
        return None

    def _validate_started_tool_call(
        self,
        *,
        request: LLMRequest,
        contract: LLMToolCallContract | None,
        tool_name: str | None,
        tool_call_id: str | None,
        tool_index: int,
        observed_tool_call_count: int,
    ) -> _ToolCallContractViolation | None:
        """在流的 ``tool_call.start`` 到达时执行可提前判定的合同检查。"""
        if contract is None:
            return None
        allowed_tool_names = {tool.name for tool in request.tools}
        if tool_name not in allowed_tool_names:
            return _ToolCallContractViolation(
                kind=LLMToolCallViolationKind.UNDECLARED_TOOL,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_index=tool_index,
                observed_tool_call_count=observed_tool_call_count,
            )
        if observed_tool_call_count > 1:
            return _ToolCallContractViolation(
                kind=LLMToolCallViolationKind.TOOL_CALL_LIMIT_EXCEEDED,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_index=tool_index,
                observed_tool_call_count=observed_tool_call_count,
            )
        return None

    async def _emit_tool_call_contract_violation(
        self,
        *,
        run_id: str,
        turn: int,
        request: LLMRequest,
        violation: _ToolCallContractViolation,
        attempt: int,
        should_retry: bool,
    ) -> None:
        """发出脱敏违规坐标；payload 排除参数、正文和完整 transcript。"""
        await self._emit(
            Event(
                kind="llm.tool_call.contract_violation",
                run_id=run_id,
                turn=turn,
                payload={
                    "attempt": attempt,
                    "violation_kind": violation.kind.value,
                    "tool_name": violation.tool_name,
                    "tool_call_id": violation.tool_call_id,
                    "tool_index": violation.tool_index,
                    "allowed_tool_names": sorted(tool.name for tool in request.tools),
                    "observed_tool_call_count": violation.observed_tool_call_count,
                    "action": "retry" if should_retry else "fail",
                },
            )
        )

    def _build_tool_call_contract_correction(
        self,
        *,
        request: LLMRequest,
        violation: _ToolCallContractViolation,
    ) -> Message:
        """构造只进入下一次 LLM 请求的瞬态纠错消息。"""
        allowed = ", ".join(sorted(tool.name for tool in request.tools))
        offending = violation.tool_name or "(missing)"
        return Message.user(
            "上一条响应违反工具调用合同。"
            f"违规类型：{violation.kind.value}；涉及工具：{offending}。"
            f"当前允许工具：{allowed}。"
            "请停止生成其他工具调用，并且只调用一次当前允许的工具。"
        )

    async def _consume_stream(
        self,
        llm: SupportsLLMStream,
        request: LLMRequest,
        *,
        run_id: str,
        turn: int,
        llm_tool_call_contract: LLMToolCallContract | None,
        contract_attempt: int,
        contract_should_retry: bool,
    ) -> LLMResponse:
        """消费 :class:`SupportsLLMStream` 的 chunk 流，返回等价 :class:`LLMResponse`。

        与 :meth:`_safe_llm_complete` 行为等价：上层（``_drive_turns``）在拿到
        ``LLMResponse`` 之后的 tool_call / approval / session.append 流程完全不
        受流式/非流式影响。

        中间会向 ``EventSink`` 发射四类事件：

        - ``llm.chunk.first``：首个非空 chunk 抵达时 emit 一次（用于 TTFT 度量）
        - ``content.delta`` / ``reasoning.delta``：每个增量 emit
        - ``llm.stream.end``：流终止时（成功或异常）汇总 emit 一次

        其它语义：

        - ``tool_call.*`` chunk 在 runner 内累积，**不**直接 emit 为 EventSink 事件
          （CLI 不渲染中间状态；最终的 ``ToolCall`` 来自 ``message.done``）
        - 出现 ``tool_call.start`` 后若 ``suppress_content_after_tool_call=True``，
          后续 ``content.delta`` 不再 emit；但 session 仍保留完整 message（来自
          ``message.done``）
        - 流非正常结束（无 ``message.done``）→ 抛 :class:`ProviderError`
        """
        started_ns = time.monotonic_ns()
        first_chunk_emitted = False
        tool_call_seen = False
        chunk_count = 0
        content_chars = 0
        reasoning_chars = 0
        tool_call_count = 0
        truncated_args = False

        final_message: Message | None = None
        final_finish_reason: FinishReason = "other"
        final_usage: ProviderUsageSnapshot | None = None
        final_provider_metadata: dict[str, Any] = {}
        deferred_response_events: list[Event] = []

        response_stream = llm.stream(request)
        try:
            if llm_tool_call_contract is not None and not isinstance(
                response_stream, _SupportsAsyncClose
            ):
                raise ProviderError(
                    "strict LLM tool-call contract requires a closable response stream",
                    details={"run_id": run_id, "turn": turn},
                )
            async for chunk in response_stream:
                chunk_count += 1

                # TTFT：首个非空 chunk 抵达时 emit 一次（不在 message.done 上触发，
                # 因为 message.done 是终态汇总，不算"首个内容到达"）
                if not first_chunk_emitted and chunk.kind != "message.done":
                    elapsed_ms = (time.monotonic_ns() - started_ns) // 1_000_000
                    await self._emit(
                        Event(
                            kind="llm.chunk.first",
                            run_id=run_id,
                            turn=turn,
                            payload={"elapsed_ms": elapsed_ms, "model": request.model},
                        )
                    )
                    first_chunk_emitted = True

                kind = chunk.kind
                if kind == "content.delta":
                    content_chars += len(chunk.delta)
                    if tool_call_seen and self._suppress_content_after_tool_call:
                        # suppression：tool_call 后的 content 不 emit
                        continue
                    content_event = Event(
                        kind="content.delta",
                        run_id=run_id,
                        turn=turn,
                        payload={"delta": chunk.delta, "index": chunk.index},
                    )
                    if llm_tool_call_contract is None:
                        await self._emit(content_event)
                    else:
                        deferred_response_events.append(content_event)
                elif kind == "reasoning.delta":
                    reasoning_chars += len(chunk.delta)
                    reasoning_event = Event(
                        kind="reasoning.delta",
                        run_id=run_id,
                        turn=turn,
                        payload={"delta": chunk.delta},
                    )
                    if llm_tool_call_contract is None:
                        await self._emit(reasoning_event)
                    else:
                        deferred_response_events.append(reasoning_event)
                elif kind == "tool_call.start":
                    tool_call_seen = True
                    tool_call_count += 1
                    violation = self._validate_started_tool_call(
                        request=request,
                        contract=llm_tool_call_contract,
                        tool_name=chunk.tool_name,
                        tool_call_id=chunk.tool_call_id,
                        tool_index=chunk.index,
                        observed_tool_call_count=tool_call_count,
                    )
                    if violation is not None:
                        raise _ToolCallContractViolationSignal(violation)
                    # 不 emit（runner 内部累积；最终 ToolCall 由 message.done 提供）
                elif kind in ("tool_call.arguments.delta", "tool_call.end"):
                    # 内部累积；不 emit
                    pass
                elif kind == "message.done":
                    if chunk.message is None:
                        raise ProviderError(
                            "stream message.done chunk missing message",
                            details={"run_id": run_id, "turn": turn},
                        )
                    final_message = chunk.message
                    final_finish_reason = chunk.finish_reason or "other"
                    final_usage = chunk.usage
                    final_provider_metadata = dict(chunk.provider_metadata)
                    # parser 把 JSON 截断降级为 length；此处记一下供 stream.end payload
                    if final_finish_reason == "length":
                        truncated_args = True
                    violation = self._validate_tool_call_contract(
                        final_message,
                        request,
                        llm_tool_call_contract,
                    )
                    if violation is not None:
                        raise _ToolCallContractViolationSignal(violation)
                # 其它未知 kind：忽略，不破坏主链路

            if final_message is None:
                raise ProviderError(
                    "stream ended without message.done chunk",
                    details={"run_id": run_id, "turn": turn, "chunk_count": chunk_count},
                )
            for deferred_event in deferred_response_events:
                await self._emit(deferred_event)

        except _ToolCallContractViolationSignal as exc:
            await self._emit_tool_call_contract_violation(
                run_id=run_id,
                turn=turn,
                request=request,
                violation=exc.violation,
                attempt=contract_attempt,
                should_retry=contract_should_retry,
            )
            exc.event_emitted = True
            raise
        except AgentError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"LLM stream consumption failed: {exc!r}",
                details={
                    "exception_type": type(exc).__name__,
                    "run_id": run_id,
                    "turn": turn,
                },
            ) from exc
        finally:
            if isinstance(response_stream, _SupportsAsyncClose):
                try:
                    async with asyncio.timeout(_STREAM_CLOSE_TIMEOUT_SECONDS):
                        await response_stream.aclose()
                except TimeoutError:
                    _logger.error(
                        "LLM response stream close timed out after %.1fs: run=%s turn=%d",
                        _STREAM_CLOSE_TIMEOUT_SECONDS,
                        run_id,
                        turn,
                    )
                except Exception:
                    _logger.exception(
                        "LLM response stream close failed: run=%s turn=%d",
                        run_id,
                        turn,
                    )
            # 终态汇总（流终止 = 成功 or 异常都 emit 一次）
            await self._emit(
                Event(
                    kind="llm.stream.end",
                    run_id=run_id,
                    turn=turn,
                    payload={
                        "chunk_count": chunk_count,
                        "finish_reason": (
                            final_finish_reason if final_message is not None else "error"
                        ),
                        "content_chars": content_chars,
                        "reasoning_chars": reasoning_chars,
                        "tool_call_count": tool_call_count,
                        "truncated_args": truncated_args,
                    },
                )
            )

        return LLMResponse(
            message=final_message,
            finish_reason=final_finish_reason,
            usage=final_usage,
            provider_metadata=final_provider_metadata,
        )

    async def _execute_tool_calls(
        self,
        *,
        state: RunState,
        session: Session,
        approval: ApprovalProvider,
        tool_calls: Iterable[ToolCall],
        tools_by_name: dict[str, Tool],
        lifecycle_hooks: Sequence[LifecycleHook],
        tool_context_metadata: Mapping[str, Any],
    ) -> None:
        """串行执行 assistant 消息携带的所有 tool_call，把结果回填到 session。

        **interrupt-run-v0.1**：每个 call 用单独 try 包裹，捕获
        :class:`asyncio.CancelledError` 后：

        1. 给当前正在跑的 call 写一条 ``[interrupted]`` tool_result 占位
           （``is_error=True``、``interrupted=True`` metadata）；
        2. 给同一 assistant 消息里**尚未起跑**的剩余 call 也补占位 —— Anthropic
           协议要求 "同一条 assistant 消息所有 tool_use 必须对应 tool_result"，
           少一条下次 LLM 调用会被服务端 400 拒掉；
        3. 把当前被打断的 call_id 写入 ``state.metadata["cancelled_tool_call_id"]``
           供 runner 顶层 except 读出来塞进 Result.metadata；
        4. 重新 raise 让 runner 顶层接 → ``Result(status="cancelled")``。

        其它异常（AgentError / 普通 Exception）由 ``_safe_tool_execute`` 包成
        :class:`ToolError`，不在本方法 except 范围；走原 ``tool_err`` 分支。
        """
        # 物化为 list：cancel 时需要知道 "剩余多少个 call 没起跑"。
        calls_list = list(tool_calls)
        for idx, call in enumerate(calls_list):
            try:
                await self._execute_single_tool_call(
                    state=state,
                    session=session,
                    approval=approval,
                    call=call,
                    tools_by_name=tools_by_name,
                    lifecycle_hooks=lifecycle_hooks,
                    tool_context_metadata=tool_context_metadata,
                )
            except asyncio.CancelledError:
                # 1. 当前 call 占位（覆盖本 call 的任何中间 await 被打断的情形）
                await self._finalize_unpaired_call(
                    state=state,
                    session=session,
                    call=call,
                    reason="user_interrupt",
                )
                state.metadata["cancelled_tool_call_id"] = call.call_id
                # 2. 剩余未起跑的 call 也占位
                for remaining in calls_list[idx + 1 :]:
                    await self._finalize_unpaired_call(
                        state=state,
                        session=session,
                        call=remaining,
                        reason="user_interrupt_pending",
                    )
                # 3. 透传给 runner 顶层 except CancelledError
                raise

    async def _execute_single_tool_call(
        self,
        *,
        state: RunState,
        session: Session,
        approval: ApprovalProvider,
        call: ToolCall,
        tools_by_name: dict[str, Tool],
        lifecycle_hooks: Sequence[LifecycleHook],
        tool_context_metadata: Mapping[str, Any],
    ) -> None:
        """执行单个 tool_call。从原 ``_execute_tool_calls`` 循环体抽出。

        把"单 call 全流程"独立成方法的目的：让外层 for 循环只需 try 一次就能
        统一处理 cancel；同时 lifecycle hook / approval / 执行 / 回填 的顺序
        与原版完全一致，没有行为变化。
        """
        await self._run_lifecycle_before_tool(state, call, lifecycle_hooks)
        await self._emit(
            Event(
                kind="tool.call.start",
                run_id=state.run_id,
                turn=state.turn,
                payload={
                    "call_id": call.call_id,
                    "tool_name": call.tool_name,
                },
            )
        )

        tool = tools_by_name.get(call.tool_name)
        if tool is None:
            registry_error = f"tool {call.tool_name!r} not registered"
            unavailable_message = (
                f"工具不可用：{call.tool_name} 当前未在本 session 启用，"
                f"或对应插件已关闭/已卸载。请改用当前可用工具。（{registry_error}）"
            )
            result_message = self._build_tool_error_message(
                call,
                unavailable_message,
                metadata={
                    "error_message": registry_error,
                    "unavailable": True,
                    "reason": "tool_unavailable",
                },
            )
            await session.append(result_message)
            state.record(result_message)
            await self._emit(
                Event(
                    kind="tool.call.end",
                    run_id=state.run_id,
                    turn=state.turn,
                    payload={
                        "call_id": call.call_id,
                        "tool_name": call.tool_name,
                        "ok": False,
                        "reason": "tool_unavailable",
                        "content": "",
                        "data": None,
                        "error_message": registry_error,
                    },
                )
            )
            await self._run_lifecycle_after_tool(state, call, result_message, lifecycle_hooks)
            return

        ctx = ToolContext(
            run_id=state.run_id,
            session_id=state.session_id,
            turn=state.turn,
            call_id=call.call_id,
            metadata=dict(tool_context_metadata),
            agent_id=state.agent_id,
        )
        try:
            prepared = (
                tool.prepare(deepcopy(call.arguments), ctx)
                if isinstance(tool, ToolCallPreparer)
                else PreparedToolCall(
                    arguments=deepcopy(call.arguments),
                    execution_scope=ToolExecutionScope(),
                )
            )
        except Exception as exc:
            error_message = f"tool preparation failed: {exc}"
            preparation_details = dict(exc.details) if isinstance(exc, AgentError) else {}
            result_message = self._build_tool_error_message(
                call,
                error_message,
                metadata={
                    "error_type": type(exc).__name__,
                    "reason": "tool_preparation_failed",
                    "preparation_error": preparation_details,
                },
            )
            await session.append(result_message)
            state.record(result_message)
            await self._emit(
                Event(
                    kind="tool.call.end",
                    run_id=state.run_id,
                    turn=state.turn,
                    payload={
                        "call_id": call.call_id,
                        "tool_name": call.tool_name,
                        "ok": False,
                        "reason": "tool_preparation_failed",
                        "content": "",
                        "data": None,
                        "error_message": error_message,
                        "preparation_error": preparation_details,
                    },
                )
            )
            await self._run_lifecycle_after_tool(state, call, result_message, lifecycle_hooks)
            return

        # 审批：runner 不判 allow/deny，只咨询 ApprovalProvider
        approval_prepared = deepcopy(prepared)
        execution_prepared = deepcopy(prepared)
        approval_request = ApprovalRequest(
            run_id=state.run_id,
            session_id=state.session_id,
            turn=state.turn,
            call_id=call.call_id,
            tool_name=call.tool_name,
            arguments=approval_prepared.arguments,
            execution_scope=approval_prepared.execution_scope,
            metadata=dict(tool_context_metadata),
        )
        await self._emit(
            Event(
                kind="approval.request",
                run_id=state.run_id,
                turn=state.turn,
                payload={"call_id": call.call_id, "tool_name": call.tool_name},
            )
        )
        previous_status = state.status
        state.mark_waiting_approval()
        decision = await approval.decide(approval_request)
        state.status = previous_status  # 恢复到之前的 running 状态
        await self._emit(
            Event(
                kind="approval.decision",
                run_id=state.run_id,
                turn=state.turn,
                payload={
                    "call_id": call.call_id,
                    "tool_name": call.tool_name,
                    "outcome": decision.outcome,
                    "reason": decision.reason,
                },
            )
        )

        if not decision.approved:
            reason = decision.reason or decision.outcome
            error_message = f"approval {decision.outcome}: {reason}"
            rejected_msg = self._build_tool_error_message(
                call,
                error_message,
                metadata={"approval_outcome": decision.outcome},
            )
            await session.append(rejected_msg)
            state.record(rejected_msg)
            await self._emit(
                Event(
                    kind="tool.call.end",
                    run_id=state.run_id,
                    turn=state.turn,
                    payload={
                        "call_id": call.call_id,
                        "tool_name": call.tool_name,
                        "ok": False,
                        "reason": f"approval_{decision.outcome}",
                        "content": "",
                        "data": None,
                        "error_message": error_message,
                    },
                )
            )
            await self._run_lifecycle_after_tool(state, call, rejected_msg, lifecycle_hooks)
            # 审批拒绝不直接终止 run；把"被拒绝"这条事实喂回模型，
            # 由模型决定下一步。这样 safety 的策略层可以通过文本说明 / reason
            # 让模型调整计划，而不需要立刻结束 run。
            return

        # 真正执行工具
        tool_result, tool_err = await self._safe_tool_execute(
            tool,
            call,
            ctx,
            prepared=execution_prepared,
        )
        if tool_err is not None:
            result_message = self._build_tool_error_message(
                call,
                tool_err.message,
                metadata={"error_type": type(tool_err).__name__},
            )
        else:
            assert tool_result is not None  # for type checker
            result_message = self._build_tool_result_message(call, tool_result)

        await session.append(result_message)
        state.record(result_message)
        # tool.call.end payload 携带 ToolResult 4 字段（content / data / ok /
        # error_message），让下游 sink（trace / web）拿到工具产出。
        # tool_err 路径走异常分支，content="" + error_message=异常消息。
        if tool_err is not None:
            payload_content = ""
            payload_data: dict[str, Any] | None = None
            payload_error_message: str | None = tool_err.message
        else:
            assert tool_result is not None
            payload_content = tool_result.content
            payload_data = tool_result.data
            payload_error_message = tool_result.error_message
        await self._emit(
            Event(
                kind="tool.call.end",
                run_id=state.run_id,
                turn=state.turn,
                payload={
                    "call_id": call.call_id,
                    "tool_name": call.tool_name,
                    "ok": tool_err is None and (tool_result is None or tool_result.ok),
                    "content": payload_content,
                    "data": payload_data,
                    "error_message": payload_error_message,
                },
            )
        )
        await self._run_lifecycle_after_tool(state, call, result_message, lifecycle_hooks)

    async def _finalize_unpaired_call(
        self,
        *,
        state: RunState,
        session: Session,
        call: ToolCall,
        reason: str,
    ) -> None:
        """给一个未配对的 tool_use 写占位 tool_result + emit tool.call.end。

        触发场景（interrupt-run-v0.1）：

        - ``reason="user_interrupt"``：当前正在跑的 call 被 cancel
        - ``reason="user_interrupt_pending"``：同 assistant 消息里**未起跑**的
          剩余 call —— Anthropic / OpenAI 协议要求所有 tool_use 必须有对应
          tool_result，不补占位下次 LLM 调用会被服务端 400 拒掉

        占位消息 metadata：``{"ok": False, "interrupted": True,
        "interrupt_reason": reason, "error_message": "..."}``，content 为
        JSON 字符串 ``{"error": "[interrupted by user: ...]"}``，
        :meth:`_build_tool_error_message` 复用，与 approval 拒绝等 error 路径
        一致，HistoryCompactor 不会把它过滤掉（``role=="tool"`` 无条件保留）。
        """
        error_text = f"[interrupted by user: {reason}]"
        result_message = self._build_tool_error_message(
            call,
            error_text,
            metadata={"interrupted": True, "interrupt_reason": reason},
        )
        await session.append(result_message)
        state.record(result_message)
        await self._emit(
            Event(
                kind="tool.call.end",
                run_id=state.run_id,
                turn=state.turn,
                payload={
                    "call_id": call.call_id,
                    "tool_name": call.tool_name,
                    "ok": False,
                    "reason": "interrupted",
                    "content": "",
                    "data": None,
                    "error_message": error_text,
                },
            )
        )

    async def _safe_tool_execute(
        self,
        tool: Tool,
        call: ToolCall,
        ctx: ToolContext,
        *,
        prepared: PreparedToolCall,
    ) -> tuple[ToolResult | None, ToolError | None]:
        """执行工具并把异常包成 ToolError。绝不让工具异常穿透主循环。"""
        try:
            result = await tool.execute(prepared, ctx)
            return result, None
        except AgentError as exc:
            # 保留具体 AgentError 语义（例如 ApprovalRejected 从 tool 层意外冒出来）
            if isinstance(exc, ToolError):
                return None, exc
            return None, ToolError(
                f"tool {call.tool_name!r} raised {type(exc).__name__}: {exc.message}",
                details={"call_id": call.call_id, "cause": type(exc).__name__},
            )
        except Exception as exc:
            return None, ToolError(
                f"tool {call.tool_name!r} raised {type(exc).__name__}: {exc}",
                details={"call_id": call.call_id, "exception_type": type(exc).__name__},
            )

    # ------------------------------------------------------------------
    # 消息构造助手
    # ------------------------------------------------------------------

    @staticmethod
    def _build_tool_result_message(call: ToolCall, result: ToolResult) -> Message:
        metadata: dict[str, Any] = {"ok": result.ok}
        if result.data is not None:
            metadata["data"] = result.data
        if result.error_message:
            metadata["error_message"] = result.error_message
        return Message.tool_result(
            tool_call_id=call.call_id,
            content=result.content,
            name=call.tool_name,
            metadata=metadata,
        )

    @staticmethod
    def _build_tool_error_message(
        call: ToolCall,
        error_text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Message:
        meta = {"ok": False, "error_message": error_text}
        if metadata:
            meta.update(metadata)
        # 用中文 JSON 字符串承载错误文本，给下游 provider / 模型明确的失败处理约束。
        content = json.dumps(
            {
                "工具执行失败": True,
                "失败原因": error_text,
                "后续处理要求": [
                    "必须先向用户说明工具执行失败和失败原因。",
                    "禁止声称工具已经成功执行、任务已经完成或产物已经生成。",
                    "禁止编造工具输出、文件路径、报告、子 agent 结果或审计日志。",
                    "需要继续时，先修正参数或请求用户补充信息，再重新调用工具。",
                ],
            },
            ensure_ascii=False,
        )
        return Message.tool_result(
            tool_call_id=call.call_id,
            content=content,
            name=call.tool_name,
            metadata=meta,
        )

    # ------------------------------------------------------------------
    # Lifecycle hook 调用（异常吞到 trace，不污染主链路）
    # ------------------------------------------------------------------

    async def _run_lifecycle_before_turn(
        self,
        state: RunState,
        lifecycle_hooks: Sequence[LifecycleHook],
    ) -> None:
        for hook in lifecycle_hooks:
            before = getattr(hook, "before_turn", None)
            if before is None:
                continue
            try:
                await before(state)
            except Exception as exc:  # pragma: no cover - 防御式
                await self._emit_hook_error("before_turn", exc, state)

    async def _run_lifecycle_after_turn(
        self,
        state: RunState,
        message: Message,
        lifecycle_hooks: Sequence[LifecycleHook],
    ) -> None:
        for hook in lifecycle_hooks:
            after = getattr(hook, "after_turn", None)
            if after is None:
                continue
            try:
                await after(state, message)
            except Exception as exc:  # pragma: no cover - 防御式
                await self._emit_hook_error("after_turn", exc, state)

    async def _run_lifecycle_before_tool(
        self,
        state: RunState,
        call: ToolCall,
        lifecycle_hooks: Sequence[LifecycleHook],
    ) -> None:
        for hook in lifecycle_hooks:
            before = getattr(hook, "before_tool", None)
            if before is None:
                continue
            try:
                await before(state, call)
            except Exception as exc:  # pragma: no cover - 防御式
                await self._emit_hook_error("before_tool", exc, state)

    async def _run_lifecycle_after_tool(
        self,
        state: RunState,
        call: ToolCall,
        result_message: Message,
        lifecycle_hooks: Sequence[LifecycleHook],
    ) -> None:
        for hook in lifecycle_hooks:
            after = getattr(hook, "after_tool", None)
            if after is None:
                continue
            try:
                await after(state, call, result_message)
            except Exception as exc:  # pragma: no cover - 防御式
                await self._emit_hook_error("after_tool", exc, state)

    async def _run_lifecycle_after_run(
        self,
        *,
        state: RunState,
        session: Session,
        result: Result,
        lifecycle_hooks: Sequence[LifecycleHook],
    ) -> None:
        for hook in lifecycle_hooks:
            after = getattr(hook, "after_run", None)
            if after is None:
                continue
            try:
                await after(state, session, result)
            except asyncio.CancelledError as exc:
                await self._emit_hook_error("after_run", exc, state)
            except Exception as exc:  # pragma: no cover - 防御式
                await self._emit_hook_error("after_run", exc, state)

    async def _emit_hook_error(
        self,
        phase: str,
        exc: BaseException,
        state: RunState,
        *,
        source: str = "lifecycle_hook",
    ) -> None:
        await self._emit(
            Event(
                kind="error",
                run_id=state.run_id,
                turn=state.turn,
                payload={
                    "source": source,
                    "phase": phase,
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        )

    # ------------------------------------------------------------------
    # EventSink fan-out
    # ------------------------------------------------------------------

    async def _emit(self, event: Event) -> None:
        """把事件 fan-out 到所有 sink。sink 异常被吞掉以免污染主链路。

        agent-tree-v0.1 模块 G：fan-out 前把当前 run 的 ``agent_id``（来自
        :data:`_RUN_AGENT_ID` ContextVar）注入到 Event 的坐标字段。runner 内
        20+ emit 点无需逐处透传 ``agent_id``，统一在此收口；Event 自带非空
        ``agent_id`` 时视为显式覆盖，不覆盖。
        """
        run_agent_id = _RUN_AGENT_ID.get()
        if run_agent_id and not event.agent_id:
            event = replace(event, agent_id=run_agent_id)
        sinks = (*tuple(self._event_sinks), *_RUN_EVENT_SINKS.get())
        for sink in sinks:
            try:
                await sink.emit(event)
            except Exception:
                # 观测层不允许影响主链路；这里静默忽略，
                # 未来可以把"sink 自己的错误"落到降级日志。
                continue

    # ------------------------------------------------------------------
    # 供装配层动态扩展 sink（可选）
    # ------------------------------------------------------------------

    def add_event_sink(self, sink: EventSink) -> None:
        """动态追加 event sink，便于装配层组合观测能力。"""
        self._event_sinks.append(sink)

    def add_lifecycle_hook(self, hook: LifecycleHook) -> None:
        """动态追加 lifecycle hook。"""
        self._lifecycle_hooks.append(hook)


def _parent_agent_snapshot(
    *,
    state: RunState,
    session: Session,
    agent_spec: AgentSpec,
    effective_max_turns: int,
    max_tokens: int | None,
    temperature: float | None,
    timeout_seconds: float | None,
) -> dict[str, Any]:
    """生成父 agent 快照，输入为当前 run 事实，输出给 ToolContext metadata。"""
    return {
        "run_id": state.run_id,
        "session_id": session.session_id,
        "agent_id": state.agent_id,
        "agent": agent_spec.name,
        "model": agent_spec.default_model,
        "preset_id": agent_spec.metadata.get("model_preset_id"),
        "reasoning_effort": agent_spec.reasoning_effort,
        "effective_max_turns": effective_max_turns,
        "max_turns": effective_max_turns,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "timeout_seconds": timeout_seconds,
        "agent_spec": {
            "name": agent_spec.name,
            "default_model": agent_spec.default_model,
            "tool_names": list(agent_spec.tool_names),
            "max_turns": agent_spec.max_turns,
            "metadata": dict(agent_spec.metadata),
            "reasoning_effort": agent_spec.reasoning_effort,
        },
    }


__all__ = ["Runner"]
