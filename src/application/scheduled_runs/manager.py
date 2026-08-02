"""定时任务 live run 门户。

功能：把 due reservation 统一提交到 HostDispatcher → AgentManager →
TaskRegistry 启动链，集中持有 reservation 幂等、并发准入、真实取消句柄与关闭顺序。
Ticker 只调用 ``submit_scheduled_run``，不创建或持有 asyncio Task。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any, Literal, Protocol

from application.agents.loop import MailRunBridge
from application.agents.registry import TaskProjection, TaskRegistrationContext
from core.errors import AgentError
from core.mail import Mail
from core.message import Message
from core.result import Result
from scheduler.delivery import RunLifecycleSink
from scheduler.domain import (
    ConcurrencyPolicy,
    DueTaskReservation,
    RunStatus,
    ScheduledRun,
    ScheduledRunSubmitDisposition,
    ScheduledRunSubmitReceipt,
    ScheduledTask,
)

logger = logging.getLogger(__name__)


class ScheduledRunCellState(StrEnum):
    """Manager 内单次 run 的 live 生命周期。"""

    PENDING = "pending"
    RUNNING = "running"
    FINISHING = "finishing"
    FINISHED = "finished"


class ScheduledRunExecution(Protocol):
    """ScheduledRunManager 消费的执行支持合同。"""

    async def execute_admitted(
        self,
        reservation: DueTaskReservation,
        *,
        run_id: str,
        session_id: str,
        cancel_reason_getter: Callable[[], str | None] | None = None,
        event_context: dict[str, Any] | None = None,
        agent_id: str = "",
        on_started: (Callable[[ScheduledTask, ScheduledRun], Awaitable[None]] | None) = None,
    ) -> ScheduledRun:
        """执行已通过 Manager 准入的 run，并返回 durable terminal 结果。"""
        ...

    def record_skipped_submission(
        self,
        reservation: DueTaskReservation,
        *,
        run_id: str,
        session_id: str,
        reason: str,
        cancel_reason: str,
    ) -> ScheduledRun:
        """记录尚未启动 Runner 的终态结果。"""
        ...

    def cancel_admitted_run(
        self,
        reservation: DueTaskReservation,
        *,
        run_id: str,
        reason: str,
    ) -> ScheduledRun | None:
        """抢占 durable terminal，并返回当前 winner。"""
        ...


class ScheduledRunDispatcher(Protocol):
    """ScheduledRunManager 消费的普通 thread 启动门户合同。"""

    async def run_scheduled_text(
        self,
        user_input: str,
        *,
        metadata: dict[str, object],
    ) -> Result:
        """通过 HostDispatcher 的 root mailbox 运行一次定时输入。"""
        ...

    def list_task_records(
        self,
        *,
        include_finished: bool = False,
    ) -> tuple[TaskProjection, ...]:
        """读取当前 root thread 的 TaskRegistry 投影。"""
        ...

    async def interrupt(self) -> None:
        """中断当前 root agent tree。"""
        ...

    async def aclose(self) -> None:
        """等待运行收口并释放 dispatcher。"""
        ...


class ScheduledRunDispatcherFactory(Protocol):
    """按单次 scheduled run 构造普通 thread dispatcher 的工厂合同。"""

    def __call__(
        self,
        *,
        session_id: str,
        thread_id: str,
        root_run_bridge: MailRunBridge,
        task_registration_context: TaskRegistrationContext,
    ) -> ScheduledRunDispatcher:
        """构造绑定 stable thread 与 fresh session 的 dispatcher。"""
        ...


@dataclass
class _ScheduledRunCell:
    """Manager 内部 live cell；业务身份稳定，运行句柄按阶段补齐。"""

    reservation: DueTaskReservation
    receipt: ScheduledRunSubmitReceipt
    fingerprint: str
    result_future: asyncio.Future[ScheduledRun]
    submission_sequence: int
    state: ScheduledRunCellState = ScheduledRunCellState.PENDING
    owner_task: asyncio.Task[None] | None = None
    dispatcher: ScheduledRunDispatcher | None = None
    requested_cancel_reason: str | None = None
    final_run: ScheduledRun | None = None


@dataclass
class _TaskAdmissionCell:
    """按 task_id 共享的 admission lock 与当前协程引用计数。"""

    lock: asyncio.Lock
    users: int = 0


class ScheduledRunManager:
    """定时任务 live owner 与唯一提交门户。"""

    def __init__(
        self,
        *,
        bridge: ScheduledRunExecution,
        dispatcher_factory: ScheduledRunDispatcherFactory,
        max_inflight: int,
        max_retained_runs: int = 1000,
        shutdown_timeout_seconds: float = 30.0,
        lifecycle_sink: RunLifecycleSink | None = None,
    ) -> None:
        """初始化空 live registry 和 Manager 级并发闸门。"""
        if max_inflight < 1:
            raise ValueError("max_inflight must be positive")
        if max_retained_runs < 1:
            raise ValueError("max_retained_runs must be positive")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        self._bridge = bridge
        self._dispatcher_factory = dispatcher_factory
        self._semaphore = asyncio.Semaphore(max_inflight)
        self._max_retained_runs = max_retained_runs
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._lifecycle_sink = lifecycle_sink
        self._submit_lock = asyncio.Lock()
        self._task_locks: dict[str, _TaskAdmissionCell] = {}
        self._cells_by_reservation: dict[str, _ScheduledRunCell] = {}
        self._cells_by_run: dict[str, _ScheduledRunCell] = {}
        self._run_ids_by_task: dict[str, set[str]] = {}
        self._closed = False
        self._next_submission_sequence = 0

    async def submit_scheduled_run(
        self,
        reservation: DueTaskReservation,
    ) -> ScheduledRunSubmitReceipt:
        """提交 reservation 并立即返回稳定 ID；live 生命周期由 Manager 收编。"""
        fingerprint = _reservation_fingerprint(reservation)
        async with self._submit_lock:
            if self._closed:
                raise RuntimeError("ScheduledRunManager is closed")
            existing = self._cells_by_reservation.get(reservation.reservation_id)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise ValueError(
                        "reservation_id collision with different payload: "
                        f"{reservation.reservation_id}"
                    )
                return replace(
                    existing.receipt,
                    disposition=ScheduledRunSubmitDisposition.DUPLICATE,
                )

            effective_reservation = _with_effective_thread(reservation)
            run_id = f"run-sched-{reservation.task.task_id}-{uuid.uuid4().hex[:12]}"
            session_id = f"sched-{reservation.task.task_id}-{uuid.uuid4().hex[:12]}"
            receipt = ScheduledRunSubmitReceipt(
                reservation_id=reservation.reservation_id,
                task_id=reservation.task.task_id,
                run_id=run_id,
                session_id=session_id,
                thread_id=effective_reservation.task.thread_id,
                disposition=ScheduledRunSubmitDisposition.ACCEPTED,
            )
            loop = asyncio.get_running_loop()
            self._next_submission_sequence += 1
            cell = _ScheduledRunCell(
                reservation=effective_reservation,
                receipt=receipt,
                fingerprint=fingerprint,
                result_future=loop.create_future(),
                submission_sequence=self._next_submission_sequence,
            )
            self._cells_by_reservation[reservation.reservation_id] = cell
            self._cells_by_run[run_id] = cell
            self._run_ids_by_task.setdefault(receipt.task_id, set()).add(run_id)
            owner_task: asyncio.Task[None] = asyncio.create_task(
                self._run_cell(cell),
                name=f"scheduled-run-owner-{run_id}",
            )
            cell.owner_task = owner_task

            def _scheduled_run_done(
                done: asyncio.Task[None],
                *,
                current_cell: _ScheduledRunCell = cell,
            ) -> None:
                """读取 owner 异常；live 索引由协程 finally 在线性化点释放。"""
                if done.cancelled():
                    self._prune_finished_cells()
                    return
                exc = done.exception()
                if exc is not None:
                    logger.error(
                        "scheduled run owner failed run_id=%s: %r",
                        current_cell.receipt.run_id,
                        exc,
                    )
                    if not current_cell.result_future.done():
                        current_cell.result_future.set_exception(exc)
                self._prune_finished_cells()

            owner_task.add_done_callback(_scheduled_run_done)
            return receipt

    async def wait_for_run(self, run_id: str) -> ScheduledRun:
        """等待指定业务 run 的 durable 结果。"""
        cell = self._cells_by_run.get(run_id)
        if cell is None:
            raise KeyError(run_id)
        return await asyncio.shield(cell.result_future)

    async def cancel_run(self, run_id: str) -> ScheduledRun:
        """用户按业务 run ID 中断真实 TaskRegistry handle 并返回 durable 终态。"""
        cell = self._cells_by_run.get(run_id)
        if cell is None:
            raise KeyError(run_id)
        await self._cancel_cell(cell, reason="user_interrupt")
        return await self.wait_for_run(run_id)

    def live_run_ids(self, task_id: str) -> tuple[str, ...]:
        """返回指定 task 当前由 Manager 持有的 live run IDs。"""
        return tuple(sorted(self._run_ids_by_task.get(task_id, set())))

    def has_live_task(self, task_id: str) -> bool:
        """返回指定业务 task 是否存在 pending 或 running cell。"""
        return bool(self._run_ids_by_task.get(task_id))

    def list_run_task_records(
        self,
        run_id: str,
        *,
        include_finished: bool = False,
    ) -> tuple[TaskProjection, ...]:
        """读取指定 scheduled run 的真实 TaskRegistry 投影。"""
        cell = self._cells_by_run.get(run_id)
        if cell is None or cell.dispatcher is None:
            return ()
        return cell.dispatcher.list_task_records(include_finished=include_finished)

    async def aclose(self) -> None:
        """限时关闭门户，并向超时的 interrupt、owner 和 lifecycle task 发出取消。"""
        async with self._submit_lock:
            self._closed = True
            cells = [
                cell
                for cell in self._cells_by_run.values()
                if cell.state is not ScheduledRunCellState.FINISHED
            ]
            for cell in cells:
                if cell.requested_cancel_reason is None:
                    cell.requested_cancel_reason = "scheduler_shutdown"
            cancellable_cells = [
                cell
                for cell in cells
                if cell.state in (ScheduledRunCellState.PENDING, ScheduledRunCellState.RUNNING)
            ]
        deadline = asyncio.get_running_loop().time() + self._shutdown_timeout_seconds
        if cancellable_cells:
            cancellation_tasks: list[asyncio.Task[None]] = [
                asyncio.create_task(
                    self._cancel_cell(cell, reason="scheduler_shutdown"),
                    name=f"scheduled-run-shutdown-{cell.receipt.run_id}",
                )
                for cell in cancellable_cells
            ]
            await self._wait_until_deadline(
                cancellation_tasks,
                deadline=deadline,
                phase="interrupt",
            )
        owners: list[asyncio.Task[None]] = [
            cell.owner_task
            for cell in cells
            if cell.owner_task is not None and not cell.owner_task.done()
        ]
        if owners:
            await self._wait_until_deadline(
                owners,
                deadline=deadline,
                phase="owner",
            )

    async def _wait_until_deadline(
        self,
        tasks: Sequence[asyncio.Task[None]],
        *,
        deadline: float,
        phase: str,
    ) -> None:
        """在共享 shutdown deadline 内等待任务；到期后取消剩余任务并立即让出事件环。"""
        pending = {task for task in tasks if not task.done()}
        if not pending:
            return
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining > 0:
            _, pending = await asyncio.wait(pending, timeout=remaining)
        if not pending:
            return
        logger.warning(
            "scheduled run shutdown %s phase exceeded %.3fs; cancelling %d task(s)",
            phase,
            self._shutdown_timeout_seconds,
            len(pending),
        )
        for task in pending:
            task.cancel()
        await asyncio.sleep(0)

    async def _run_cell(self, cell: _ScheduledRunCell) -> None:
        """执行准入、Manager 限流和统一 HostDispatcher 启动链。"""
        task = cell.reservation.task
        admission = self._task_locks.setdefault(
            task.task_id,
            _TaskAdmissionCell(lock=asyncio.Lock()),
        )
        admission.users += 1
        try:
            async with admission.lock:
                active = [
                    other
                    for run_id in tuple(self._run_ids_by_task.get(task.task_id, set()))
                    if run_id != cell.receipt.run_id
                    if (other := self._cells_by_run.get(run_id)) is not None
                    and other.submission_sequence < cell.submission_sequence
                    and other.state is not ScheduledRunCellState.FINISHED
                ]
                if task.policy.concurrency_policy is ConcurrencyPolicy.FORBID and active:
                    active_run_ids = ",".join(sorted(other.receipt.run_id for other in active))
                    final = self._bridge.record_skipped_submission(
                        cell.reservation,
                        run_id=cell.receipt.run_id,
                        session_id=cell.receipt.session_id,
                        reason=f"existing scheduled run is live: {active_run_ids}",
                        cancel_reason="forbid_existing_run",
                    )
                    cell.final_run = final
                    return
                if task.policy.concurrency_policy is ConcurrencyPolicy.REPLACE:
                    for victim in active:
                        await self._cancel_cell(
                            victim,
                            reason="replaced_by_new_run",
                        )

            async with self._semaphore:
                cell.state = ScheduledRunCellState.RUNNING

                async def scheduled_mail_run_bridge(
                    mail_text: str,
                    *,
                    mail: Mail,
                ) -> Result:
                    """在 AgentManager 创建的真实 run Task 内驱动 cron execution plan。"""
                    del mail_text
                    if cell.requested_cancel_reason is not None:
                        final = self._bridge.record_skipped_submission(
                            cell.reservation,
                            run_id=cell.receipt.run_id,
                            session_id=cell.receipt.session_id,
                            reason="scheduled run cancelled before durable start",
                            cancel_reason=cell.requested_cancel_reason,
                        )
                        cell.final_run = final
                        return _scheduled_run_result(final)
                    try:
                        final = await self._bridge.execute_admitted(
                            cell.reservation,
                            run_id=cell.receipt.run_id,
                            session_id=cell.receipt.session_id,
                            cancel_reason_getter=lambda: cell.requested_cancel_reason,
                            event_context={
                                "run_epoch": mail.epoch,
                                "mail_kind": mail.kind,
                                "mail_task_id": mail.task_id,
                                "conversation_id": cell.receipt.session_id,
                            },
                            agent_id=mail.recipient_agent_id,
                            on_started=self._notify_run_started,
                        )
                    except asyncio.CancelledError:
                        raise
                    cell.final_run = final
                    return _scheduled_run_result(final)

                root_bridge: MailRunBridge = scheduled_mail_run_bridge
                dispatcher = self._dispatcher_factory(
                    session_id=cell.receipt.session_id,
                    thread_id=cell.receipt.thread_id,
                    root_run_bridge=root_bridge,
                    task_registration_context=TaskRegistrationContext(
                        thread_id=cell.receipt.thread_id,
                        source="scheduled",
                        workflow_id="scheduler",
                        workflow_task_id=cell.receipt.task_id,
                        task_run_id=cell.receipt.run_id,
                        task_name=task.name,
                        session_id=cell.receipt.session_id,
                    ),
                )
                cell.dispatcher = dispatcher
                try:
                    await dispatcher.run_scheduled_text(
                        task.target.input_text,
                        metadata={
                            "scheduled_run_id": cell.receipt.run_id,
                            "reservation_id": cell.receipt.reservation_id,
                        },
                    )
                finally:
                    try:
                        await dispatcher.aclose()
                    finally:
                        cell.dispatcher = None
        except asyncio.CancelledError:
            if cell.final_run is None:
                final = self._bridge.record_skipped_submission(
                    cell.reservation,
                    run_id=cell.receipt.run_id,
                    session_id=cell.receipt.session_id,
                    reason="scheduled run cancelled before execution",
                    cancel_reason=cell.requested_cancel_reason or "scheduler_shutdown",
                )
                cell.final_run = final
        finally:
            admission.users -= 1
            if admission.users == 0 and self._task_locks.get(task.task_id) is admission:
                self._task_locks.pop(task.task_id, None)
            await self._finish_cell(cell)

    async def _cancel_cell(
        self,
        cell: _ScheduledRunCell,
        *,
        reason: str,
    ) -> None:
        """按阶段取消 cell，并等待真实注册 Task 或 pending owner 收口。"""
        if cell.state in (
            ScheduledRunCellState.FINISHING,
            ScheduledRunCellState.FINISHED,
        ):
            return
        if cell.requested_cancel_reason is None:
            cell.requested_cancel_reason = reason
        effective_reason = cell.requested_cancel_reason
        owner = cell.owner_task
        if cell.dispatcher is not None:
            winner = self._bridge.cancel_admitted_run(
                cell.reservation,
                run_id=cell.receipt.run_id,
                reason=effective_reason,
            )
            if winner is not None:
                cell.final_run = winner
            await cell.dispatcher.interrupt()
        elif owner is not None and not owner.done():
            owner.cancel()
        if owner is not None and owner is not asyncio.current_task() and not owner.done():
            await asyncio.gather(owner, return_exceptions=True)
        if cell.final_run is None:
            final = self._bridge.record_skipped_submission(
                cell.reservation,
                run_id=cell.receipt.run_id,
                session_id=cell.receipt.session_id,
                reason="scheduled run owner cancelled before result publication",
                cancel_reason=effective_reason,
            )
            cell.final_run = final
        if not cell.result_future.done() and (owner is None or owner.done()):
            await self._finish_cell(cell)

    async def _notify_run_started(
        self,
        task: ScheduledTask,
        run: ScheduledRun,
    ) -> None:
        """在 RUNNING 持久化后发布 started；通知异常不影响执行。"""
        if self._lifecycle_sink is None:
            return
        try:
            await self._lifecycle_sink.run_started(task, run)
        except Exception:
            logger.exception("scheduled run started lifecycle notification failed")

    async def _finish_cell(self, cell: _ScheduledRunCell) -> None:
        """发布 durable 结果并释放 live 索引，再发送 finished 生命周期通知。"""
        cell.state = ScheduledRunCellState.FINISHING
        task_run_ids = self._run_ids_by_task.get(cell.receipt.task_id)
        if task_run_ids is not None:
            task_run_ids.discard(cell.receipt.run_id)
            if not task_run_ids:
                self._run_ids_by_task.pop(cell.receipt.task_id, None)
        final = cell.final_run
        if final is None:
            final = self._bridge.record_skipped_submission(
                cell.reservation,
                run_id=cell.receipt.run_id,
                session_id=cell.receipt.session_id,
                reason="scheduled run owner closed without a terminal result",
                cancel_reason=cell.requested_cancel_reason or "scheduler_shutdown",
            )
            cell.final_run = final
        if not cell.result_future.done():
            cell.result_future.set_result(final)
        try:
            if self._lifecycle_sink is not None:
                try:
                    await self._lifecycle_sink.run_finished(
                        cell.reservation.task,
                        final,
                    )
                except asyncio.CancelledError:
                    logger.warning(
                        "scheduled run finished lifecycle notification cancelled run_id=%s",
                        cell.receipt.run_id,
                    )
                except Exception:
                    logger.exception("scheduled run finished lifecycle notification failed")
        finally:
            cell.state = ScheduledRunCellState.FINISHED
            self._prune_finished_cells()

    def _prune_finished_cells(self) -> None:
        """按固定上限淘汰最旧终态 cell，限制幂等回执与结果缓存占用。"""
        finished_cells = [
            cell
            for cell in self._cells_by_run.values()
            if cell.state is ScheduledRunCellState.FINISHED and cell.result_future.done()
        ]
        overflow = len(finished_cells) - self._max_retained_runs
        for oldest_finished in finished_cells[: max(overflow, 0)]:
            self._cells_by_run.pop(oldest_finished.receipt.run_id, None)
            current = self._cells_by_reservation.get(oldest_finished.receipt.reservation_id)
            if current is oldest_finished:
                self._cells_by_reservation.pop(
                    oldest_finished.receipt.reservation_id,
                    None,
                )


def _with_effective_thread(
    reservation: DueTaskReservation,
) -> DueTaskReservation:
    """为历史空 thread 任务派生稳定 thread id，保持每次 run 使用 fresh session。"""
    if reservation.task.thread_id:
        return reservation
    digest = hashlib.sha256(reservation.task.task_id.encode("utf-8")).hexdigest()[:12]
    task = replace(reservation.task, thread_id=f"thread-{digest}")
    return replace(reservation, task=task)


def _reservation_fingerprint(reservation: DueTaskReservation) -> str:
    """生成 reservation payload 指纹，检测同主键异内容冲突。"""
    payload = json.dumps(
        asdict(reservation),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _scheduled_run_result(run: ScheduledRun) -> Result:
    """把面板用 ScheduledRun 映射为 AgentManager 消费的统一 Result。"""
    final_message = (
        Message.assistant(content=run.final_message_excerpt)
        if run.final_message_excerpt is not None
        else None
    )
    metadata: dict[str, object] = {
        "scheduled_run_status": run.status.value,
        "reservation_id": run.reservation_id,
    }
    if run.cancel_reason is not None:
        metadata["cancel_reason"] = run.cancel_reason
    status: Literal["completed", "failed", "cancelled"]
    if run.status in {RunStatus.COMPLETED, RunStatus.SILENT}:
        status = "completed"
        error = None
    elif run.status is RunStatus.CANCELLED:
        status = "cancelled"
        error = None
    else:
        status = "failed"
        error = AgentError(run.error_message or f"scheduled run ended as {run.status.value}")
    return Result(
        run_id=run.run_id,
        session_id=run.session_id or "",
        status=status,
        final_message=final_message,
        turn_count=0,
        error=error,
        metadata=metadata,
    )


__all__ = [
    "ScheduledRunCellState",
    "ScheduledRunDispatcher",
    "ScheduledRunDispatcherFactory",
    "ScheduledRunExecution",
    "ScheduledRunManager",
]
