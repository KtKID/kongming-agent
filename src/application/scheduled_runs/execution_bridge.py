"""scheduled run 用例执行桥。

把一个 :class:`scheduler.domain.DueTaskReservation` 转成一次 fresh
``Runner.run()``，并把结果回写为 :class:`scheduler.domain.ScheduledRun`。

设计要点（参见 ``docs/agent-cron-module-v0.1/03-core-workflows.md`` §3 / §4
与 ``docs/agent-cron-module-v0.1/02-module-breakdown.md`` 模块 4）：

- fresh ``session_id`` / ``run_id``；不复用旧会话
- 装配期裁掉 ``schedule.*`` / ``cron.*`` 工具，防止 cron run 内自我繁殖
- 把现有 :class:`core.contracts.ApprovalProvider` 包一层
  :class:`scheduler.safety_wrapper.ScheduleApprovalProvider`，cron run 命中
  consent 的高风险动作直接 deny → 工具失败 → 整 run failed(needs_approval)
- ``InactivityWatchdog`` 作为 :class:`core.contracts.EventSink` 接入 runner
  的 sink fan-out，在每个活动事件刷新 last_activity_ts；超时取消 runner 协程
- ``[SILENT]`` 投递抑制：final message 命中 ``[SILENT]`` 标记时
  ``ScheduledRun.status = SILENT``、``silent_suppressed=True``，仍正常落盘审计
- 所有错误进 ``ScheduledRun.error_message`` / ``failure_reason``，不向上抛
  ``asyncio.CancelledError``

边界：
- 不引入新异常类、不新增 EventSink Protocol
- 不直接 import ``safety.*``（沿用 scheduler 执行边界）
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from core.agent_spec import AgentSpec
from core.contracts import (
    ApprovalProvider,
    Event,
    EventSink,
    LLMProvider,
    Session,
    Tool,
    ToolLookup,
)
from core.message import Message
from core.result import Result
from core.runner import Runner
from infrastructure.config.models import SchedulerApprovalConfig
from scheduler.delivery import DeliveryDispatcher
from scheduler.domain import (
    DEFAULT_INACTIVITY_TIMEOUT,
    ApprovalMode,
    DueTaskReservation,
    RunFailureReason,
    RunStatus,
    ScheduledRun,
    ScheduledRunRequest,
    ScheduledTask,
    TaskExecutionContext,
    resolve_effective_mode,
)
from scheduler.policy import apply_concurrency_policy
from scheduler.safety_wrapper import ScheduleApprovalProvider
from scheduler.silent import is_silent, strip_silent_prefix
from scheduler.store import Store
from scheduler.timing import to_iso, utc_now

if TYPE_CHECKING:
    from infrastructure.config.models import Config, LLMPresetConfig
    from infrastructure.tracing import JsonlTraceSink

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------


_FALLBACK_MAX_TURNS_NO_CONFIG = 90
"""v0.5.1: ``base_config is None`` 时的最后兜底 max_turns。

正常路径走 ``cfg.scheduler.default_max_turns``（也默认 90），本常量仅在装配方
完全没传 base_config 时（极少数 CLI 测试场景）使用，与 config 默认保持一致。
"""

_DEFAULT_POLL_INTERVAL = 5.0
"""watchdog 默认轮询间隔（秒）。文档 §3 约定 5s。"""

_FINAL_MESSAGE_EXCERPT_LIMIT = 512
"""``ScheduledRun.final_message_excerpt`` 最大字符数。"""

_DISALLOWED_TOOL_PREFIXES: tuple[str, ...] = ("schedule.", "cron.")
"""cron run 装配期裁掉的工具名前缀（防递归创建任务）。"""

_DISALLOWED_TOOL_NAMES: frozenset[str] = frozenset({"schedule", "cron"})
"""cron run 装配期裁掉的单字工具名（防递归创建任务）。

v0.2 新增 :class:`tools.builtin.schedule_tool.ScheduleTool`，其 ``name`` 是单字
``"schedule"``——既不带 ``.`` 也不命中现有前缀。这里显式黑名单单字命中，
保留前缀机制供未来 namespace 化的 schedule.* / cron.* 工具使用。
"""


def _is_disallowed_tool_name(name: str) -> bool:
    """判定工具名是否在 cron run 装配期被裁掉。"""
    return name in _DISALLOWED_TOOL_NAMES or name.startswith(_DISALLOWED_TOOL_PREFIXES)


_ACTIVITY_KINDS: frozenset[str] = frozenset(
    {
        "tool.call.start",
        "tool.call.end",
        "content.delta",
        "reasoning.delta",
        "llm.chunk.first",
        "llm.stream.end",
        "turn.start",
        "llm.request",
        "llm.response",
    }
)
"""watchdog 视作"agent 仍在活动"的事件 kind 集合。"""


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------


class InactivityWatchdog:
    """连续无活动 N 秒强制取消 runner 协程的 watchdog。

    同时实现 :class:`core.contracts.EventSink` 协议：每收到一个属于
    :data:`_ACTIVITY_KINDS` 的事件就刷新 ``last_activity_ts``；其他事件不刷新
    （比如 ``approval.request`` 视为业务流程内部状态，不算活动）。

    使用方式：
    1. 把 watchdog 实例 append 到 runner 的 event sinks 里
    2. ``runner_task = asyncio.create_task(runner.run(...))``
    3. ``watch_task = asyncio.create_task(watchdog.watch(runner_task))``
    4. ``await runner_task``；finally ``watch_task.cancel()``

    ``timeout_seconds is None`` / ``<= 0`` 时 watchdog 不取消任务（仍可作为
    sink 收事件，无副作用），用于"禁用 watchdog 但 sink 链路保持"场景。
    """

    def __init__(
        self,
        *,
        timeout_seconds: int | None,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL,
        activity_kinds: frozenset[str] = _ACTIVITY_KINDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._activity_kinds = activity_kinds
        self._clock = clock
        self._last_activity_ts: float = clock()
        self._triggered: bool = False

    @property
    def triggered(self) -> bool:
        """watchdog 是否已经触发过 cancel。"""
        return self._triggered

    def reset(self) -> None:
        """启动前重置时间戳，复用同一实例时使用。"""
        self._last_activity_ts = self._clock()
        self._triggered = False

    async def emit(self, event: Event) -> None:
        """:class:`EventSink` 协议实现：命中活动 kind 即刷新时间戳。"""
        if event.kind in self._activity_kinds:
            self._last_activity_ts = self._clock()

    async def watch(self, target_task: asyncio.Task[Result]) -> None:
        """轮询直至 ``target_task`` 结束或超时。

        - ``timeout_seconds`` 不启用（``None`` / ``<= 0``）→ 直接返回，不取消
        - 超时命中 → ``target_task.cancel()`` 后退出；``triggered=True``
        - target 已完成 → 直接返回
        """
        timeout = self._timeout_seconds
        if timeout is None or timeout <= 0:
            return
        while True:
            if target_task.done():
                return
            elapsed = self._clock() - self._last_activity_ts
            if elapsed >= timeout:
                self._triggered = True
                target_task.cancel()
                return
            try:
                await asyncio.sleep(self._poll_interval_seconds)
            except asyncio.CancelledError:
                # 上层主动取消 watch（runner 已结束），安静退出。
                return


# ---------------------------------------------------------------------------
# ToolLookup 适配
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FilteredToolLookup:
    """按白名单从父 ToolLookup 暴露子集。

    :class:`core.runner.Runner` 用 ``__contains__`` / ``__getitem__`` 做
    名→工具查询；本类满足同样的结构性 Protocol，但只放行 ``allowed``
    集合内的名字。
    """

    parent: ToolLookup
    allowed: frozenset[str]

    def __contains__(self, name: object) -> bool:
        return name in self.allowed and name in self.parent

    def __getitem__(self, name: str) -> Tool:
        if name not in self.allowed:
            raise KeyError(name)
        return self.parent[name]


# ---------------------------------------------------------------------------
# EventSink 聚合（v0.5 cron approval mode audit）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AggregateEventSink:
    """聚合多个 :class:`EventSink` 的 emit 调用，供 ScheduleApprovalProvider 使用。

    runner 已经持 ``list[EventSink]`` 用于事件分发，但
    :class:`scheduler.safety_wrapper.ScheduleApprovalProvider` 在装配阶段需要
    **单一** sink 引用（其 ``event_sink: EventSink | None`` 字段）；本类把
    bridge 持有的多 sink 包装成一个统一 EventSink，逐个 emit 给下游。

    某个 sink emit 失败不阻塞其他 sink（防御性 try/except）；ScheduleApproval
    wrapper 自身也在外层 try/except 兜底，保证 sink 异常不影响审批主链路。
    """

    sinks: Sequence[EventSink]

    async def emit(self, event: Event) -> None:
        for sink in self.sinks:
            # 单个 sink 失败不影响其他；wrapper 也已在外层用 try/except 兜底
            with contextlib.suppress(Exception):
                await sink.emit(event)


@dataclass(frozen=True)
class _CronAuditWriterSink:
    """监听 approval.cron.auto_allow event 转写为 audit 行（v0.5 新增）。

    bridge 装配时把本 sink 注入 wrapper 的 event sink 链；
    wrapper trust 自动放行 emit event 时，本 sink 把 event payload 关键字段
    映射到 ``store.append_audit(action="run_approval_auto_allow", ...)``。

    职责单一：只关心 ``approval.cron.auto_allow``，其他 kind 跳过；
    audit 写入失败用 :func:`contextlib.suppress` 兜底，不阻塞决策链。

    设计动机：把"event 落 audit"作为独立 sink，避免 :class:`_AggregateEventSink`
    感知 audit 概念；store / task_id 在装配阶段由 bridge 注入，调用阶段无副作用。
    """

    store: Store
    task_id: str
    preset_id: str = ""
    """v0.5.2: task 显式声明的 preset_id（空串表示未声明 preset → 走默认
    provider，即 cli/web 装配时由 ``cfg.model.*`` 构造的 ``self._llm``）。"""
    model_name: str = ""
    """v0.5.2: 装配后实际生效的模型名（preset.model 或 cfg.model.name）。"""

    async def emit(self, event: Event) -> None:
        if event.kind != "approval.cron.auto_allow":
            return
        payload = event.payload or {}
        with contextlib.suppress(Exception):
            self.store.append_audit(
                action="run_approval_auto_allow",
                task_id=self.task_id,
                actor="scheduler",
                payload={
                    "tool_name": payload.get("tool_name"),
                    "original_decision_class": payload.get("original_decision_class"),
                    "original_decision_source": payload.get("original_decision_source"),
                    "matched_rule": payload.get("matched_rule"),
                    "arguments_digest": payload.get("arguments_digest"),
                    "preset_id": self.preset_id,
                    "model_name": self.model_name,
                },
            )


# ---------------------------------------------------------------------------
# ExecutionBridge
# ---------------------------------------------------------------------------


class ExecutionBridge:
    """把 due reservation 转成一次 fresh ``Runner.run()`` 并写回 ``ScheduledRun``。

    该类是装配层：构造时收所有依赖；:meth:`execute` 不做装配只跑流程。
    """

    def __init__(
        self,
        *,
        runner: Runner,
        llm: LLMProvider,
        tools: ToolLookup,
        enabled_tool_names: Sequence[str],
        inner_approval: ApprovalProvider,
        session_factory: Callable[[str], Session],
        event_sinks: Sequence[EventSink],
        agent_spec: AgentSpec,
        store: Store,
        dispatcher: DeliveryDispatcher | None = None,
        watchdog_poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL,
        preset_map: dict[str, LLMPresetConfig] | None = None,
        base_config: Config | None = None,
        trace_dir: Path | None = None,
    ) -> None:
        self._runner = runner
        self._llm = llm
        self._tools = tools
        self._enabled_tool_names = tuple(enabled_tool_names)
        self._inner_approval = inner_approval
        self._session_factory = session_factory
        self._event_sinks: list[EventSink] = list(event_sinks)
        self._agent_spec = agent_spec
        self._store = store
        # v0.3 cron-delivery M3：投递路由器；None 时不调投递（保持 v0.2 行为）。
        # 装配方（cli/web）通过 runtime_factory 传入；M4/M5 提供具体 sink 实现。
        self._dispatcher = dispatcher
        self._watchdog_poll_interval_seconds = watchdog_poll_interval_seconds
        # v0.4 per-task LLM preset：根据 task.preset_id 构建独立 provider。
        self._preset_map = preset_map
        self._base_config = base_config
        # v0.4 per-run trace：每次 cron run 写独立 jsonl trace 文件。
        self._trace_dir = trace_dir

    # ------------------------------------------------------------------
    # per-task LLM provider 构建
    # ------------------------------------------------------------------

    def _build_provider(self, preset_id: str) -> LLMProvider:
        """根据 ``preset_id`` 构建独立 LLM provider。

        两种合法路径：

        1. **未启用 preset 体系**（``preset_id`` 为空 **或** ``preset_map`` 为
           ``None``）→ 返回 ``self._llm``，即 cli/web 装配时通过
           :func:`infrastructure.llm_providers.provider_factory.build_provider` 用 ``cfg.model``
           构造的**默认 provider**。``cfg.model`` 字段优先级（高 → 低）：

           - env：``KONGMING_MODEL_BASE_URL`` / ``KONGMING_MODEL_NAME`` /
             ``KONGMING_MODEL_API_KEY``（见 :class:`infrastructure.config.models.ModelConfig`）
           - ``config/setting.yaml`` 的 ``model:`` 段
           - dataclass 默认值

           这条路径不是 fallback，是"未启用 preset 功能"的合法默认。

        2. **启用了 preset 且命中**（``preset_id in preset_map``）→ 用 preset
           覆盖 ``cfg.model`` 后构造独立 provider，调用方负责 ``aclose``。

        **错配场景（v0.5.3 改）**：装配方启用了 preset 体系但
        ``task.preset_id`` 在 ``preset_map`` 里找不到（key 拼错 / 配置漂移），
        **不再静默 fallback 到默认 provider**，直接抛 :class:`ValueError`，由
        :meth:`execute` 捕获转 FAILED run + 写日志，把错配暴露给用户。
        """
        # 未启用 preset 体系 → 走默认 provider（self._llm = cfg.model.*）
        if not preset_id or not self._preset_map:
            return self._llm
        # 启用了 preset 但 task 写的 preset_id 不存在 → 抛错，不偷换默认
        if preset_id not in self._preset_map:
            raise ValueError(
                f"scheduled task preset_id {preset_id!r} not found in preset_map "
                f"(available presets: {sorted(self._preset_map.keys())})"
            )
        from infrastructure.llm_providers.provider_factory import apply_preset, build_provider

        preset = self._preset_map[preset_id]
        cfg = apply_preset(self._base_config, preset)  # type: ignore[arg-type]
        return build_provider(cfg)

    def _resolve_effective_agent_spec(self, preset_id: str) -> AgentSpec:
        """v0.5.3：preset 命中时返回覆盖了 ``default_model`` /
        ``reasoning_effort`` 的 per-run :class:`AgentSpec`；否则返回装配期
        ``self._agent_spec``。

        修复 v0.4 切 preset 时只换 provider 不换 agent_spec 的"半拉子" bug。
        :class:`core.runner.Runner` 组装 :class:`core.contracts.LLMRequest`
        时把 ``agent_spec.default_model`` 作为 ``request.model`` 透传给
        provider；如果不在此处一起覆盖，请求体 ``"model"`` 字段仍是装配期
        ``cfg.model.name``（env ``KONGMING_MODEL_NAME`` 默认值），导致
        ``base_url=preset`` + ``model=默认`` 的错配——后端返回"模型不存在"。

        ``reasoning_effort``：preset 未声明时保留 spec 现值，避免 ``None``
        覆盖装配期已存在的设置。
        """
        if not preset_id or not self._preset_map or preset_id not in self._preset_map:
            return self._agent_spec
        preset = self._preset_map[preset_id]
        # dataclasses.replace 对 **dict[str, str] 报 invariance 错；直接列出
        # 字段保持类型安全，分支处理 reasoning_effort 是否为 None。
        if preset.reasoning_effort is not None:
            return replace(
                self._agent_spec,
                default_model=preset.model,
                reasoning_effort=preset.reasoning_effort,
            )
        return replace(self._agent_spec, default_model=preset.model)

    def _resolve_run_audit_context(self, task: ScheduledTask) -> dict[str, str]:
        """v0.5.2: 解析 cron run audit payload 中要附加的 model 上下文。

        返回 ``{preset_id, model_name, thread_id}``：

        - ``preset_id``：task 显式声明（空串表示走默认）
        - ``model_name``：preset 命中 → ``preset.model``；否则 fallback
          ``cfg.model.name``；连 base_config 都缺失 → ``""``

        所有 cron run 相关 audit（run_started / run_finished / run_failed /
        run_silent_suppressed / run_inactivity_timeout / run_skipped_by_concurrency
        / run_approval_auto_allow）的 payload 都附加这些字段，
        让 audits.jsonl 自描述"这条 run 用了什么模型"。
        """
        preset_id = task.preset_id or ""
        model_name = ""
        if preset_id and self._preset_map and preset_id in self._preset_map:
            model_name = self._preset_map[preset_id].model
        elif self._base_config is not None:
            model_name = self._base_config.model.name
        return {
            "preset_id": preset_id,
            "model_name": model_name,
            "thread_id": task.thread_id,
        }

    # ------------------------------------------------------------------
    # v0.5 approval wrapper 装配（per-task mode + audit sink 聚合）
    # ------------------------------------------------------------------

    def _build_approval_wrapper(self, task: ScheduledTask) -> ScheduleApprovalProvider:
        """解析 effective approval mode + 装配 wrapper（v0.5 新增辅助方法）。

        优先级（高 → 低）：

        1. ``task.policy.approval_mode``（task 显式声明）
        2. ``self._base_config.scheduler.approval.mode``（全局配置）
        3. ``ApprovalMode.TRUST``（无 base_config 兜底——v0.5 调整：cron 即用户预批准任务）

        把 bridge 持有的 ``event_sinks`` 聚合成单一
        :class:`_AggregateEventSink` 注入 wrapper；空列表则传 ``None`` 保持
        "无 sink 不 emit" 行为不变。

        抽成方法的动机：``_drive_runner`` 太长难以直接单测；本方法纯装配，
        可直接 instantiate ExecutionBridge 后调用断言 mode / sink 解析结果。
        """
        global_mode_value = (
            self._base_config.scheduler.approval.mode if self._base_config is not None else "trust"
        )
        global_mode = ApprovalMode(global_mode_value)
        effective_mode = resolve_effective_mode(task.policy.approval_mode, global_mode)

        # v0.5 阶段 D：audit writer 永远在链路里——trust 自动放行 emit event 时
        # 落一行 ``run_approval_auto_allow`` audit。fail_closed 模式下 wrapper
        # 不会 emit，writer 不被触发，保持向后兼容（不写多余 audit）。
        audit_ctx = self._resolve_run_audit_context(task)
        audit_writer = _CronAuditWriterSink(
            store=self._store,
            task_id=task.task_id,
            preset_id=audit_ctx["preset_id"],
            model_name=audit_ctx["model_name"],
        )
        sink_chain: list[EventSink] = [audit_writer]
        sink_chain.extend(self._event_sinks)
        audit_sink = _AggregateEventSink(tuple(sink_chain))

        return ScheduleApprovalProvider(
            inner=self._inner_approval,
            task_id=task.task_id,
            mode=effective_mode,
            policy=(
                self._base_config.scheduler.approval
                if self._base_config is not None
                else SchedulerApprovalConfig()
            ),
            event_sink=audit_sink,
        )

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------

    async def execute(self, reservation: DueTaskReservation) -> ScheduledRun:
        """跑一次 due task；返回最终落盘的 :class:`ScheduledRun`。

        前置：先应用 ``concurrency_policy`` 决定本次行为。
        - ``skip`` (forbid + 已有 RUNNING)：合成一条 CANCELLED 行 + 写 audit，
          直接返回，不启动 Runner
        - ``replace``：:func:`apply_concurrency_policy` 已经把旧 RUNNING 行收尾
          为 CANCELLED，本函数继续走正常 RUNNING 路径启动新 Runner
        - ``proceed``：正常路径
        """
        task = reservation.task
        scheduled_for = reservation.scheduled_for

        # 1) concurrency_policy 前置应用
        decision = apply_concurrency_policy(task=task, store=self._store)
        if decision.action == "skip":
            return self._record_skipped_by_concurrency(
                task=task,
                scheduled_for=scheduled_for,
                reason=decision.reason or "skipped by concurrency_policy",
            )

        session_id = task.thread_id or self._fresh_session_id(task.task_id)
        run_id = self._fresh_run_id(task.task_id)
        started_at = to_iso(utc_now())

        # 装配 fresh request（domain 校验类型，不在 bridge 里重复）
        execution_context = TaskExecutionContext(
            task_id=task.task_id,
            scheduled_for=scheduled_for,
            trigger_type=task.trigger.trigger_type,
            origin=task.origin,
            is_scheduled_run=True,
            thread_id=task.thread_id,
        )
        request = ScheduledRunRequest(
            user_input=task.target.input_text,
            agent_name=task.target.agent_name,
            session_id=session_id,
            run_id=run_id,
            max_turns_override=task.policy.max_turns,
            execution_context=execution_context,
        )

        # 写 RUNNING + audit run_started
        running_record = ScheduledRun(
            run_id=run_id,
            task_id=task.task_id,
            status=RunStatus.RUNNING,
            scheduled_for=scheduled_for,
            started_at=started_at,
            finished_at=None,
            session_id=session_id,
            result_status=None,
            final_message_excerpt=None,
            error_message=None,
            failure_reason=None,
            delivery_error=None,
            silent_suppressed=False,
        )
        self._store.append_run(running_record)
        run_audit_ctx = self._resolve_run_audit_context(task)
        self._store.append_audit(
            action="run_started",
            task_id=task.task_id,
            actor="scheduler",
            payload={
                "run_id": run_id,
                "session_id": session_id,
                "scheduled_for": scheduled_for,
                **run_audit_ctx,
            },
        )

        # v0.4 per-task provider：根据 task.preset_id 构建独立 provider
        # v0.5.3：_build_provider 错配（preset_id 不在 preset_map）会抛
        # ValueError；这里转 FAILED run + supersede RUNNING + 写收尾 audit，
        # 不向上抛（符合模块约定 "所有错误进 ScheduledRun.error_message"）。
        try:
            provider = self._build_provider(task.preset_id)
        except ValueError as exc:
            logger.error(
                "execute: task %s preset misconfigured: %s",
                task.task_id,
                exc,
            )
            failed_run = self._build_exception_record(
                running_record=running_record,
                exc=exc,
            )
            self._store.supersede_and_append_run(failed_run)
            self._emit_finishing_audit(task=task, run=failed_run)
            return failed_run

        # v0.5.3 修复：preset 命中时 agent_spec.default_model 必须跟 preset.model
        # 走，否则 LLMRequest.model 仍是装配期 cfg.model.name（env 默认），导致
        # 实际请求 = preset.base_url + 默认 model name → 后端 400/不存在。
        # provider 只覆盖 base_url + api_key + 内部 model_config.name；
        # request.model 字段读 agent_spec.default_model，得在此处一并覆盖。
        effective_agent_spec = self._resolve_effective_agent_spec(task.preset_id)

        is_per_task_provider = provider is not self._llm

        try:
            # 真正跑 runner（含 watchdog + tool 裁剪 + approval wrap）
            final_run = await self._drive_runner(
                request=request,
                task=task,
                running_record=running_record,
                llm=provider,
                agent_spec=effective_agent_spec,
            )

            # v0.3 cron-delivery M3：投递阶段（在 supersede 之前合并 delivery 字段）
            if self._dispatcher is not None:
                final_run = await self._apply_delivery(task=task, run=final_run)
        finally:
            if is_per_task_provider and hasattr(provider, "aclose"):
                with contextlib.suppress(Exception):
                    await provider.aclose()

        # 状态机变更：用 supersede 写最终行
        self._store.supersede_and_append_run(final_run)

        # v0.2 P0 修复（保险）：写回 task.last_run_at。
        # ONCE 已经在 reserve_due_tasks 阶段归档（last_run_at 提前设置），
        # 这里再补一次拿"实际完成时间"覆盖原值；recurring 之前完全没回写
        # last_run_at，这里第一次有机会落盘。
        # 不更新 state / enabled，避免 reserve 阶段已归档 ONCE 与本步冲突。
        if final_run.finished_at is not None:
            from scheduler.store import TaskNotFoundError as _TaskNotFound

            with contextlib.suppress(_TaskNotFound):
                self._store.update_task(
                    task.task_id,
                    last_run_at=final_run.finished_at,
                )

        # 写收尾 audit
        self._emit_finishing_audit(task=task, run=final_run)
        return final_run

    @staticmethod
    def _augment_wrapper_with_run_trace(
        wrapper: ScheduleApprovalProvider,
        trace_sink: EventSink | None,
    ) -> ScheduleApprovalProvider:
        """把 per-run trace sink 追加到 wrapper.event_sink（v0.5 补 trace gap）。

        ``_build_approval_wrapper`` 装配时只能拿到 bridge 构造期传入的
        ``event_sinks``，不知道 ``_drive_runner`` 后续动态创建的 per-run
        trace sink。本方法在 ``_drive_runner`` 装配 trace_sink 之后调用，
        把它追加进 wrapper.event_sink 的 ``_AggregateEventSink.sinks``，
        让 trust 自动放行的 ``approval.cron.auto_allow`` event 同时写到
        cron audits.jsonl + per-run trace jsonl（DoD#5 双份证据）。

        ``trace_sink=None`` 或 wrapper.event_sink 类型不匹配时返回原 wrapper。
        """
        if trace_sink is None or not isinstance(wrapper.event_sink, _AggregateEventSink):
            return wrapper
        return replace(
            wrapper,
            event_sink=_AggregateEventSink((*wrapper.event_sink.sinks, trace_sink)),
        )

    # ------------------------------------------------------------------
    # 核心：装配 + Runner.run + watchdog + 收尾
    # ------------------------------------------------------------------

    async def _drive_runner(
        self,
        *,
        request: ScheduledRunRequest,
        task: ScheduledTask,
        running_record: ScheduledRun,
        llm: LLMProvider,
        agent_spec: AgentSpec | None = None,
    ) -> ScheduledRun:
        """装配 fresh runner 调用 + watchdog；返回最终 ``ScheduledRun``。

        v0.5.3：``agent_spec`` 参数允许 :meth:`execute` 在 preset 命中时按
        ``preset.model`` / ``preset.reasoning_effort`` 替换默认 spec，让
        :class:`core.contracts.LLMRequest` 里的 ``model`` 字段跟实际 provider
        对齐。未传时回退到 ``self._agent_spec``（装配期 cfg.model.name）。
        """
        resolved_agent_spec = agent_spec if agent_spec is not None else self._agent_spec
        # 1) 工具裁剪
        allowed_tool_names = frozenset(
            name for name in self._enabled_tool_names if not _is_disallowed_tool_name(name)
        )
        filtered_tools_lookup = _FilteredToolLookup(parent=self._tools, allowed=allowed_tool_names)
        enabled_tools: list[Tool] = []
        for name in self._enabled_tool_names:
            if _is_disallowed_tool_name(name):
                continue
            if name in self._tools:
                enabled_tools.append(self._tools[name])

        # 2) approval wrap（v0.5：抽到 _build_approval_wrapper 解析 effective mode + 聚合 sink）
        wrapped_approval = self._build_approval_wrapper(task)

        # 3) watchdog
        timeout_seconds = task.policy.inactivity_timeout_seconds
        if timeout_seconds is None:
            timeout_seconds = DEFAULT_INACTIVITY_TIMEOUT
        watchdog = InactivityWatchdog(
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=self._watchdog_poll_interval_seconds,
        )
        watchdog.reset()

        # 4) 把 watchdog 注入 runner 的 sink fan-out
        #    runner._event_sinks 是 list；挂入 + finally 移除。
        sinks_list: list[EventSink] = self._runner._event_sinks
        sinks_list.append(watchdog)

        # 4b) v0.4 per-run trace sink：每次 cron run 独立 jsonl 记录
        trace_sink: JsonlTraceSink | None = None
        if self._trace_dir is not None:
            from infrastructure.tracing import JsonlTraceSink

            trace_path = self._trace_dir / f"cron-{request.run_id}.jsonl"
            trace_sink = JsonlTraceSink(trace_path, auto_flush=True)
            sinks_list.append(trace_sink)

        # 4c) v0.5 修复：把 per-run trace sink 也注入 wrapped_approval.event_sink
        wrapped_approval = self._augment_wrapper_with_run_trace(wrapped_approval, trace_sink)

        # 5) fresh session
        session = self._session_factory(request.session_id)

        # 6) max_turns 解析（v0.5.1：task.policy.max_turns > cfg.scheduler.default_max_turns > 兜底）
        if task.policy.max_turns is not None:
            max_turns = task.policy.max_turns
        elif self._base_config is not None:
            max_turns = self._base_config.scheduler.default_max_turns
        else:
            max_turns = _FALLBACK_MAX_TURNS_NO_CONFIG

        runner_task: asyncio.Task[Result] | None = None
        watch_task: asyncio.Task[None] | None = None
        try:
            runner_task = asyncio.create_task(
                self._runner.run(
                    request.user_input,
                    session=session,
                    agent_spec=resolved_agent_spec,
                    llm=llm,
                    tools=filtered_tools_lookup,
                    approval=wrapped_approval,
                    max_turns=max_turns,
                    run_id=request.run_id,
                    enabled_tools=enabled_tools,
                )
            )
            watch_task = asyncio.create_task(watchdog.watch(runner_task))
            try:
                result = await runner_task
            except asyncio.CancelledError:
                # 仅 watchdog 命中时会触发；任何其他 cancel 都会被这里吞为
                # inactivity_timeout（v0.1 不区分外部取消）。
                return self._build_inactivity_record(
                    running_record=running_record,
                    triggered_by_watchdog=watchdog.triggered,
                )
            return self._classify_result(result, running_record=running_record)
        except Exception as exc:  # 防御式：runner 自己的异常已包成 Result
            return self._build_exception_record(
                running_record=running_record,
                exc=exc,
            )
        finally:
            # 先停 watch（它若仍 sleep 会就此退出），再清理 sink 引用
            if watch_task is not None and not watch_task.done():
                watch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watch_task
            # 移除 watchdog（保证不会污染下次 execute）
            with contextlib.suppress(ValueError):
                sinks_list.remove(watchdog)
            # 移除 per-run trace sink 并关闭
            if trace_sink is not None:
                with contextlib.suppress(ValueError):
                    sinks_list.remove(trace_sink)
                await trace_sink.close()

    # ------------------------------------------------------------------
    # 结果分类
    # ------------------------------------------------------------------

    def _classify_result(
        self,
        result: Result,
        *,
        running_record: ScheduledRun,
    ) -> ScheduledRun:
        """根据 :class:`core.result.Result` 把状态映射到 :class:`ScheduledRun`。"""
        finished_at = to_iso(utc_now())
        final_text = _final_message_text(result.final_message)

        # 失败：runner 已捕获异常并产出 failed Result
        if result.status == "failed":
            failure_reason = self._derive_failure_reason(result)
            return ScheduledRun(
                run_id=running_record.run_id,
                task_id=running_record.task_id,
                status=RunStatus.FAILED,
                scheduled_for=running_record.scheduled_for,
                started_at=running_record.started_at,
                finished_at=finished_at,
                session_id=running_record.session_id,
                result_status=result.status,
                final_message_excerpt=_truncate(final_text),
                error_message=_extract_error_message(result),
                failure_reason=failure_reason,
                delivery_error=None,
                silent_suppressed=False,
            )

        # cancelled：v0.1 未明确单独建模；当作 failed/runner_exception 兜底
        if result.status == "cancelled":
            return ScheduledRun(
                run_id=running_record.run_id,
                task_id=running_record.task_id,
                status=RunStatus.FAILED,
                scheduled_for=running_record.scheduled_for,
                started_at=running_record.started_at,
                finished_at=finished_at,
                session_id=running_record.session_id,
                result_status=result.status,
                final_message_excerpt=_truncate(final_text),
                error_message="run cancelled",
                failure_reason=RunFailureReason.RUNNER_EXCEPTION,
                delivery_error=None,
                silent_suppressed=False,
            )

        # completed：可能命中 [SILENT]
        if final_text is not None and is_silent(final_text):
            stripped = strip_silent_prefix(final_text)
            excerpt = _truncate(stripped) or "[SILENT]"
            return ScheduledRun(
                run_id=running_record.run_id,
                task_id=running_record.task_id,
                status=RunStatus.SILENT,
                scheduled_for=running_record.scheduled_for,
                started_at=running_record.started_at,
                finished_at=finished_at,
                session_id=running_record.session_id,
                result_status=result.status,
                final_message_excerpt=excerpt,
                error_message=None,
                failure_reason=None,
                delivery_error=None,
                silent_suppressed=True,
            )

        return ScheduledRun(
            run_id=running_record.run_id,
            task_id=running_record.task_id,
            status=RunStatus.COMPLETED,
            scheduled_for=running_record.scheduled_for,
            started_at=running_record.started_at,
            finished_at=finished_at,
            session_id=running_record.session_id,
            result_status=result.status,
            final_message_excerpt=_truncate(final_text or ""),
            error_message=None,
            failure_reason=None,
            delivery_error=None,
            silent_suppressed=False,
        )

    @staticmethod
    def _derive_failure_reason(result: Result) -> RunFailureReason:
        """从 ``result.error.message`` 启发式推断 :class:`RunFailureReason`。"""
        message = ""
        if result.error is not None:
            message = result.error.message or ""
        lower = message.lower()
        if "needs_approval" in lower or "approval" in lower:
            return RunFailureReason.NEEDS_APPROVAL
        if "tool" in lower and "error" in lower:
            return RunFailureReason.TOOL_ERROR
        return RunFailureReason.RUNNER_EXCEPTION

    def _build_inactivity_record(
        self,
        *,
        running_record: ScheduledRun,
        triggered_by_watchdog: bool,
    ) -> ScheduledRun:
        """watchdog 命中或外部 CancelledError → INACTIVITY_TIMEOUT。"""
        finished_at = to_iso(utc_now())
        msg = (
            "inactivity timeout"
            if triggered_by_watchdog
            else "run cancelled (treated as inactivity timeout)"
        )
        return ScheduledRun(
            run_id=running_record.run_id,
            task_id=running_record.task_id,
            status=RunStatus.INACTIVITY_TIMEOUT,
            scheduled_for=running_record.scheduled_for,
            started_at=running_record.started_at,
            finished_at=finished_at,
            session_id=running_record.session_id,
            result_status=None,
            final_message_excerpt=None,
            error_message=msg,
            failure_reason=RunFailureReason.INACTIVITY_TIMEOUT,
            delivery_error=None,
            silent_suppressed=False,
        )

    def _build_exception_record(
        self,
        *,
        running_record: ScheduledRun,
        exc: BaseException,
    ) -> ScheduledRun:
        """runner 自己抛裸异常（实际不应发生）→ FAILED + RUNNER_EXCEPTION。"""
        finished_at = to_iso(utc_now())
        return ScheduledRun(
            run_id=running_record.run_id,
            task_id=running_record.task_id,
            status=RunStatus.FAILED,
            scheduled_for=running_record.scheduled_for,
            started_at=running_record.started_at,
            finished_at=finished_at,
            session_id=running_record.session_id,
            result_status="failed",
            final_message_excerpt=None,
            error_message=f"{type(exc).__name__}: {exc}",
            failure_reason=RunFailureReason.RUNNER_EXCEPTION,
            delivery_error=None,
            silent_suppressed=False,
        )

    # ------------------------------------------------------------------
    # v0.3 cron-delivery：投递阶段
    # ------------------------------------------------------------------

    async def _apply_delivery(self, *, task: ScheduledTask, run: ScheduledRun) -> ScheduledRun:
        """在 supersede 落盘前，调 ``DeliveryDispatcher.deliver`` 把投递结果
        合并进 :class:`ScheduledRun`。

        投递语义见 :class:`scheduler.delivery.DeliveryDispatcher`：

        - ``task.delivery is None`` / ``silent_marker`` 命中 / 无 sink → SKIPPED
        - sink 抛异常 → FAILED + ``delivery_error``
        - sink 成功 → DELIVERED + ``delivered_at``

        **传给 dispatcher 的 final_message 是** ``run.final_message_excerpt``：
        已被 :meth:`_classify_result` 处理（SILENT 前缀剥离 / 截断到
        ``_FINAL_MESSAGE_EXCERPT_LIMIT``）。dispatcher 不应假设 final_message
        包含原始 [SILENT] 前缀；silent 状态由 ``run.status`` 直接表达。

        本函数本身**不抛异常**：dispatcher 内部已捕获 sink 异常；外层 catch
        是给"dispatcher 自身崩 / 半坏"的极少数情况兜底，记录 traceback 帮
        定位装配 bug。``BaseException``（如 ``CancelledError``）继续传播。
        """
        import traceback

        from scheduler.delivery import DeliveryResult

        assert self._dispatcher is not None  # caller 已判
        final_message = run.final_message_excerpt or ""
        delivery_task = task
        if task.thread_id and task.thread_id == run.session_id and task.delivery is not None:
            delivery_task = replace(
                task,
                delivery=replace(task.delivery, target=None),
            )
        try:
            result = await self._dispatcher.deliver(delivery_task, run, final_message)
        except Exception as exc:
            # 极少见：dispatcher 自身崩（半坏对象 / mock 漏方法 / 内部装配 bug）
            # 写 traceback 后两帧（避免 RunRecord 单字段过大），帮定位
            tb_tail = "".join(traceback.format_exception(exc)[-2:]).strip()
            result = DeliveryResult.failed(f"dispatcher_{type(exc).__name__}: {exc}\n{tb_tail}")

        return replace(
            run,
            delivered_at=result.delivered_at,
            delivery_status=result.status,
            delivery_error=result.error_message,
        )

    # ------------------------------------------------------------------
    # audit 收尾
    # ------------------------------------------------------------------

    def _emit_finishing_audit(self, *, task: ScheduledTask, run: ScheduledRun) -> None:
        """按最终 :class:`RunStatus` 写收尾 audit（v0.5.2: 加 preset_id + model_name）。"""
        task_id = task.task_id
        audit_ctx = self._resolve_run_audit_context(task)
        common = {
            "run_id": run.run_id,
            "session_id": run.session_id,
            "result_status": run.result_status,
            **audit_ctx,
        }
        if run.status is RunStatus.COMPLETED:
            self._store.append_audit(
                action="run_finished",
                task_id=task_id,
                actor="scheduler",
                payload={**common, "silent_suppressed": False},
            )
            return
        if run.status is RunStatus.SILENT:
            self._store.append_audit(
                action="run_silent_suppressed",
                task_id=task_id,
                actor="scheduler",
                payload={**common, "silent_suppressed": True},
            )
            self._store.append_audit(
                action="run_finished",
                task_id=task_id,
                actor="scheduler",
                payload={**common, "silent_suppressed": True},
            )
            return
        if run.status is RunStatus.INACTIVITY_TIMEOUT:
            self._store.append_audit(
                action="run_inactivity_timeout",
                task_id=task_id,
                actor="scheduler",
                payload={
                    **common,
                    "failure_reason": (run.failure_reason.value if run.failure_reason else None),
                    "error_message": run.error_message,
                },
            )
            return
        # FAILED / CANCELLED / ABANDONED 等
        action = (
            "run_needs_approval"
            if run.failure_reason is RunFailureReason.NEEDS_APPROVAL
            else "run_failed"
        )
        self._store.append_audit(
            action=action,
            task_id=task_id,
            actor="scheduler",
            payload={
                **common,
                "failure_reason": (run.failure_reason.value if run.failure_reason else None),
                "error_message": run.error_message,
            },
        )

    # ------------------------------------------------------------------
    # concurrency=skip：合成 CANCELLED 行 + 写 audit
    # ------------------------------------------------------------------

    def _record_skipped_by_concurrency(
        self,
        *,
        task: ScheduledTask,
        scheduled_for: str,
        reason: str,
    ) -> ScheduledRun:
        """forbid 策略命中已有 RUNNING：不启动 Runner，落盘合成 CANCELLED 行。

        - run_id 是 fresh 的（区别于被保护的旧 RUNNING run_id）
        - 不调 :meth:`Store.supersede_and_append_run`，因为没有前置 RUNNING 行
        - 写 audit ``run_skipped_by_concurrency``，方便事后排查
        """
        finished_at = to_iso(utc_now())
        synthesized = ScheduledRun(
            run_id=self._fresh_run_id(task.task_id),
            task_id=task.task_id,
            status=RunStatus.CANCELLED,
            scheduled_for=scheduled_for,
            started_at=None,
            finished_at=finished_at,
            session_id=None,
            result_status=None,
            final_message_excerpt=None,
            error_message=reason,
            failure_reason=None,
            delivery_error=None,
            silent_suppressed=False,
        )
        self._store.append_run(synthesized)
        audit_ctx = self._resolve_run_audit_context(task)
        self._store.append_audit(
            action="run_skipped_by_concurrency",
            task_id=task.task_id,
            actor="scheduler",
            payload={
                "run_id": synthesized.run_id,
                "scheduled_for": scheduled_for,
                "reason": reason,
                **audit_ctx,
            },
        )
        return synthesized

    # ------------------------------------------------------------------
    # id helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fresh_session_id(task_id: str) -> str:
        return f"sched-{task_id}-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _fresh_run_id(task_id: str) -> str:
        return f"run-sched-{task_id}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# 内部 helpers
# ---------------------------------------------------------------------------


def _final_message_text(message: Message | None) -> str | None:
    """从 :class:`Message` 抽 ``content`` 文本；``None`` 安全。"""
    if message is None:
        return None
    return message.content


def _truncate(text: str | None) -> str | None:
    """把 final message 截断到 :data:`_FINAL_MESSAGE_EXCERPT_LIMIT`。"""
    if text is None:
        return None
    if len(text) <= _FINAL_MESSAGE_EXCERPT_LIMIT:
        return text
    return text[:_FINAL_MESSAGE_EXCERPT_LIMIT]


def _extract_error_message(result: Result) -> str | None:
    """读 ``result.error.message``；无 error 时拿 ``result.metadata`` 兜底。"""
    if result.error is not None:
        return result.error.message or type(result.error).__name__
    return None


__all__ = [
    "ExecutionBridge",
    "InactivityWatchdog",
    "_AggregateEventSink",
    "_CronAuditWriterSink",
]
