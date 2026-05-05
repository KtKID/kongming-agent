"""scheduler v0.1 — Execution Bridge（Wave C，#11/#12/#13/#15）。

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
- 不直接 import ``safety.*``（沿用 scheduler 模块边界）
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

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
from scheduler.delivery import DeliveryDispatcher
from scheduler.domain import (
    DEFAULT_INACTIVITY_TIMEOUT,
    DueTaskReservation,
    RunFailureReason,
    RunStatus,
    ScheduledRun,
    ScheduledRunRequest,
    ScheduledTask,
    TaskExecutionContext,
)
from scheduler.policy import apply_concurrency_policy
from scheduler.safety_wrapper import ScheduleApprovalProvider
from scheduler.silent import is_silent, strip_silent_prefix
from scheduler.store import Store
from scheduler.timing import to_iso, utc_now

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------


_DEFAULT_MAX_TURNS = 20
"""task.policy.max_turns 缺省时的兜底；与 :class:`AgentSpec` 默认对齐口径。"""

_DEFAULT_POLL_INTERVAL = 5.0
"""watchdog 默认轮询间隔（秒）。文档 §3 约定 5s。"""

_FINAL_MESSAGE_EXCERPT_LIMIT = 512
"""``ScheduledRun.final_message_excerpt`` 最大字符数。"""

_DISALLOWED_TOOL_PREFIXES: tuple[str, ...] = ("schedule.", "cron.")
"""cron run 装配期裁掉的工具名前缀（防递归创建任务）。"""

_DISALLOWED_TOOL_NAMES: frozenset[str] = frozenset({"schedule", "cron"})
"""cron run 装配期裁掉的单字工具名（防递归创建任务）。

v0.2 新增 :class:`tools.schedule_tool.ScheduleTool`，其 ``name`` 是单字
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

        session_id = self._fresh_session_id(task.task_id)
        run_id = self._fresh_run_id(task.task_id)
        started_at = to_iso(utc_now())

        # 装配 fresh request（domain 校验类型，不在 bridge 里重复）
        execution_context = TaskExecutionContext(
            task_id=task.task_id,
            scheduled_for=scheduled_for,
            trigger_type=task.trigger.trigger_type,
            origin=task.origin,
            is_scheduled_run=True,
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
        self._store.append_audit(
            action="run_started",
            task_id=task.task_id,
            actor="scheduler",
            payload={
                "run_id": run_id,
                "session_id": session_id,
                "scheduled_for": scheduled_for,
            },
        )

        # 真正跑 runner（含 watchdog + tool 裁剪 + approval wrap）
        final_run = await self._drive_runner(
            request=request,
            task=task,
            running_record=running_record,
        )

        # v0.3 cron-delivery M3：投递阶段（在 supersede 之前合并 delivery 字段）
        if self._dispatcher is not None:
            final_run = await self._apply_delivery(task=task, run=final_run)

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
        self._emit_finishing_audit(task_id=task.task_id, run=final_run)
        return final_run

    # ------------------------------------------------------------------
    # 核心：装配 + Runner.run + watchdog + 收尾
    # ------------------------------------------------------------------

    async def _drive_runner(
        self,
        *,
        request: ScheduledRunRequest,
        task: ScheduledTask,
        running_record: ScheduledRun,
    ) -> ScheduledRun:
        """装配 fresh runner 调用 + watchdog；返回最终 ``ScheduledRun``。"""
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

        # 2) approval wrap
        wrapped_approval = ScheduleApprovalProvider(
            inner=self._inner_approval, task_id=task.task_id
        )

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

        # 5) fresh session
        session = self._session_factory(request.session_id)

        # 6) max_turns
        if task.policy.max_turns is not None:
            max_turns = task.policy.max_turns
        else:
            max_turns = _DEFAULT_MAX_TURNS

        runner_task: asyncio.Task[Result] | None = None
        watch_task: asyncio.Task[None] | None = None
        try:
            runner_task = asyncio.create_task(
                self._runner.run(
                    request.user_input,
                    session=session,
                    agent_spec=self._agent_spec,
                    llm=self._llm,
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
        try:
            result = await self._dispatcher.deliver(task, run, final_message)
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

    def _emit_finishing_audit(self, *, task_id: str, run: ScheduledRun) -> None:
        """按最终 :class:`RunStatus` 写收尾 audit。"""
        common = {
            "run_id": run.run_id,
            "session_id": run.session_id,
            "result_status": run.result_status,
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
        self._store.append_audit(
            action="run_skipped_by_concurrency",
            task_id=task.task_id,
            actor="scheduler",
            payload={
                "run_id": synthesized.run_id,
                "scheduled_for": scheduled_for,
                "reason": reason,
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
]
