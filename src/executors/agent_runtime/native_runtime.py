"""进程内运行时装配层。

按 :file:`docs/kongming-agent-v1-minimal/10-contracts.md` 中"Native Runtime
边界"的约定：

    runner 负责跑，native_runtime 负责装，cli/main 负责进。

所以这里**不**自持第二份 turn loop、不复制任何 :class:`core.run_state.RunState`
相关状态。所有 turn 推进都走 :class:`core.runner.Runner`。

**依赖方向**：

- 只 import ``core`` 协议 / 数据结构、``config_loader`` 配置、本包自己的
  ``executors/llm`` provider，以及 ``safety/`` 下的安全链装配入口。
- 对 ``safety/`` 的依赖是"装配层 → 下层 policy"的向下引用，属于运行时装配
  职责的一部分（已在 ``.importlinter`` 中显式白名单化，避免触犯 layered 合约）。
- 绝不 import ``tools/`` / ``host/`` / ``cli/`` / ``observability/`` /
  ``context/`` 里的任何具体类。调用方（host 或 cli 或 tests）通过 ``build(...)``
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

from collections.abc import Callable, Mapping, Sequence

from config_loader.models import Config
from context import HistoryCompactor
from context.history_compactor import CompactorConfig
from core.agent_spec import AgentSpec
from core.contracts import (
    ApprovalDecision,
    ApprovalProvider,
    ApprovalRequest,
    EventSink,
    LLMProvider,
    MessageCompactor,
    Session,
    Tool,
    ToolLookup,
)
from core.result import Result
from core.runner import Runner
from core.session import InMemorySession
from executors.llm.anthropic_messages import AnthropicMessagesProvider
from executors.llm.openai_responses import OpenAIResponsesProvider
from safety import (
    CapabilityPolicy,
    PermissionPolicy,
    SafetyGatedApproval,
    build_safety_chain,
)

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
    ) -> NativeRuntime:
        """按 Config 装配一份默认 runtime。

        Args:
            config: 从 :func:`config_loader.load_config` 拿到的 :class:`Config`。
            event_sinks: 事件落地 sinks。默认空列表；observability 批次落地
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
                通过 :class:`context.InstructionLoader` 把 agent_spec / 外部文件 /
                环境变量合并后传入。显式传 ``agent_spec`` 时，本参数被忽略。
            capability_policy: 显式注入的 :class:`safety.CapabilityPolicy`。
                ``None`` 时走 :meth:`CapabilityPolicy.from_config` 自动装配。
            permission_policy: 显式注入的 :class:`safety.PermissionPolicy`。
                ``None`` 时走 :meth:`PermissionPolicy.from_config` 自动装配。
            message_compactor: 在 runner 每 turn 把 history 送给 LLM 之前做一次
                加工。``None`` 时使用默认的 :class:`context.HistoryCompactor`
                （超过 50 条时裁剪中段、截断超长 tool_result，永远保留首条 system
                和最近 20 条）。显式传 ``False`` 式占位对象可关闭；测试场景可传
                ``_NoopCompactor`` 之类的 stub。

        Returns:
            装配好的 :class:`NativeRuntime`，直接 ``await runtime.run(...)`` 即可。
        """
        if config.model.provider == "anthropic":
            llm: LLMProvider = AnthropicMessagesProvider(
                model_config=config.model,
                max_retries=config.retry.max_retries,
                retry_backoff=config.retry.retry_backoff,
            )
        else:
            llm = OpenAIResponsesProvider(
                model_config=config.model,
                max_retries=config.retry.max_retries,
                retry_backoff=config.retry.retry_backoff,
                enable_raw_dump=config.trace.raw_llm,
            )

        resolved_tools: ToolLookup
        if tools is None:
            resolved_tools = _EmptyToolLookup()
        else:
            resolved_tools = tools  # Mapping[str, Tool] 结构上满足 ToolLookup

        resolved_tool_names = list(enabled_tool_names or [])

        # 底层 approval：命中 ask 时用的 human-in-the-loop。
        base_approval: ApprovalProvider = approval or _AllowAllApproval()

        # 把 capability / permission / approval 串成高层安全链；
        # 这是最终传给 runner 的 ApprovalProvider。
        safety_approval: SafetyGatedApproval = build_safety_chain(
            config,
            interactive_approval=base_approval,
            capability_policy=capability_policy,
            permission_policy=permission_policy,
        )

        resolved_session_factory = session_factory or (lambda sid: InMemorySession(session_id=sid))

        resolved_event_sinks: list[EventSink] = list(event_sinks or [])

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
        )

        resolved_compactor: MessageCompactor = message_compactor or HistoryCompactor(
            config=CompactorConfig(
                max_messages=config.compactor.max_messages,
                keep_recent=config.compactor.keep_recent,
                keep_system=config.compactor.keep_system,
                tool_result_max_chars=config.compactor.tool_result_max_chars,
            )
        )

        runner = Runner(
            event_sinks=resolved_event_sinks,
            message_compactor=resolved_compactor,
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
    ) -> Result:
        """执行一次"输入 → 结果"主链路。

        - 同一 ``session_id`` 反复调用会落到同一个 Session 实例上（多轮对话）。
        - ``session_id`` 为 ``None`` 时走匿名新 session。
        """
        session = self._get_or_create_session(session_id)
        return await self._runner.run(
            user_input,
            session=session,
            agent_spec=self._agent_spec,
            llm=self._llm,
            tools=self._tools,
            approval=self._approval,
            max_turns=self._config.runner.max_turns,
            enabled_tools=self._resolve_enabled_tools(),
        )

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

    def add_event_sink(self, sink: EventSink) -> None:
        """追加事件 sink 到底层 runner。"""
        self._event_sinks.append(sink)
        self._runner.add_event_sink(sink)

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


__all__ = ["NativeRuntime"]
