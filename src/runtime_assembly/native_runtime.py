"""进程内运行时装配层。

按 :file:`docs/kongming-agent-v1-minimal/10-contracts.md` 中"Native Runtime
边界"的约定：

    runner 负责跑，native_runtime 负责装，cli/main 负责进。

所以这里**不**自持第二份 turn loop、不复制任何 :class:`core.run_state.RunState`
相关状态。所有 turn 推进都走 :class:`core.runner.Runner`。

**依赖方向**：

- 只 import ``core`` 协议 / 数据结构、``infrastructure.config`` 配置、本包自己的
  ``executors/llm`` provider，以及 ``safety/`` 下的安全链装配入口。
- 对 ``safety/`` 的依赖是"装配层 → 下层 policy"的向下引用，属于运行时装配
  职责的一部分（已在 ``.importlinter`` 中显式白名单化，避免触犯 layered 合约）。
- 绝不 import ``tools/`` / ``host/`` / ``cli/`` / ``infrastructure.tracing/`` /
  ``sessions/` / `prompting/`` 里的任何具体类。调用方（host 或 cli 或 tests）通过 ``build(...)``
  的 kwargs 注入它们。
- ``tools: ToolLookup | None`` / ``approval: ApprovalProvider | None`` /
  ``event_sinks: list[EventSink] | None`` / ``session_factory`` 全部允许空缺，
  缺省时用最小占位实现（见类内 ``_AllowAllApproval`` / ``InMemorySession``）。

**安全链装配策略**：

- ``build()`` 传入的 ``approval`` 参数始终视为 **底层审批**（命中 ``ask`` 时用）。
- 运行时装配层把 capability / permission / approval 串成高层安全链，
  最终传给 :class:`core.runner.Runner` 的 approval 永远是
  :class:`safety.SafetyGatedApproval`，不是底层的 :class:`InteractiveApproval`。
- 这条装配链始终启用，不提供"关掉 safety chain"的后门；测试需要全放行时，
  注入 ``AutoAllowApproval`` 作为底层 approval，并通过 ``approval.mode=auto_allow``
  让 :class:`PermissionPolicy` 默认 ``allow`` 即可。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from core.agent_spec import AgentSpec
from core.contracts import (
    ApprovalDecision,
    ApprovalProvider,
    ApprovalRequest,
    Event,
    EventSink,
    LLMProvider,
    MessageCompactor,
    PromptDebugSink,
    Session,
    Tool,
    ToolLookup,
)
from core.message import Message
from core.result import Result
from core.runner import Runner
from core.session import InMemorySession
from infrastructure.config.models import Config
from infrastructure.llm_providers.anthropic_messages import AnthropicMessagesProvider
from infrastructure.llm_providers.openai_responses import OpenAIResponsesProvider
from prompting import HistoryCompactor
from prompting.assembly.input_assembler import InputAssembler
from prompting.compaction.history_compactor import CompactorConfig
from prompting.instructions.instruction_loader import InstructionSource
from safety import (
    CapabilityPolicy,
    PermissionPolicy,
    SafetyGatedApproval,
    build_safety_chain,
)

if TYPE_CHECKING:
    from evolution import EvolutionStateStore, TranscriptWindow

# ---------------------------------------------------------------------------
# 占位实现
# ---------------------------------------------------------------------------


class _AllowAllApproval:
    """最小占位 **底层** ApprovalProvider。

    这是安全链的**底层 fallback**：当调用方没有显式注入 ``approval=...``
    且 :class:`PermissionPolicy` 判定为 ``ask`` 时，安全链会下沉到这里拿
    "approved"。典型场景是无宿主的 tests / smoke。

    它**不再**是对外的 ApprovalProvider —— 装配层永远把它包进
    :class:`safety.SafetyGatedApproval` 之后才交给 runner。所以即便它无条件放行，
    真正是否允许仍然由 capability / permission 两层先过一遍。
    """

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(
            outcome="approved",
            reason="allow-all fallback inside safety chain",
            metadata={"placeholder": "_AllowAllApproval", "tool_name": request.tool_name},
        )


class _EmptyToolLookup:
    """空的 ToolLookup。

    当调用方没有注入 tools 时使用；任何 ``name in self`` 都返回 False，
    让 runner 在 ``_resolve_tools`` 阶段直接返回空列表（前提是
    ``agent_spec.tool_names`` 也为空，这是 :meth:`NativeRuntime.build` 的
    默认装配行为）。
    """

    def __contains__(self, name: object) -> bool:
        return False

    def __getitem__(self, name: str) -> Tool:
        raise KeyError(name)


class _NoopCompactor:
    """原样透传的 MessageCompactor。

    当 ``cfg.compactor.enabled=False``（默认）时装配进 runner / input_assembler，
    runner 感知上等同 "没有 compactor"。这样可以避免 :class:`InputAssembler` 的
    默认 fallback（``compactor or HistoryCompactor()``）重新装上一个 FIFO 压缩器。

    语义：不改消息数量、不截断任何字段，仅返回 ``list(history)`` 副本。
    """

    async def compact(self, history: Sequence[Message]) -> list[Message]:
        return list(history)


_NOOP_COMPACTOR = _NoopCompactor()


# ---------------------------------------------------------------------------
# NativeRuntime
# ---------------------------------------------------------------------------


class NativeRuntime:
    """进程内运行时装配层。

    ``__init__`` 保持显式依赖注入（便于 host / cli / tests 完全控制装配）；
    :meth:`build` 是"按 Config 装一份默认依赖"的工厂糖。
    """

    def __init__(
        self,
        *,
        config: Config,
        runner: Runner,
        llm: LLMProvider,
        tools: ToolLookup,
        enabled_tool_names: list[str],
        approval: ApprovalProvider,
        session_factory: Callable[[str], Session],
        event_sinks: list[EventSink],
        agent_spec: AgentSpec,
    ) -> None:
        self._config = config
        self._runner = runner
        self._llm = llm
        self._tools = tools
        self._enabled_tool_names = list(enabled_tool_names)
        self._approval = approval
        self._session_factory = session_factory
        self._event_sinks = list(event_sinks)
        self._agent_spec = agent_spec

        # 单 agent、单 run loop 语义：运行时开一个 session cache，让外部多次
        # ``run(session_id=same)`` 落到同一个 Session 实例（多轮对话连续性）。
        self._sessions: dict[str, Session] = {}
        self._pending_review_tasks: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------
    # 构造方法
    # ------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        config: Config,
        *,
        event_sinks: list[EventSink] | None = None,
        approval: ApprovalProvider | None = None,
        tools: ToolLookup | Mapping[str, Tool] | None = None,
        enabled_tool_names: list[str] | None = None,
        session_factory: Callable[[str], Session] | None = None,
        agent_spec: AgentSpec | None = None,
        instructions: str | None = None,
        capability_policy: CapabilityPolicy | None = None,
        permission_policy: PermissionPolicy | None = None,
        message_compactor: MessageCompactor | None = None,
        prompt_debug_sink: PromptDebugSink | None = None,
        instruction_origins: Sequence[str] | None = None,
    ) -> NativeRuntime:
        """按 Config 装配一份默认 runtime。

        Args:
            config: 从 :func:`infrastructure.config.load_config` 拿到的 :class:`Config`。
            event_sinks: 事件落地 sinks。默认空列表；infrastructure.tracing 批次落地
                后由 host 或 cli 注入 ``JsonlTraceSink``。
            approval: **底层** 审批 provider（命中 ``ask`` 时用）。默认使用
                :class:`_AllowAllApproval` 占位（裸放行）。不论是否传入，
                装配层都会把它包进 :class:`safety.SafetyGatedApproval`
                （capability → permission → approval）再交给 runner，
                所以"不传 approval"并不等于"关掉 safety"。
            tools: 工具查找面。接受满足 :class:`ToolLookup` 的对象或
                ``Mapping[str, Tool]``（dict 天然满足）。默认空查找面。
            enabled_tool_names: 要启用的工具名白名单；用于构造默认 AgentSpec
                的 ``tool_names``。默认空列表（无 tool_call 闭环）。
            session_factory: 给定 ``session_id`` 返回一个 :class:`Session` 实现。
                默认返回 :class:`InMemorySession`。
            agent_spec: 默认 AgentSpec；不传则按 Config 合成一份最小 spec。
            instructions: 系统指令文本。若 ``agent_spec`` 也为 ``None``，这段文本
                会替换默认 ``"You are kongming agent."``；装配层（CLI / host）可
                通过 :class:`prompting.InstructionLoader` 把 agent_spec / 外部文件 /
                环境变量合并后传入。显式传 ``agent_spec`` 时，本参数被忽略。
            capability_policy: 显式注入的 :class:`safety.CapabilityPolicy`。
                ``None`` 时走 :meth:`CapabilityPolicy.from_config` 自动装配。
            permission_policy: 显式注入的 :class:`safety.PermissionPolicy`。
                ``None`` 时走 :meth:`PermissionPolicy.from_config` 自动装配。
            message_compactor: 在 runner 每 turn 把 history 送给 LLM 之前做一次
                加工。``None`` 时使用默认的 :class:`prompting.HistoryCompactor`
                （超过 50 条时裁剪中段、截断超长 tool_result，永远保留首条 system
                和最近 20 条）。显式传 ``False`` 式占位对象可关闭；测试场景可传
                ``_NoopCompactor`` 之类的 stub。
            prompt_debug_sink: prompt debug 输出 sink；不传则关闭。
            instruction_origins: CLI / host 侧收集到的真实 instruction 来源列表，
                仅用于 prompt debug dump。

        Returns:
            装配好的 :class:`NativeRuntime`，直接 ``await runtime.run(...)`` 即可。
        """
        # 用 effective_provider 而非 provider：当 base_url 命中 anthropic 启发式
        # （host=api.anthropic.com / path 段含 "anthropic"）时自动切到 anthropic
        # 协议，让用户在 .env 里切第三方 anthropic 兼容端点（MiniMax / OpenRouter）
        # 时无需手动改 yaml 里的 provider 字段。
        if config.model.effective_provider == "anthropic":
            llm: LLMProvider = AnthropicMessagesProvider(
                model_config=config.model,
                max_retries=config.retry.max_retries,
                retry_backoff=config.retry.retry_backoff,
                enable_raw_dump=config.trace.raw_llm,
                stream_read_timeout=config.stream.read_timeout,
            )
        else:
            llm = OpenAIResponsesProvider(
                model_config=config.model,
                max_retries=config.retry.max_retries,
                retry_backoff=config.retry.retry_backoff,
                enable_raw_dump=config.trace.raw_llm,
                stream_read_timeout=config.stream.read_timeout,
            )

        resolved_tools: ToolLookup
        if tools is None:
            resolved_tools = _EmptyToolLookup()
        else:
            resolved_tools = tools  # Mapping[str, Tool] 结构上满足 ToolLookup

        resolved_tool_names = list(enabled_tool_names or [])

        # 底层 approval：命中 ask 时用的 human-in-the-loop。
        base_approval: ApprovalProvider = approval or _AllowAllApproval()

        resolved_event_sinks: list[EventSink] = list(event_sinks or [])

        # 把 capability / permission / approval 串成高层安全链；
        # 这是最终传给 runner 的 ApprovalProvider。
        # v0.1.4：传入 event_sinks 让 SafetyDecisionEngine emit 三个新决策事件
        # （tool.denied / tool.approval_required / tool.silently_allowed）到
        # 统一 trace（受 cfg.safety.log_silent_reads 控制是否写盘）。
        safety_approval: SafetyGatedApproval = build_safety_chain(
            config,
            interactive_approval=base_approval,
            capability_policy=capability_policy,
            permission_policy=permission_policy,
            event_sinks=resolved_event_sinks,
        )

        resolved_session_factory = session_factory or (lambda sid: InMemorySession(session_id=sid))

        resolved_instructions = (
            instructions
            if instructions is not None and instructions.strip()
            else "You are kongming agent."
        )
        resolved_spec: AgentSpec = agent_spec or AgentSpec(
            name="default",
            instructions=resolved_instructions,
            default_model=config.model.name,
            tool_names=tuple(resolved_tool_names),
            max_turns=config.runner.max_turns,
            reasoning_effort=config.model.reasoning_effort,
        )

        # 压缩默认关闭（cfg.compactor.enabled=False）。显式传入 message_compactor 时
        # 优先使用；否则仅当 enabled=True 才装配 HistoryCompactor，否则走 _NOOP_COMPACTOR
        # （原样透传 history，不做裁剪）。走 _NOOP_COMPACTOR 而非 None，是为了避免
        # InputAssembler 默认的 ``compactor or HistoryCompactor()`` fallback 又装回
        # FIFO 压缩器。LLM summarize 式压缩留给后续独立 task compactor-v2-llm-summarize。
        resolved_compactor: MessageCompactor
        if message_compactor is not None:
            resolved_compactor = message_compactor
        elif config.compactor.enabled:
            resolved_compactor = HistoryCompactor(
                config=CompactorConfig(
                    max_messages=config.compactor.max_messages,
                    keep_recent=config.compactor.keep_recent,
                    keep_system=config.compactor.keep_system,
                    tool_result_max_chars=config.compactor.tool_result_max_chars,
                )
            )
        else:
            resolved_compactor = _NOOP_COMPACTOR

        # 把 AgentSpec.instructions 包装成 InstructionSource，交给 InputAssembler。
        # origin="" 表示"透传"：NativeRuntime 接收的 instructions 可能已经是
        # InstructionLoader.render() 产出的格式化文本（带 "# origin\n" 前缀），
        # 用空 origin 避免 InputAssembler 再次追加 "# agent_spec\n" 前缀（双重渲染）。
        # InputAssembler 接管 system 注入 + compact；Runner._seed_messages()
        # 在有 assembler 时只写 user 消息，不再双重注入 system。
        agent_instruction_sources: list[InstructionSource] = []
        if resolved_spec.instructions and resolved_spec.instructions.strip():
            agent_instruction_sources.append(
                InstructionSource(
                    origin="",
                    content=resolved_spec.instructions,
                )
            )

        # InputAssembler 复用 resolved_compactor，确保显式传入的 message_compactor
        # 在 assembler 路径下也生效（不另创建新实例）。
        input_assembler = InputAssembler(compactor=resolved_compactor)

        runner = Runner(
            event_sinks=resolved_event_sinks,
            message_compactor=resolved_compactor,
            input_assembler=input_assembler,
            instruction_sources=agent_instruction_sources,
            prompt_debug_sink=prompt_debug_sink,
            instruction_origins=instruction_origins,
            stream_enabled=config.stream.enabled,
            suppress_content_after_tool_call=config.stream.suppress_content_after_tool_call,
        )

        return cls(
            config=config,
            runner=runner,
            llm=llm,
            tools=resolved_tools,
            enabled_tool_names=resolved_tool_names,
            approval=safety_approval,
            session_factory=resolved_session_factory,
            event_sinks=resolved_event_sinks,
            agent_spec=resolved_spec,
        )

    # ------------------------------------------------------------------
    # 运行入口
    # ------------------------------------------------------------------

    async def run(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        reasoning_effort: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> Result:
        """执行一次"输入 → 结果"主链路。

        - 同一 ``session_id`` 反复调用会落到同一个 Session 实例上（多轮对话）。
        - ``session_id`` 为 ``None`` 时走匿名新 session。
        - ``reasoning_effort`` 非 None 时临时覆盖 model_config.reasoning_effort，
          本次 run 结束后恢复原值。Provider 每次调用都读 model_config，无需改动。
        - ``attachments`` 是用户输入附件 ref 的 dict 列表（``UserInputAttachment.model_dump()``
          形态），由 web ws.py 透传过来；CLI 路径默认 None 不影响行为。
        """
        session = self._get_or_create_session(session_id)
        result: Result

        if reasoning_effort is not None:
            saved = self._config.model.reasoning_effort
            self._config.model.reasoning_effort = reasoning_effort  # type: ignore[assignment]
            try:
                result = await self._runner.run(
                    user_input,
                    session=session,
                    agent_spec=self._agent_spec,
                    llm=self._llm,
                    tools=self._tools,
                    approval=self._approval,
                    max_turns=self._config.runner.max_turns,
                    enabled_tools=self._resolve_enabled_tools(),
                    attachments=attachments,
                )
            finally:
                self._config.model.reasoning_effort = saved
        else:
            result = await self._runner.run(
                user_input,
                session=session,
                agent_spec=self._agent_spec,
                llm=self._llm,
                tools=self._tools,
                approval=self._approval,
                max_turns=self._config.runner.max_turns,
                enabled_tools=self._resolve_enabled_tools(),
                attachments=attachments,
            )

        await self._maybe_start_evolution_review(session=session, result=result)
        return result

    # ------------------------------------------------------------------
    # 访问器（便于 host / cli / tests 观察装配结果）
    # ------------------------------------------------------------------

    @property
    def config(self) -> Config:
        return self._config

    @property
    def runner(self) -> Runner:
        return self._runner

    @property
    def agent_spec(self) -> AgentSpec:
        return self._agent_spec

    @property
    def llm(self) -> LLMProvider:
        """暴露 provider 实例。

        host 装配层（如 ``cli/main.py``）需要它来探测 ``SupportsLLMStream`` 能力，
        以决定是否在 ``SessionBridge`` 中跳过 final.content 重复输出（流式路径下
        ``CLIStreamSink`` 已渲染过完整正文）。
        """
        return self._llm

    @property
    def tools(self) -> ToolLookup:
        """暴露已装配的 ToolLookup。

        scheduler / cron 装配层（:mod:`scheduler.runtime_factory`）需要
        从 runtime 拿到 tool 散件以构造 :class:`ExecutionBridge`。
        """
        return self._tools

    @property
    def enabled_tool_names(self) -> list[str]:
        """暴露已装配的 enabled tool 白名单（副本）。"""
        return list(self._enabled_tool_names)

    @property
    def approval(self) -> ApprovalProvider:
        """暴露已装配的高层 :class:`ApprovalProvider`（已包 SafetyGatedApproval）。"""
        return self._approval

    @property
    def session_factory(self) -> Callable[[str], Session]:
        """暴露 session 工厂（``session_id -> Session``）。"""
        return self._session_factory

    @property
    def event_sinks(self) -> list[EventSink]:
        """暴露 event sink 列表（副本，避免外部直改影响 runner fan-out）。"""
        return list(self._event_sinks)

    @property
    def grant_store(self):  # type: ignore[no-untyped-def]
        """暴露底层 :class:`safety.grants.store.GrantStore`（如有）。

        web ``thread_manager.evict_cell`` 用 ``runtime.grant_store.clear_session(session_id)``
        清理 thread 私有 grants（修 bug-report-20260427-235232 跨 thread 信任泄漏）。
        approval 不是 ``SafetyGatedApproval``（如 stub / AutoAllow）时返回 ``None``。

        无 type 注解：避免在执行器层 import safety.grants.store（架构边界）。
        """
        return getattr(self._approval, "grant_store", None)

    def add_event_sink(self, sink: EventSink) -> None:
        """追加事件 sink 到底层 runner。"""
        self._event_sinks.append(sink)
        self._runner.add_event_sink(sink)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """关闭底层资源（目前只有 provider 的 httpx client）。

        幂等：provider 自身的 ``aclose`` 已做 None 检查，多次调不会抛。
        由 CLI / SessionBridge 退出路径或测试 finally 触发。
        """
        await self._drain_pending_reviews()
        aclose = getattr(self._llm, "aclose", None)
        if aclose is not None:
            await aclose()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _get_or_create_session(self, session_id: str | None) -> Session:
        if session_id is None:
            # 匿名 session 不缓存，每次新建一份。
            return self._session_factory("")
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        created = self._session_factory(session_id)
        self._sessions[session_id] = created
        return created

    def _resolve_enabled_tools(self) -> Sequence[Tool] | None:
        """按 ``enabled_tool_names`` 从 ToolLookup 里解析 Tool 列表。

        返回 ``None`` 时 runner 会回退到按 ``AgentSpec.tool_names`` 查询。
        这里提前解析是为了：
        - 早失败：若有工具名在 lookup 里缺失，在装配层就能直接暴露。
        - 避免 runner 多次做同样查询。

        如果 ``enabled_tool_names`` 为空，返回 ``[]`` 让 runner 拿到
        "显式空列表"而不是 ``None``，避免 runner 再次去 lookup 里查。
        """
        if not self._enabled_tool_names:
            return []
        resolved: list[Tool] = []
        for name in self._enabled_tool_names:
            if name not in self._tools:
                # 交给 runner 抛 AgentError（保持错误归属一致），这里返回 None
                # 让 runner 自己按 AgentSpec.tool_names 再试一次并报错。
                return None
            resolved.append(self._tools[name])
        return resolved

    async def _maybe_start_evolution_review(self, *, session: Session, result: Result) -> None:
        if result.status != "completed":
            return
        if self._agent_spec.metadata.get("evolution_role") == "reviewer":
            return
        learning = getattr(self._config.evolution, "learning", None)
        if learning is None or not learning.enabled:
            return
        if "evolution_write" not in self._tools:
            return

        from evolution import (
            EvolutionStateStore,
            build_transcript_window,
            count_user_turns,
            resolve_evolution_root,
        )

        history = await session.history()
        user_turn_count = count_user_turns(history)
        if user_turn_count < learning.min_user_turns:
            return

        root_dir = resolve_evolution_root(learning.root_path)
        state_store = EvolutionStateStore(root_dir)
        session_state = await state_store.record_parent_run(
            session_id=result.session_id,
            user_turn_count=user_turn_count,
        )
        if session_state.run_count % learning.every_n_runs != 0:
            return

        window = build_transcript_window(
            session_id=result.session_id,
            run_id=result.run_id,
            history=history,
            final_message=result.final_message,
            max_messages=learning.max_history_messages,
        )
        review_id = f"evo-review:{result.run_id}"
        await self._emit_runtime_event(
            kind="evolution.review.started",
            run_id=result.run_id,
            payload={
                "review_id": review_id,
                "session_id": result.session_id,
                "user_turn_count": user_turn_count,
                "included_turns": list(window.included_turns),
                "timeout_seconds": learning.review_timeout_seconds,
            },
        )
        task = asyncio.create_task(
            self._run_evolution_review_task(
                review_id=review_id,
                parent_result=result,
                state_store=state_store,
                transcript_window=window,
            ),
            name=review_id,
        )
        self._pending_review_tasks[review_id] = task

    async def _run_evolution_review_task(
        self,
        *,
        review_id: str,
        parent_result: Result,
        state_store: EvolutionStateStore,
        transcript_window: TranscriptWindow,
    ) -> None:
        learning = self._config.evolution.learning
        try:
            from evolution import run_child_review

            child_outcome = await run_child_review(
                parent_runtime=self,
                window=transcript_window,
                trigger_reason="cadence",
                timeout_seconds=learning.review_timeout_seconds,
                max_nutrients=learning.max_nutrients,
                min_confidence=learning.nutrient_confidence_threshold,
            )
            for event in child_outcome.visible_events:
                await self._emit_runtime_event(
                    kind=event.kind,
                    run_id=event.run_id or "",
                    payload=dict(event.payload or {}),
                    turn=event.turn,
                )
            child_result = child_outcome.result
            if child_outcome.write_ok:
                await self._emit_runtime_event(
                    kind="evolution.review.completed",
                    run_id=parent_result.run_id,
                    payload={
                        "review_id": review_id,
                        "review_run_id": child_result.run_id,
                        "session_id": parent_result.session_id,
                        "write_status": child_outcome.write_status,
                        "duration_ms": child_outcome.duration_ms,
                        "timeout_hit": child_outcome.timed_out,
                        "timeout_seconds": child_outcome.timeout_seconds,
                        "nutrients_written": child_outcome.write_data.get("nutrients_written")
                        if isinstance(child_outcome.write_data, dict)
                        else None,
                        "written_nutrient_ids": child_outcome.write_data.get("written_nutrient_ids")
                        if isinstance(child_outcome.write_data, dict)
                        else None,
                        "timed_out_after_write": child_outcome.timed_out,
                    },
                )
                return
            if child_result.status != "completed":
                child_error = child_result.error
                child_error_kind = (
                    type(child_error).__name__ if child_error is not None else "ChildReviewerError"
                )
                child_error_message = (
                    child_error.message
                    if child_error is not None
                    else f"child reviewer finished with status={child_result.status}"
                )
                await state_store.mark_review_result(
                    session_id=parent_result.session_id,
                    run_id=parent_result.run_id,
                    status="failed",
                )
                await self._emit_runtime_event(
                    kind="evolution.review.failed",
                    run_id=parent_result.run_id,
                    payload={
                        "review_id": review_id,
                        "review_run_id": child_result.run_id,
                        "session_id": parent_result.session_id,
                        "error_kind": child_error_kind,
                        "message": child_error_message,
                        "child_status": child_result.status,
                        "duration_ms": child_outcome.duration_ms,
                        "timeout_hit": child_outcome.timed_out,
                        "timeout_seconds": child_outcome.timeout_seconds,
                    },
                )
                return
            write_error_message = child_outcome.write_error or (
                f"evolution_write did not succeed status={child_outcome.write_status}"
            )
            await state_store.mark_review_result(
                session_id=parent_result.session_id,
                run_id=parent_result.run_id,
                status="failed",
            )
            await self._emit_runtime_event(
                kind="evolution.review.failed",
                run_id=parent_result.run_id,
                payload={
                    "review_id": review_id,
                    "review_run_id": child_result.run_id,
                    "session_id": parent_result.session_id,
                    "error_kind": "EvolutionWriteError",
                    "message": write_error_message,
                    "write_status": child_outcome.write_status,
                    "duration_ms": child_outcome.duration_ms,
                    "timeout_hit": child_outcome.timed_out,
                    "timeout_seconds": child_outcome.timeout_seconds,
                },
            )
        except asyncio.CancelledError:
            await state_store.mark_review_result(
                session_id=parent_result.session_id,
                run_id=parent_result.run_id,
                status="cancelled",
            )
            await self._emit_runtime_event(
                kind="evolution.review.failed",
                run_id=parent_result.run_id,
                payload={
                    "review_id": review_id,
                    "session_id": parent_result.session_id,
                    "error_kind": "cancelled",
                    "timeout_hit": False,
                },
            )
            raise
        except Exception as exc:
            await state_store.mark_review_result(
                session_id=parent_result.session_id,
                run_id=parent_result.run_id,
                status="failed",
            )
            await self._emit_runtime_event(
                kind="evolution.review.failed",
                run_id=parent_result.run_id,
                payload={
                    "review_id": review_id,
                    "session_id": parent_result.session_id,
                    "error_kind": type(exc).__name__,
                    "message": str(exc),
                    "timeout_hit": isinstance(exc, TimeoutError),
                },
            )
        finally:
            self._pending_review_tasks.pop(review_id, None)

    async def _drain_pending_reviews(self) -> None:
        if not self._pending_review_tasks:
            return
        learning = getattr(self._config.evolution, "learning", None)
        timeout_seconds = (
            learning.drain_on_close_seconds if learning is not None and learning.enabled else 0.0
        )
        pending_items = list(self._pending_review_tasks.items())
        try:
            await asyncio.wait_for(
                asyncio.gather(*(task for _, task in pending_items), return_exceptions=True),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            await self._emit_runtime_event(
                kind="evolution.review.drain_timeout",
                run_id="runtime-close",
                payload={
                    "pending_review_ids": [review_id for review_id, _ in pending_items],
                    "timeout_seconds": timeout_seconds,
                },
            )
            for _, task in pending_items:
                task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(*(task for _, task in pending_items), return_exceptions=True)

    async def _emit_runtime_event(
        self,
        *,
        kind: str,
        run_id: str,
        payload: dict[str, object],
        turn: int | None = None,
    ) -> None:
        event = Event(kind=kind, run_id=run_id, turn=turn, payload=payload)
        for sink in self._event_sinks:
            await sink.emit(event)


__all__ = ["NativeRuntime"]
