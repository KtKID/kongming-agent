"""高层安全链：v0.1.4 主决策链装配 + v0.1.3 兼容薄壳。

本模块的两件事：

1. :class:`SafetyGatedApproval` —— v0.1.3 暴露给上游（``tools/`` / ``host/`` /
   ``cli/``）的 :class:`core.contracts.ApprovalProvider` 实现。v0.1.4 起改为
   薄壳：构造 :class:`safety.types.RuntimeBoundaryContext` → 调
   :class:`safety.decision_engine.SafetyDecisionEngine` → 装饰
   ``metadata.stage`` 兼容字段后返回。
2. :func:`build_safety_chain` —— 装配入口。v0.1.4 内部装配新四件套
   （HardBlock / BoundaryResolver / TrustResolver / ConsentResolver）+
   SafetyDecisionEngine + EventSink fan-out。函数签名向上游零变更：
   ``capability_policy`` / ``permission_policy`` 仍为 keyword 参数（兼容期），
   新增的 ``boundary_resolver`` / ``grant_store`` / ``trace_emitter`` 等也
   走 keyword-only，默认 ``None``。

迁移说明：

- v0.1.3 的 ``capability_policy.check()`` / ``permission_policy.evaluate()``
  内部判定已被删除（保留方法签名 + ``raise NotImplementedError``）。``from_config``
  仍保留作为 v0.1.3 yaml 字段到 v0.1.4 三张规则表的迁移入口。
- ``metadata.stage`` 兼容字段保留：``hard_block`` → ``capability``、
  ``explicit_consent + standard`` → ``permission``、
  ``explicit_consent + elevated`` → ``approval``，
  ``silent_allow`` 不写 stage（v0.1.3 没这个概念）。

详见 ``docs/modules/安全/README.md`` 末尾的迁移指引。
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from config_loader.models import Config
from core.contracts import (
    ApprovalDecision,
    ApprovalProvider,
    ApprovalRequest,
)
from core.errors import AgentError
from safety.boundary_resolver import BoundaryResolver
from safety.capability_policy import CapabilityPolicy
from safety.decision_engine import SafetyDecisionEngine, TraceEmitter
from safety.grant_store import GrantStore, grant_with_now
from safety.guards.consent import ConsentResolver
from safety.guards.destructive import DestructiveForceAskGuard
from safety.guards.hard_block import HardBlockGuard
from safety.guards.trust import TrustResolver
from safety.permission_policy import PermissionPolicy
from safety.types import (
    ApprovalMetadataKeys,
    BoundaryKind,
    DecisionSource,
    RuntimeBoundaryContext,
)

if TYPE_CHECKING:
    from core.contracts import Event, EventSink

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class SafetyChainError(AgentError):
    """安全链内部异常。

    仅用于"安全链自身出错"——例如 SafetyDecisionEngine 抛了未预料的异常。
    HardBlock / Trust / Consent 各 guard 自己的"拒绝"不走这个异常，而是转成
    :class:`ApprovalDecision(outcome="rejected")`。
    """


# ---------------------------------------------------------------------------
# SafetyGatedApproval
# ---------------------------------------------------------------------------


class SafetyGatedApproval:
    """v0.1.4 安全链对外门面（薄壳）。

    实现 :class:`core.contracts.ApprovalProvider` Protocol（duck-typed）。
    ``decide`` 内部：

    1. 构造 :class:`RuntimeBoundaryContext` (``boundary_kind=HOST``)
    2. 调 :class:`SafetyDecisionEngine.decide`
    3. 装饰 ``metadata.stage`` 兼容字段（v0.1.3 → v0.1.4 平滑迁移）
    4. 返回 :class:`ApprovalDecision`

    上游 ``tools/`` / ``host/`` / ``cli/`` 接线零侵入。
    """

    def __init__(
        self,
        *,
        engine: SafetyDecisionEngine,
        grant_store: GrantStore,
        capability_policy: CapabilityPolicy | None = None,
        permission_policy: PermissionPolicy | None = None,
    ) -> None:
        self._engine = engine
        # 持 grant_store 引用：用于 ACCEPT_FOR_SESSION 写回（v0.1.4 缺口修复）
        # + 暴露给上游（NativeRuntime / web thread_manager evict 钩子）。
        self._grant_store = grant_store
        # 保留引用便于 v0.1.3 测试期间访问；本类不再调用其判定方法
        self._cap = capability_policy
        self._perm = permission_policy

    @property
    def grant_store(self) -> GrantStore:
        """暴露 grant_store 引用给上游（NativeRuntime / thread_manager）。

        thread_manager.evict_cell 用 ``grant_store.clear_session(session_id)``
        清理 thread 私有 grants；CLI 路径不需要这个引用（进程退出时 GC 回收）。
        """
        return self._grant_store

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """委托 SafetyDecisionEngine 给出决策；装饰 metadata.stage 兼容字段。

        如果 engine 抛任何未预料的异常，包成 :class:`SafetyChainError` 上抛。

        v0.1.6 修复：拿到 ``approved + grant_scope=session`` 的 decision 后，
        构造 ``Grant`` 写回 :class:`GrantStore` —— 让"本 session 同意"真正
        生效（之前 v0.1.4 留了空，导致 CLI [s] 是死代码、web "本 session 同意"
        点了等于 once）。
        """
        runtime = RuntimeBoundaryContext(boundary_kind=BoundaryKind.HOST)
        try:
            decision = await self._engine.decide(request, runtime)
        except Exception as exc:
            raise SafetyChainError(
                "SafetyDecisionEngine raised unexpectedly",
                details={
                    "tool_name": request.tool_name,
                    "call_id": request.call_id,
                    "error": repr(exc),
                },
            ) from exc

        decorated = _decorate_stage_compat(decision)
        self._maybe_write_session_grant(decorated, request)
        return decorated

    def _maybe_write_session_grant(
        self,
        decision: ApprovalDecision,
        request: ApprovalRequest,
    ) -> None:
        """ACCEPT_FOR_SESSION 写回 GrantStore。

        触发条件：``decision.outcome=="approved"`` + ``metadata.grant_scope=="session"``。

        Grant matcher 策略（P0 安全修复）：

        - ``run_shell``：提取命令前缀（第一个 word）作为 matcher。
          例：``ls /tmp`` → ``matcher="ls"``，``git status`` → ``matcher="git"``。
          解析失败时 fallback 到 ``"*"``（兼容旧行为）。
        - 非 shell 工具：保持 ``matcher="*"``（``memory`` / ``schedule`` 等）。

        ``request.session_id`` 缺失时静默跳过（防御性）。
        """
        metadata = decision.metadata or {}
        if decision.outcome != "approved":
            return
        if metadata.get(ApprovalMetadataKeys.GRANT_SCOPE) != "session":
            return
        if not request.session_id:
            return

        # shell 命令：按命令前缀细化 matcher
        matcher = "*"
        if request.tool_name == "run_shell":
            cmd = (request.arguments or {}).get("command")
            if isinstance(cmd, str):
                if not cmd.strip():
                    # 空/纯空白命令不写 grant（避免 fallback 到 "*" 过度授权）
                    return
                cmd_base = extract_command_base(cmd)
                if cmd_base:
                    matcher = cmd_base

        grant = grant_with_now(
            capability=f"tool:{request.tool_name}",
            matcher=matcher,
            scope="session",
            source=DecisionSource.SESSION,
            request_id=request.call_id,
            session_id=request.session_id,
        )
        self._grant_store.put_session(grant)

    def _legacy_decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """v0.1.3 兼容期 fallback。

        v0.1.4 已删除 v0.1.3 内部判定路径，本方法保留为占位防止外部直接调用，
        如需恢复请重新实现。
        """
        raise RuntimeError(
            "SafetyGatedApproval._legacy_decide: legacy v0.1.3 path removed; "
            "v0.1.4 主链路只走 SafetyDecisionEngine。"
        )


# ---------------------------------------------------------------------------
# metadata.stage 兼容映射
# ---------------------------------------------------------------------------


def _decorate_stage_compat(decision: ApprovalDecision) -> ApprovalDecision:
    """按 v0.1.3 → v0.1.4 映射规则补 ``metadata.stage`` 兼容字段。

    映射规则：

    - ``decision_class=hard_block`` → ``stage="capability"``
    - ``decision_class=silent_allow`` → 不写 stage（v0.1.3 没有 silent_allow 概念）
    - ``decision_class=explicit_consent`` + ``decision_source=standard`` →
      ``stage="permission"``
    - ``decision_class=explicit_consent`` + ``decision_source=elevated`` →
      ``stage="approval"``

    已有 ``stage`` 字段时不覆盖（HardBlockGuard / ConsentResolver 已自行写入）。
    """
    metadata = dict(decision.metadata or {})
    if ApprovalMetadataKeys.STAGE in metadata:
        # 已有 stage（HardBlockGuard / ConsentResolver 自行写入），保持不变
        return decision

    decision_class = metadata.get(ApprovalMetadataKeys.DECISION_CLASS)
    decision_source = metadata.get(ApprovalMetadataKeys.DECISION_SOURCE)

    stage: str | None = None
    if decision_class == "hard_block":
        stage = "capability"
    elif decision_class == "explicit_consent":
        stage = "approval" if decision_source == "elevated" else "permission"
    # silent_allow 不写 stage

    if stage is None:
        return decision

    metadata[ApprovalMetadataKeys.STAGE] = stage
    return replace(decision, metadata=metadata)


# ---------------------------------------------------------------------------
# build_safety_chain
# ---------------------------------------------------------------------------


def build_safety_chain(
    config: Config,
    *,
    interactive_approval: ApprovalProvider,
    capability_policy: CapabilityPolicy | None = None,
    permission_policy: PermissionPolicy | None = None,
    boundary_resolver: BoundaryResolver | None = None,
    grant_store: GrantStore | None = None,
    trace_emitter: TraceEmitter | None = None,
    event_sinks: list[EventSink] | None = None,
) -> SafetyGatedApproval:
    """按统一配置 + 底层 approval 装配一条完整的高层安全链。

    Args:
        config: 统一 :class:`Config`。三张规则表 + grant 配置都从这里读。
        interactive_approval: 底层 :class:`ApprovalProvider`（通常是
            :class:`tools.runtime.approval.InteractiveApproval` 或
            :class:`tools.runtime.approval.AutoAllowApproval` 等）。
        capability_policy: v0.1.3 兼容字段；本次实现仅作占位保留，规则迁移
            通过 ``CapabilityPolicy.from_config`` 在运行时返回的对象只影响
            内部 ``hard_deny_commands`` 增量（HardBlockGuard 已合并）。
        permission_policy: v0.1.3 兼容字段；同上，规则迁移通过
            ``PermissionPolicy.from_config`` 在运行时把老 yaml 字段翻译到新
            规则表。
        boundary_resolver: 显式注入的 :class:`BoundaryResolver`；``None`` 走
            :meth:`BoundaryResolver.from_project_root`。
        grant_store: 显式注入的 :class:`GrantStore`；``None`` 走
            :meth:`GrantStore.from_config`。
        trace_emitter: trace callback；``None`` 时若提供 ``event_sinks``，自动
            构造一个把 EventSink 列表 fan-out 的 emitter。
        event_sinks: ``EventSink`` 列表；当 ``trace_emitter`` 为 ``None`` 时，
            本参数用于自动构造 emitter。

    Returns:
        :class:`SafetyGatedApproval` 实例，满足
        :class:`core.contracts.ApprovalProvider` Protocol。
    """
    boundary = boundary_resolver or BoundaryResolver.from_project_root()
    grants = grant_store or GrantStore.from_config(config)
    hard_block = HardBlockGuard.from_config(config)
    destructive = DestructiveForceAskGuard.from_config(config)
    trust = TrustResolver(boundary, grants)
    consent = ConsentResolver.from_config(config, interactive_approval=interactive_approval)

    # 自动 emitter：trace_emitter 显式给优先；否则按 event_sinks 装一个
    resolved_emitter: TraceEmitter | None = trace_emitter
    if resolved_emitter is None and event_sinks:
        resolved_emitter = _build_event_sink_emitter(
            event_sinks=event_sinks,
            log_silent_reads=config.safety.log_silent_reads,
        )

    engine = SafetyDecisionEngine(
        hard_block=hard_block,
        boundary=boundary,
        destructive=destructive,
        trust=trust,
        consent=consent,
        trace_emitter=resolved_emitter,
    )

    return SafetyGatedApproval(
        engine=engine,
        grant_store=grants,
        capability_policy=capability_policy,
        permission_policy=permission_policy,
    )


# ---------------------------------------------------------------------------
# EventSink fan-out 适配器
# ---------------------------------------------------------------------------


# read 类工具集合（决定 silent_allow 是否落 trace）
_READ_TOOL_NAMES: frozenset[str] = frozenset({"read_file", "list_dir"})

# 持有 sink emit 任务引用，避免 GC 提前回收（RUF006 / fire-and-forget pattern）。
# 模块级 set 而非实例属性：emitter 是闭包，没有自然挂载点；同进程多个
# build_safety_chain 共享一份 task pool 不影响正确性。
_PENDING_EMIT_TASKS: set[Any] = set()


def _build_event_sink_emitter(
    *,
    event_sinks: list[EventSink],
    log_silent_reads: bool,
) -> TraceEmitter:
    """构造把 EventSink 列表 fan-out 的同步 emitter。

    SafetyDecisionEngine 的 ``trace_emitter`` 是同步回调（避免主决策路径上
    引入 await）；EventSink 是异步接口。这里用 ``asyncio.ensure_future``
    把 emit 调度到 loop，避免阻塞。

    写盘控制：

    - read 类工具的 ``tool.silently_allowed`` 默认不写（除非
      ``log_silent_reads=True``）；write 类总写。
    - ``tool.denied`` / ``tool.approval_required`` / ``tool.destructive_force_ask`` 总写。
    """
    from core.contracts import Event

    def _emit(
        event_kind: str,
        decision: ApprovalDecision,
        request: ApprovalRequest,
    ) -> None:
        # read silent 写盘控制（destructive_force_ask 永远写盘，不过滤）
        if (
            event_kind == "tool.silently_allowed"
            and request.tool_name in _READ_TOOL_NAMES
            and not log_silent_reads
        ):
            return

        payload = _build_event_payload(decision, request)
        event = Event(
            kind=event_kind,
            run_id=request.run_id,
            turn=request.turn,
            payload=payload,
        )
        for sink in event_sinks:
            _safely_dispatch_emit(sink, event)

    return _emit


def _safely_dispatch_emit(sink: EventSink, event: Event) -> None:
    """把异步 emit 调度到当前 event loop；无 loop 时退化为创建临时 loop。"""
    import asyncio
    import contextlib

    coro = sink.emit(event)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 不在 event loop 内（同步上下文）。这种场景在生产中极少出现
        # （SafetyDecisionEngine.decide 是 async，调用方一定在 loop 内）；
        # 测试调用 _emit 时可能命中。回退到 asyncio.run 的简单兜底，
        # 避免吞掉异常但也不阻塞主流程。
        with contextlib.suppress(Exception):
            asyncio.run(coro)
        return
    # 在 loop 内：调度成 task。store reference to avoid premature gc (RUF006)。
    task = loop.create_task(coro)
    # 把 task 引用塞到 set 里，sink 完成后由 done callback 移除。
    _PENDING_EMIT_TASKS.add(task)
    task.add_done_callback(_PENDING_EMIT_TASKS.discard)


def _build_event_payload(
    decision: ApprovalDecision,
    request: ApprovalRequest,
) -> dict[str, Any]:
    """从 ApprovalDecision + ApprovalRequest 拼出 trace event payload。

    字段对齐 ``docs/safety-scope-v0.1.4/04-data-and-state.md`` § 事件模型：
    ``decision_class`` / ``decision_source`` / ``matched_rule`` / ``reason`` /
    ``boundary_kind`` / ``tool_name`` / ``path_or_command`` / ``request_id``。
    """
    metadata = decision.metadata or {}
    payload: dict[str, Any] = {
        "decision_class": metadata.get(ApprovalMetadataKeys.DECISION_CLASS),
        "decision_source": metadata.get(ApprovalMetadataKeys.DECISION_SOURCE),
        "matched_rule": metadata.get(ApprovalMetadataKeys.MATCHED_RULE),
        "reason": metadata.get(ApprovalMetadataKeys.REASON) or decision.reason,
        "boundary_kind": metadata.get(ApprovalMetadataKeys.BOUNDARY_KIND, "host"),
        "grant_scope": metadata.get(ApprovalMetadataKeys.GRANT_SCOPE),
        "tool_name": request.tool_name,
        "path_or_command": _extract_path_or_command(request),
        "request_id": request.call_id,
        "outcome": decision.outcome,
    }
    # shell 命令：追加完整 command 文本（便于审计 destructive 操作）
    if request.tool_name == "run_shell":
        cmd = (request.arguments or {}).get("command")
        if isinstance(cmd, str):
            payload["command"] = cmd
    return payload


def _extract_path_or_command(request: ApprovalRequest) -> str | None:
    """从 ApprovalRequest.arguments 提取 path 或 command 字段。"""
    args = request.arguments or {}
    path = args.get("path")
    if isinstance(path, str):
        return path
    command = args.get("command")
    if isinstance(command, str):
        return command
    return None


# shell 分隔符正则（复用 hard_block 的逻辑）
_CMD_SEGMENT_SPLITTER = re.compile(r"(?:&&|\|\||;|\|)")


def extract_command_base(command: str) -> str:
    """从 shell 命令提取命令前缀（第一段的第一个 word）。

    用于 session grant 的 matcher 细化：``ls /tmp`` → ``"ls"``，
    ``cd /tmp && rm -rf foo`` → ``"cd"``。

    逻辑：
    1. 按 ``&&`` / ``||`` / ``;`` / ``|`` 拆段
    2. 取第一段
    3. strip 后取第一个 word（空格分隔）
    4. 返回小写

    返回空串表示解析失败。
    """
    if not command or not command.strip():
        return ""
    segments = _CMD_SEGMENT_SPLITTER.split(command)
    first_segment = segments[0].strip() if segments else ""
    if not first_segment:
        return ""
    first_word = first_segment.split(maxsplit=1)[0]
    return first_word.lower()


__all__ = [
    "SafetyChainError",
    "SafetyGatedApproval",
    "build_safety_chain",
    "extract_command_base",
]
