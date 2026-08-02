"""会话级执行引擎装配层。

:class:`SessionEngine` 是按会话（CLI 交互 / Web thread / cron run）装配的执行依赖总成：
provider（油路）、ToolRegistry（传动）、SafetyGatedApproval（控制单元）、Runner（曲轴）
都在 :meth:`build` 里组装到位。一个引擎实例服务一个 session，session 内的 root agent
和全部 spawn 出去的 child agent 共用它；**进程内可同时存在多个实例**（Web 的每个
thread 一个、CLI 主聊天与 cron 各一个）。

按 :file:`docs/kongming-agent-v1-minimal/10-contracts.md` 中"Native Runtime
边界"的约定：

    runner 负责跑，session_engine 负责装，cli/main 负责进。

所以这里**不**自持第二份 turn loop、不复制任何 :class:`core.run_state.RunState`
相关状态。所有 turn 推进都走 :class:`core.runner.Runner`。

**依赖方向**：

- 只 import ``core`` 协议 / 数据结构、``infrastructure.config`` 配置、本包自己的
  ``executors/llm`` provider，以及 ``safety/`` 下的安全链装配入口。
- 对 ``safety/`` 的依赖是"装配层 → 下层 policy"的向下引用，属于运行时装配
  职责的一部分（已在 ``.importlinter`` 中显式白名单化，避免触犯 layered 合约）。
- ``prompting/`` 装配边已由 ``.importlinter`` Contract 3 放行（``runtime_assembly →
  prompting*``）：HistoryCompactor / InputAssembler / ConversationReferenceManager
  等 prompt 组装组件由本层装配。其余 ``tools/`` / ``hosts/`` / ``cli/`` /
  ``infrastructure.tracing/`` / ``sessions/`` 仍不 import 任何具体类——调用方
  （host 或 cli 或 tests）通过 ``build(...)`` 的 kwargs 注入它们。
- ``tools: ToolLookup | None`` / ``approval: ApprovalProvider | None`` /
  ``event_sinks: list[EventSink] | None`` / ``session_factory`` 全部允许空缺，
  缺省时用最小占位实现（见类内 ``_FailClosedApproval`` / ``InMemorySession``）。

**安全链装配策略**：

- ``build()`` 传入的 ``approval`` 参数始终视为底层人工审批 Provider。
- 运行时装配层固定装配 DangerGuard、全局模式和 thread permissions，最终传给
  :class:`core.runner.Runner` 的入口统一为 :class:`safety.SafetyGatedApproval`。
- 测试可显式注入 ``permissions_manager`` 与底层 approval fake，仍会经过 DangerGuard。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.agent_spec import AgentSpec, coerce_reasoning_effort
from core.contracts import (
    ApprovalDecision,
    ApprovalProvider,
    ApprovalRequest,
    AssetBytesReader,
    EventSink,
    LLMProvider,
    LLMToolCallContract,
    MessageCompactor,
    PromptDebugSink,
    ProviderUsageSnapshot,
    RunExecutionOverrides,
    Session,
    SteerRequest,
    Tool,
    ToolLookup,
)
from core.lifecycle import LifecycleHook
from core.message import Message
from core.result import Result
from core.runner import Runner
from core.session import InMemorySession
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.model_provider_catalog import ResolvedModelConfig
from infrastructure.config.models import Config
from infrastructure.config.paths import get_kongming_home
from infrastructure.llm_providers.provider_factory import build_provider, resolve_model_config
from infrastructure.llm_providers.reasoning import ReasoningConfig, resolve_reasoning_plan
from prompting import HistoryCompactor
from prompting.assembly.input_assembler import InputAssembler
from prompting.compaction.history_compactor import CompactorConfig
from prompting.context_sources.conversation_reference_manager import (
    ConversationReferenceContext,
    ConversationReferenceManager,
)
from prompting.instructions.instruction_loader import InstructionSource
from safety import PermissionsManager, SafetyGatedApproval, build_safety_chain
from safety.auto_approval.disposition import ApprovalDispositionResolver

_ENABLED_TOOLS_DEFAULT = object()

# ---------------------------------------------------------------------------
# 占位实现
# ---------------------------------------------------------------------------


class _FailClosedApproval:
    """缺少人工宿主时使用的 fail-closed 底层 ApprovalProvider。

    SafetyDecisionEngine 只有在 danger 或普通未命中时才调用本 fallback。
    无 CLI/Web 审批宿主的装配必须返回 rejected，避免后台运行把“需要人确认”
    错当成批准。full_trust 与 permissions allow 在到达本层前已经完成裁决。
    """

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """把无法交付给人的审批稳定收敛为拒绝。"""
        return ApprovalDecision(
            outcome="rejected",
            reason="approval requires an interactive host",
            metadata={"placeholder": "_FailClosedApproval", "tool_name": request.tool_name},
        )


class _EmptyToolLookup:
    """空的 ToolLookup。

    当调用方没有注入 tools 时使用；任何 ``name in self`` 都返回 False，
    让 runner 在 ``_resolve_tools`` 阶段直接返回空列表（前提是
    ``agent_spec.tool_names`` 也为空，这是 :meth:`SessionEngine.build` 的
    默认装配行为）。
    """

    def __contains__(self, name: object) -> bool:
        return False

    def __getitem__(self, name: str) -> Tool:
        raise KeyError(name)


def _snapshot_tool_lookup(tools: ToolLookup | Mapping[str, Tool]) -> ToolLookup:
    """生成 session 级工具查找快照，保留 Tool 对象引用。"""
    if isinstance(tools, Mapping):
        return dict(tools)

    all_tools = getattr(tools, "all_tools", None)
    if callable(all_tools):
        values = all_tools()
        if isinstance(values, Iterable):
            return {tool.name: tool for tool in values}

    if isinstance(tools, Iterable):
        return {tool.name: tool for tool in tools}

    return tools


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


def _metadata_cwd_path(metadata: Mapping[str, Any]) -> Path | None:
    """从工具上下文 metadata 解析 cwd，输入为 metadata，输出为绝对 Path 或 None。"""
    raw = metadata.get("cwd")
    if isinstance(raw, Path):
        return raw.expanduser().resolve(strict=False)
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser().resolve(strict=False)
    return None


# ---------------------------------------------------------------------------
# SessionEngine
# ---------------------------------------------------------------------------


class SessionEngine:
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
        model_catalog_manager: ModelCatalogManager | None = None,
        model_config: ResolvedModelConfig | None = None,
        lifecycle_hooks: Sequence[LifecycleHook] | None = None,
        tool_context_metadata: Mapping[str, Any] | None = None,
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
        self._model_catalog_manager = model_catalog_manager
        self._model_config = model_config
        self._lifecycle_hooks: list[LifecycleHook] = list(lifecycle_hooks or [])
        self._tool_context_metadata: dict[str, Any] = dict(tool_context_metadata or {})

        # 单 agent、单 run loop 语义：运行时开一个 session cache，让外部多次
        # ``run(session_id=same)`` 落到同一个 Session 实例（多轮对话连续性）。
        self._sessions: dict[str, Session] = {}

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
        permissions_manager: PermissionsManager | None = None,
        message_compactor: MessageCompactor | None = None,
        prompt_debug_sink: PromptDebugSink | None = None,
        instruction_origins: Sequence[str] | None = None,
        conversation_reference_context: ConversationReferenceContext | None = None,
        asset_reader: AssetBytesReader | None = None,
        llm_provider: LLMProvider | None = None,
        tool_context_metadata: Mapping[str, Any] | None = None,
        disposition_resolver: ApprovalDispositionResolver | None = None,
        model_catalog_manager: ModelCatalogManager | None = None,
        model_config: ResolvedModelConfig | None = None,
    ) -> SessionEngine:
        """按 Config 装配一份默认 runtime。

        Args:
            config: 从 :func:`infrastructure.config.load_config` 拿到的 :class:`Config`。
            event_sinks: 事件落地 sinks。默认空列表；infrastructure.tracing 批次落地
                后由 host 或 cli 注入 ``JsonlTraceSink``。
            approval: **底层** 人工审批 provider。默认使用
                :class:`_FailClosedApproval` 占位。装配层始终把它包进
                :class:`safety.SafetyGatedApproval`，并执行 DangerGuard →
                approval mode → thread permissions 三层决策后交给 runner。
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
            permissions_manager: thread permissions 唯一门户。Web 等多 runtime
                宿主可注入进程级共享实例；缺省时由安全链按 Kongming home 装配。
            message_compactor: 在 runner 每 turn 把 history 送给 LLM 之前做一次
                加工。``None`` 时使用默认的 :class:`prompting.HistoryCompactor`
                （超过 50 条时裁剪中段、截断超长 tool_result，永远保留首条 system
                和最近 20 条）。显式传 ``False`` 式占位对象可关闭；测试场景可传
                ``_NoopCompactor`` 之类的 stub。
            prompt_debug_sink: prompt debug 输出 sink；不传则关闭。
            instruction_origins: CLI / host 侧收集到的真实 instruction 来源列表，
                仅用于 prompt debug dump。
            conversation_reference_context: Web conversation reference 解析上下文；
                未传时使用当前进程 ``KONGMING_HOME`` 与工具上下文 cwd。
            asset_reader: 宿主层注入的附件 bytes 读取器；Web 上传路径传入
                ``AssetStorage``，CLI / cron 纯文本路径保持 ``None``。
            llm_provider: 显式注入的 LLMProvider。测试和 eval harness 可用它提供
                确定性 provider；未传时按 Config 构造 provider。

        Returns:
            装配好的 :class:`SessionEngine`，直接 ``await runtime.run(...)`` 即可。
        """
        catalog_manager = model_catalog_manager or ModelCatalogManager()
        resolved_model = model_config or resolve_model_config(
            config,
            catalog_manager=catalog_manager,
        )
        llm: LLMProvider
        if llm_provider is not None:
            llm = llm_provider
        else:
            llm = build_provider(
                config,
                asset_reader=asset_reader,
                catalog_manager=catalog_manager,
                resolved_model=resolved_model,
            )

        # Mapping[str, Tool] 结构上满足 ToolLookup；保留显式 if-else 以维持
        # 类型注解（三元表达式会丢掉 resolved_tools: ToolLookup 声明）。
        resolved_tools: ToolLookup
        if tools is None:
            resolved_tools = _EmptyToolLookup()
        else:
            resolved_tools = _snapshot_tool_lookup(tools)

        resolved_tool_names = list(enabled_tool_names or [])

        # 底层 approval：命中 ask 时用的 human-in-the-loop。
        base_approval: ApprovalProvider = approval or _FailClosedApproval()

        resolved_event_sinks: list[EventSink] = list(event_sinks or [])
        resolved_tool_context_metadata: dict[str, Any] = dict(tool_context_metadata or {})
        resolved_workspace = _metadata_cwd_path(resolved_tool_context_metadata)

        # 装配 DangerGuard → mode → thread permissions 三步安全链。
        safety_approval: SafetyGatedApproval = build_safety_chain(
            config,
            interactive_approval=base_approval,
            permissions_manager=permissions_manager,
            event_sinks=resolved_event_sinks,
            disposition_resolver=disposition_resolver,
        )

        resolved_session_factory = session_factory or (lambda sid: InMemorySession(session_id=sid))

        resolved_instructions = (
            instructions
            if instructions is not None and instructions.strip()
            else "You are kongming agent."
        )
        if agent_spec is None:
            resolved_spec = AgentSpec(
                name="default",
                instructions=resolved_instructions,
                default_model=resolved_model.name,
                tool_names=tuple(resolved_tool_names),
                max_turns=config.runner.max_turns,
                metadata={"model_preset_id": resolved_model.preset_id},
                reasoning_effort=resolved_model.default_reasoning_effort,
            )
        else:
            spec_metadata = dict(agent_spec.metadata)
            spec_metadata.setdefault("model_preset_id", resolved_model.preset_id)
            resolved_spec = replace(agent_spec, metadata=spec_metadata)

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
        # origin="" 表示"透传"：SessionEngine 接收的 instructions 可能已经是
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
        resolved_reference_context = conversation_reference_context or ConversationReferenceContext(
            home=get_kongming_home(),
            workspace=resolved_workspace or Path.cwd(),
        )
        input_assembler = InputAssembler(
            compactor=resolved_compactor,
            conversation_reference_manager=ConversationReferenceManager(resolved_reference_context),
        )

        runner = Runner(
            event_sinks=resolved_event_sinks,
            message_compactor=resolved_compactor,
            input_assembler=input_assembler,
            instruction_sources=agent_instruction_sources,
            prompt_debug_sink=prompt_debug_sink,
            instruction_origins=instruction_origins,
            stream_enabled=config.stream.enabled,
            suppress_content_after_tool_call=config.stream.suppress_content_after_tool_call,
            tool_context_metadata=resolved_tool_context_metadata,
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
            model_catalog_manager=catalog_manager,
            model_config=resolved_model,
            tool_context_metadata=resolved_tool_context_metadata,
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
        agent_spec: AgentSpec | None = None,
        max_turns: int | None = None,
        enabled_tools: Sequence[Tool] | object | None = _ENABLED_TOOLS_DEFAULT,
        lifecycle_hooks: Sequence[LifecycleHook] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
        llm_request_metadata: Mapping[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
        event_context: dict[str, Any] | None = None,
        thread_id: str | None = None,
        agent_id: str = "",
        llm_provider: LLMProvider | None = None,
        llm_tool_call_contract: LLMToolCallContract | None = None,
        execution_overrides: RunExecutionOverrides | None = None,
    ) -> Result:
        """执行一次"输入 → 结果"主链路。

        - 同一 ``session_id`` 反复调用会落到同一个 Session 实例上（多轮对话）。
        - ``session_id`` 为 ``None`` 时走匿名新 session。
        - ``reasoning_effort`` 非 None 时只覆盖本次 run 的 AgentSpec；
          Runner 会把该值写入 LLMRequest，provider 按请求级 effort 组装 payload。
        - ``agent_spec`` / ``enabled_tools`` / ``lifecycle_hooks`` 等 run
          级覆盖用于子 agent。``enabled_tools`` 未传时沿用 runtime 全局 enabled tools；
          显式传 ``None`` 时让 runner 按 ``agent_spec.tool_names`` 解析工具。
        - ``attachments`` / ``references`` 是用户结构化输入的 dict 列表，
          由 web ws.py 透传过来；CLI 路径默认 None 不影响行为。
        - ``agent_id``（agent-tree-v0.1 模块 G）：本次 run 的 agent 归属，默认 ``""``
          兼容单 agent；runner 写入 RunState.agent_id，各 Event / ToolContext 坐标字段
          从此取值。子 agent spawn（task-5）由 AgentManager 的 run_fn 闭包透传子 agent_id，
          让子 agent 的流式帧带正确 agent_id（task-1 已在 _S2CFrameBase 加 agent_id）。
        - ``event_context``：本次 run 的观测上下文，透传给 Runner 的 turn.start/end
          payload；典型来源是 mailbox Mail 的 epoch/kind/task_id。
        - ``thread_id``：顶层对话归属键。子 agent 显式沿用 root thread，未传时
          Runner 回落稳定 session id；子 session id 继续作为执行与审计身份。
        - ``execution_overrides``：一次 run 的 frozen 已装配依赖快照。scheduled
          thread 用它传入 fresh session、preset provider、经过 Safety 包装的任务级
          approval、工具视图、稳定 run ID、临时 sinks 和 tool context；普通 thread
          省略该参数并继续使用 runtime 默认依赖。
        """
        overrides = execution_overrides or RunExecutionOverrides()
        session = (
            overrides.session
            if overrides.session is not None
            else self._get_or_create_session(session_id)
        )
        run_spec = self._agent_spec_for_run(reasoning_effort, agent_spec=agent_spec)
        resolved_llm_request_metadata = self._llm_request_metadata_for_run(
            run_spec=run_spec,
            requested_effort=reasoning_effort,
            additional=llm_request_metadata,
        )
        resolved_enabled_tools: Sequence[Tool] | None
        if enabled_tools is _ENABLED_TOOLS_DEFAULT:
            resolved_enabled_tools = self._resolve_enabled_tools()
        else:
            resolved_enabled_tools = enabled_tools  # type: ignore[assignment]
        resolved_approval = self._approval
        if overrides.approval_transform is not None:
            resolved_approval = overrides.approval_transform(resolved_approval)
        return await self._runner.run(
            user_input,
            session=session,
            agent_spec=run_spec,
            llm=overrides.llm or llm_provider or self._llm,
            tools=overrides.tools if overrides.tools is not None else self._tools,
            approval=resolved_approval,
            max_turns=max_turns if max_turns is not None else self._config.runner.max_turns,
            run_id=overrides.run_id,
            enabled_tools=resolved_enabled_tools,
            attachments=attachments,
            references=references,
            lifecycle_hooks=(
                *self._lifecycle_hooks_snapshot(),
                *(lifecycle_hooks or ()),
            ),
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            llm_request_metadata=resolved_llm_request_metadata,
            event_context=event_context,
            thread_id=thread_id,
            agent_id=agent_id,
            llm_tool_call_contract=llm_tool_call_contract,
            event_sinks=overrides.event_sinks,
            tool_context_metadata=overrides.tool_context_metadata,
        )

    async def continue_from_last_user_message(
        self,
        *,
        session_id: str,
        reasoning_effort: str | None = None,
        event_context: dict[str, Any] | None = None,
        thread_id: str | None = None,
        agent_id: str = "",
    ) -> Result:
        """继续处理 session 里已持久化的最后一条 user message。

        Web 首发创建会先把 user message 写入 session，再调用本入口启动后续
        assistant run。这里复用 Runner 的继续入口，避免再次 append user message。
        """
        session = self._get_or_create_session(session_id)
        run_spec = self._agent_spec_for_run(reasoning_effort)
        resolved_llm_request_metadata = self._llm_request_metadata_for_run(
            run_spec=run_spec,
            requested_effort=reasoning_effort,
            additional=None,
        )
        return await self._runner.continue_from_last_user_message(
            session=session,
            agent_spec=run_spec,
            llm=self._llm,
            tools=self._tools,
            approval=self._approval,
            max_turns=self._config.runner.max_turns,
            enabled_tools=self._resolve_enabled_tools(),
            lifecycle_hooks=self._lifecycle_hooks_snapshot(),
            llm_request_metadata=resolved_llm_request_metadata,
            event_context=event_context,
            thread_id=thread_id,
            agent_id=agent_id,
        )

    def steer(self, session_id: str, request: SteerRequest) -> bool:
        """门户透传：把补充输入注入该 session 当前活跃 run 的 turn 边界（steer）。

        职责：本方法只做透传，把调用委托给唯一 run loop（:class:`Runner`）持有的
        steer 缓冲区，不在装配层复制任何注入逻辑（约束3：turn 推进/注入只在 Runner）。

        关键输入：
        - ``session_id``：目标 session；定位其当前活跃 run。
        - ``request``：待注入的补充输入（:class:`SteerRequest`），含 text 真源 +
          pending_input_id 消账主键，下一 turn 边界作为 user 消息注入。

        关键输出：
        - ``True``：命中活跃 run，已入 steer 缓冲，下一 turn 可见。
        - ``False``：无活跃 run（或该 run 正在收尾已拒收），调用方应回落到排队路径。
        """
        return self._runner.steer(session_id, request)

    def _agent_spec_for_run(
        self,
        reasoning_effort: str | None,
        *,
        agent_spec: AgentSpec | None = None,
    ) -> AgentSpec:
        """返回本次 run 使用的 AgentSpec，保持 runtime 默认 spec 不变。"""
        base_spec = agent_spec or self._agent_spec
        if reasoning_effort is None:
            return base_spec
        return replace(
            base_spec,
            reasoning_effort=coerce_reasoning_effort(reasoning_effort),
        )

    def _llm_request_metadata_for_run(
        self,
        *,
        run_spec: AgentSpec,
        requested_effort: str | None,
        additional: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """生成可进入 trace 的脱敏 catalog 与 reasoning 决策证据。"""
        metadata = dict(additional or {})
        runtime = self._model_config
        if runtime is None:
            return metadata
        effective_effort = run_spec.reasoning_effort
        plan = resolve_reasoning_plan(
            runtime.name,
            ReasoningConfig(
                enabled=effective_effort is not None,
                effort=effective_effort,
            ),
            runtime.reasoning,
        )
        metadata["model_catalog"] = {
            "version": runtime.catalog_version,
            "source": runtime.catalog_source.value,
            "provider_id": runtime.provider_id,
            "preset_id": runtime.preset_id,
            "remote_model": runtime.name,
        }
        metadata["reasoning_plan"] = {
            "requested_effort": requested_effort,
            "effective_effort": plan.requested_effort,
            "normalized_effort": plan.normalized_effort,
            "adapter": plan.adapter_name,
            "send_reasoning": plan.send_reasoning,
            "payload_keys": sorted(plan.payload_patch),
        }
        return metadata

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
    def model_config(self) -> ResolvedModelConfig | None:
        """返回本 runtime 绑定的 immutable 模型快照。"""
        return self._model_config

    @property
    def model_catalog_manager(self) -> ModelCatalogManager | None:
        """返回本 runtime 使用的 catalog 门户。"""
        return self._model_catalog_manager

    @property
    def llm(self) -> LLMProvider:
        """暴露 provider 实例。

        host 装配层（如 ``cli/main.py``）需要它来探测 ``SupportsLLMStream`` 能力，
        以决定是否在 CLI adapter 兜底渲染中跳过 final.content 重复输出（流式路径下
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
    def enabled_tools_snapshot(self) -> tuple[Tool, ...] | None:
        """返回默认 run 的实际工具快照，供同一 runtime 的 Agent 树继承与裁剪。"""
        resolved = self._resolve_enabled_tools()
        if resolved is not None:
            return tuple(resolved)
        fallback: list[Tool] = []
        for name in self._agent_spec.tool_names:
            if name not in self._tools:
                return None
            fallback.append(self._tools[name])
        return tuple(fallback)

    @property
    def approval(self) -> ApprovalProvider:
        """暴露已装配的高层 :class:`ApprovalProvider`（已包 SafetyGatedApproval）。"""
        return self._approval

    @property
    def session_factory(self) -> Callable[[str], Session]:
        """暴露 session 工厂（``session_id -> Session``）。"""
        return self._session_factory

    def get_or_create_session(self, session_id: str) -> Session:
        """返回 runtime 缓存中的指定 Session，不存在时通过工厂创建并缓存。"""
        return self._get_or_create_session(session_id)

    async def read_session_history(self, session_id: str) -> list[Message]:
        """通过 runtime 缓存真源读取指定 session 的结构化历史。"""
        session = self._get_or_create_session(session_id)
        return await session.history()

    async def append_session_message(
        self,
        session_id: str,
        message: Message,
        *,
        usage: ProviderUsageSnapshot | None = None,
    ) -> None:
        """通过 runtime 缓存真源向指定 session 追加一条结构化消息。"""
        session = self._get_or_create_session(session_id)
        await session.append(message, usage=usage)

    async def seed_empty_session_history(
        self,
        session_id: str,
        messages: Sequence[Message],
    ) -> None:
        """向空 session 播种历史，任一追加失败时清空已写入前缀。

        非空目标会在任何写入前失败。rollback 失败信息附加到原始异常，调用方
        仍收到触发播种失败的原始错误类型和 traceback。
        """
        session = self._get_or_create_session(session_id)
        if await session.history():
            raise ValueError(f"target session history must be empty: {session_id}")
        try:
            for message in messages:
                await session.append(message)
        except BaseException as exc:
            try:
                await session.clear()
            except BaseException as rollback_exc:
                exc.add_note(f"seed rollback failed: {type(rollback_exc).__name__}: {rollback_exc}")
            raise

    async def clear_session_history(self, session_id: str) -> None:
        """清空指定 session，供跨模块事务失败后的补偿路径使用。"""
        session = self._get_or_create_session(session_id)
        await session.clear()

    @property
    def event_sinks(self) -> list[EventSink]:
        """暴露 event sink 列表（副本，避免外部直改影响 runner fan-out）。"""
        return list(self._event_sinks)

    @property
    def tool_context_metadata(self) -> dict[str, Any]:
        """暴露默认工具上下文 metadata 副本，供子 agent / scheduler 显式继承。"""
        return dict(self._tool_context_metadata)

    def add_event_sink(self, sink: EventSink) -> None:
        """追加事件 sink 到底层 runner。"""
        self._event_sinks.append(sink)
        self._runner.add_event_sink(sink)

    def add_lifecycle_hook(self, hook: LifecycleHook) -> None:
        """追加 lifecycle hook，后续 run 按注册顺序触发。"""
        self._lifecycle_hooks.append(hook)

    def remove_lifecycle_hook(self, hook: LifecycleHook) -> bool:
        """按对象身份移除 lifecycle hook，返回是否移除成功。"""
        for index, registered in enumerate(self._lifecycle_hooks):
            if registered is hook:
                del self._lifecycle_hooks[index]
                return True
        return False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """关闭底层资源（目前只有 provider 的 httpx client）。

        幂等：provider 自身的 ``aclose`` 已做 None 检查，多次调不会抛。
        由 CLI / Web 宿主退出路径或测试 finally 触发。
        """
        aclose = getattr(self._llm, "aclose", None)
        if aclose is not None:
            await aclose()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _lifecycle_hooks_snapshot(self) -> tuple[LifecycleHook, ...]:
        """生成单次 run 的 lifecycle hook 快照，运行中保持稳定。"""
        return tuple(self._lifecycle_hooks)

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


__all__ = ["SessionEngine"]
