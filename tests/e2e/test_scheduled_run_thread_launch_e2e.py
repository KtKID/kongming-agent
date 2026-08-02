"""Store → ticker → ScheduledRunManager → HostDispatcher → Runner → Store 薄链路。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from application.scheduled_runs.execution_bridge import ExecutionBridge
from application.scheduled_runs.manager import ScheduledRunManager
from core import AgentSpec, InMemorySession, ToolCall
from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    LLMRequest,
    LLMResponse,
    PreparedToolCall,
    Tool,
    ToolContext,
    ToolResult,
)
from core.message import Message
from hosts.shared.host_dispatcher import build_scheduled_run_dispatcher_factory
from hosts.web.app_support.cron_delivery import WebDeliverySink
from hosts.web.websocket.cron import CronWSBroker
from infrastructure.config.models import Config, ModelSelectionConfig
from runtime_assembly.session_engine import SessionEngine
from safety.auto_approval.disposition import ApprovalDispositionMode
from scheduler.delivery import DeliveryDispatcher, DeliveryResult
from scheduler.domain import (
    ConcurrencyPolicy,
    DeliveryChannel,
    RunStatus,
    ScheduleDelivery,
    ScheduledRun,
    ScheduledTask,
    ScheduleTrigger,
    TaskExecutionPolicy,
    TaskLifecycleState,
    TaskOrigin,
    TaskTarget,
    TriggerType,
)
from scheduler.store import Store
from scheduler.ticker import tick
from scheduler.timing import to_iso, utc_now
from tests.unit.web.test_cron_router import _login_client_with_store


class _StubLLM:
    """记录真实 Runner 是否抵达外部 LLM 边界。"""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        del request
        self.calls += 1
        return LLMResponse(message=Message.assistant(content="scheduled result"))


class _AllowApproval:
    """无工具路径使用的审批占位。"""

    def __init__(self) -> None:
        self.calls: list[ApprovalRequest] = []

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        self.calls.append(request)
        return ApprovalDecision(outcome="approved")


class _BlockingLLM:
    """在真实 SessionEngine/Runner 链末端阻塞，并记录当前注册 Task。"""

    def __init__(self, *, require_first_cancel_before_second: bool = False) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.first_cancelled = asyncio.Event()
        self.require_first_cancel_before_second = require_first_cancel_before_second
        self.task_names: list[str] = []
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        del request
        self.calls += 1
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("LLM call requires current Task")
        self.task_names.append(current.get_name())
        if self.calls == 1:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.first_cancelled.set()
                raise
            return LLMResponse(
                message=Message.assistant(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            call_id="scheduled-echo",
                            tool_name="scheduled_echo",
                            arguments={"value": "ok"},
                        )
                    ],
                ),
                finish_reason="tool_calls",
            )
        if self.require_first_cancel_before_second and not self.first_cancelled.is_set():
            raise AssertionError("replacement Runner started before old Runner cancelled")
        return LLMResponse(
            message=Message.assistant(content="scheduled result"),
            finish_reason="stop",
        )


class _EchoTool:
    """证明 scheduled run 穿过真实 Safety 与工具执行链。"""

    name = "scheduled_echo"
    description = "echo a value"
    input_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    def __init__(self) -> None:
        self.calls: list[PreparedToolCall] = []

    async def execute(
        self,
        prepared: PreparedToolCall,
        ctx: ToolContext,
    ) -> ToolResult:
        del ctx
        self.calls.append(prepared)
        return ToolResult(ok=True, content=str(prepared.arguments["value"]))


class _FullTrustResolver:
    """测试专用安全处置真源；DangerGuard 仍先于该模式执行。"""

    def mode_for(self, cwd: str) -> ApprovalDispositionMode:
        del cwd
        return ApprovalDispositionMode.FULL_TRUST


class _BlockingDeliverySink:
    """模拟投递阶段无限等待，用于证明取消不会悬挂 Manager future。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def deliver(
        self,
        task: ScheduledTask,
        run: ScheduledRun,
        final_message: str,
    ) -> DeliveryResult:
        del task, run, final_message
        self.started.set()
        await asyncio.Event().wait()
        return DeliveryResult.delivered(at="2026-07-31T00:00:00+00:00")


class _CapturingDeliverySink:
    """记录真实 ExecutionBridge 完成投递的 run，验证被替换 run 零迟到投递。"""

    def __init__(self) -> None:
        self.run_ids: list[str] = []

    async def deliver(
        self,
        task: ScheduledTask,
        run: ScheduledRun,
        final_message: str,
    ) -> DeliveryResult:
        del task
        assert final_message == "scheduled result"
        self.run_ids.append(run.run_id)
        return DeliveryResult.delivered(at="2026-07-31T00:00:00+00:00")


class _LifecycleSink:
    """记录 started/finished 顺序和对应 durable IDs。"""

    def __init__(self, store: Store) -> None:
        self._store = store
        self.started: list[ScheduledRun] = []
        self.finished: list[ScheduledRun] = []
        self.persisted_status_at_started: list[RunStatus] = []
        self.persisted_status_at_finished: list[RunStatus] = []
        self.live_reader: Callable[[str], bool] | None = None
        self.live_at_started: list[bool] = []
        self.live_at_finished: list[bool] = []

    async def run_started(self, task: ScheduledTask, run: ScheduledRun) -> None:
        self.started.append(run)
        persisted = self._store.get_run(task.task_id, run.run_id)
        assert persisted is not None
        self.persisted_status_at_started.append(persisted.status)
        if self.live_reader is not None:
            self.live_at_started.append(self.live_reader(task.task_id))

    async def run_finished(self, task: ScheduledTask, run: ScheduledRun) -> None:
        self.finished.append(run)
        persisted = self._store.get_run(task.task_id, run.run_id)
        assert persisted is not None
        self.persisted_status_at_finished.append(persisted.status)
        if self.live_reader is not None:
            self.live_at_finished.append(self.live_reader(task.task_id))


class _CapturingCronWebSocket:
    """接入真实 CronWSBroker，记录 Manager 生命周期发布的 wire frame。"""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.finished = asyncio.Event()

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.frames.append(payload)
        if payload.get("frame_type") == "cron.run.finished":
            self.finished.set()


class _FailFirstRunningStore(Store):
    """首次写 RUNNING 失败，后续 terminal 与 incident 正常落盘。"""

    def __init__(self, home: Path) -> None:
        super().__init__(home)
        self._failed = False

    def append_run(self, run: ScheduledRun) -> None:
        if run.status is RunStatus.RUNNING and not self._failed:
            self._failed = True
            raise OSError("injected RUNNING persistence failure")
        super().append_run(run)


def _build_runtime(
    *,
    llm: _BlockingLLM | _StubLLM,
    agent_spec: AgentSpec,
    approval: _AllowApproval | None = None,
    tools: dict[str, Tool] | None = None,
) -> SessionEngine:
    """装配真实 SessionEngine、SafetyGatedApproval 与 Runner。"""
    return SessionEngine.build(
        Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it")),
        llm_provider=llm,
        approval=approval or _AllowApproval(),
        tools=tools or {},
        enabled_tool_names=list(tools or {}),
        session_factory=lambda session_id: InMemorySession(session_id),
        agent_spec=agent_spec,
        disposition_resolver=_FullTrustResolver(),
    )


def _task() -> ScheduledTask:
    """构造当前已到期的一次性任务。"""
    now = to_iso(utc_now())
    return ScheduledTask(
        task_id="task-thread-e2e",
        name="thread launch e2e",
        lifecycle=TaskLifecycleState.SCHEDULED,
        origin=TaskOrigin.SYSTEM,
        trigger=ScheduleTrigger(
            trigger_type=TriggerType.ONCE,
            expr=now,
            timezone="UTC",
        ),
        policy=TaskExecutionPolicy(
            concurrency_policy=ConcurrencyPolicy.FORBID,
        ),
        target=TaskTarget(agent_name="default", input_text="run scheduled job"),
        next_run_at=now,
        last_run_at=None,
        created_by="test",
        created_at=now,
        updated_at=now,
        thread_id="thread-aaaaaaaaaaaa",
    )


@pytest.mark.asyncio
async def test_real_runner_watchdog_persists_inactivity_timeout(
    tmp_path: Path,
) -> None:
    """真实 Runner 吞取消并返回 cancelled 时，watchdog 仍保留超时业务语义。"""
    store = Store(tmp_path)
    task = store.create_task(
        replace(
            _task(),
            task_id="task-real-runner-timeout",
            name="real runner timeout",
            policy=replace(
                _task().policy,
                inactivity_timeout_seconds=1,
            ),
        )
    )
    llm = _BlockingLLM()
    runtime = _build_runtime(
        llm=llm,
        agent_spec=AgentSpec(
            name="scheduled-timeout",
            instructions="",
            default_model="stub",
            tool_names=(),
            max_turns=2,
        ),
    )
    bridge = ExecutionBridge(
        runtime=runtime,
        llm=runtime.llm,
        tools=runtime.tools,
        enabled_tool_names=runtime.enabled_tool_names,
        inner_approval=runtime.approval,
        session_factory=runtime.session_factory,
        event_sinks=runtime.event_sinks,
        agent_spec=runtime.agent_spec,
        store=store,
        watchdog_poll_interval_seconds=0.01,
    )
    manager = ScheduledRunManager(
        bridge=bridge,
        dispatcher_factory=build_scheduled_run_dispatcher_factory(runtime),
        max_inflight=1,
    )

    assert await tick(store, manager, now=task.next_run_at) == {
        "due_count": 1,
        "spawned": 1,
    }
    await llm.started.wait()
    run_id = manager.live_run_ids(task.task_id)[0]
    final = await asyncio.wait_for(manager.wait_for_run(run_id), timeout=2.0)

    assert final.status is RunStatus.INACTIVITY_TIMEOUT
    assert final.failure_reason is not None
    assert store.get_run(task.task_id, run_id) == final
    assert store.get_task(task.task_id).last_run_at == final.finished_at  # type: ignore[union-attr]
    await manager.aclose()
    await runtime.aclose()


@pytest.mark.asyncio
async def test_manager_finished_frame_matches_terminal_rest_projection(
    tmp_path: Path,
) -> None:
    """真实 Manager finished frame 与 REST 面板数据读取同一 durable terminal。"""
    client, store = _login_client_with_store(tmp_path)
    runtime: SessionEngine | None = None
    manager: ScheduledRunManager | None = None
    broker = CronWSBroker()
    web_socket = _CapturingCronWebSocket()
    await broker.attach(web_socket)  # type: ignore[arg-type]
    try:
        task = store.create_task(
            replace(
                _task(),
                task_id="task-web-projection",
                name="web projection",
            )
        )
        runtime = _build_runtime(
            llm=_StubLLM(),
            agent_spec=AgentSpec(
                name="scheduled-web",
                instructions="",
                default_model="stub",
                tool_names=(),
                max_turns=2,
            ),
        )
        bridge = ExecutionBridge(
            runtime=runtime,
            llm=runtime.llm,
            tools=runtime.tools,
            enabled_tool_names=runtime.enabled_tool_names,
            inner_approval=runtime.approval,
            session_factory=runtime.session_factory,
            event_sinks=runtime.event_sinks,
            agent_spec=runtime.agent_spec,
            store=store,
        )
        manager = ScheduledRunManager(
            bridge=bridge,
            dispatcher_factory=build_scheduled_run_dispatcher_factory(runtime),
            max_inflight=1,
            lifecycle_sink=WebDeliverySink(broker),
        )

        assert await tick(store, manager, now=task.next_run_at) == {
            "due_count": 1,
            "spawned": 1,
        }
        await asyncio.wait_for(web_socket.finished.wait(), timeout=1.0)
        finished_frame = next(
            frame for frame in web_socket.frames if frame["frame_type"] == "cron.run.finished"
        )
        final = await manager.wait_for_run(str(finished_frame["run_id"]))

        response = client.get(f"/api/cron/tasks/{task.task_id}/runs?limit=10")
        assert response.status_code == 200, response.text
        [rest_run] = response.json()
        assert [frame["frame_type"] for frame in web_socket.frames] == [
            "cron.run.started",
            "cron.run.finished",
        ]
        assert finished_frame["run_id"] == final.run_id == rest_run["run_id"]
        assert finished_frame["status"] == final.status.value == rest_run["status"]
        assert (
            finished_frame["final_message"]
            == final.final_message_excerpt
            == rest_run["final_message_excerpt"]
            == "scheduled result"
        )
        assert rest_run["finished_at"] == final.finished_at
        assert store.get_task(task.task_id).last_run_at == final.finished_at  # type: ignore[union-attr]
    finally:
        await broker.detach(web_socket)  # type: ignore[arg-type]
        if manager is not None:
            await manager.aclose()
        if runtime is not None:
            await runtime.aclose()
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_ticker_launches_scheduled_run_through_real_thread_task_chain(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    task = store.create_task(_task())
    llm = _BlockingLLM()
    approval = _AllowApproval()
    echo_tool = _EchoTool()
    lifecycle = _LifecycleSink(store)
    agent_spec = AgentSpec(
        name="scheduled-e2e",
        instructions="",
        default_model="stub",
        tool_names=(echo_tool.name,),
        max_turns=3,
    )
    runtime = _build_runtime(
        llm=llm,
        agent_spec=agent_spec,
        approval=approval,
        tools={echo_tool.name: echo_tool},
    )
    bridge = ExecutionBridge(
        runtime=runtime,
        llm=runtime.llm,
        tools=runtime.tools,
        enabled_tool_names=runtime.enabled_tool_names,
        inner_approval=runtime.approval,
        session_factory=runtime.session_factory,
        event_sinks=runtime.event_sinks,
        agent_spec=runtime.agent_spec,
        store=store,
    )
    manager = ScheduledRunManager(
        bridge=bridge,
        dispatcher_factory=build_scheduled_run_dispatcher_factory(
            runtime,
        ),
        max_inflight=2,
        lifecycle_sink=lifecycle,
    )
    lifecycle.live_reader = manager.has_live_task

    stats = await tick(store, manager, now=task.next_run_at)
    await llm.started.wait()
    live_ids = manager.live_run_ids(task.task_id)
    assert stats == {"due_count": 1, "spawned": 1}
    assert len(live_ids) == 1
    run_id = live_ids[0]

    running = store.get_run(task.task_id, run_id)
    records = manager.list_run_task_records(run_id)
    assert running is not None
    assert running.status is RunStatus.RUNNING
    assert running.thread_id == task.thread_id
    assert running.session_id is not None
    assert len(records) == 1
    assert records[0].thread_id == task.thread_id
    assert records[0].workflow_task_id == task.task_id
    assert records[0].task_run_id == run_id
    assert records[0].session_id == running.session_id

    llm.release.set()
    final = await manager.wait_for_run(run_id)
    persisted = store.get_run(task.task_id, run_id)

    assert final.status is RunStatus.COMPLETED, (
        final.error_message,
        final.failure_reason,
        approval.calls,
        echo_tool.calls,
    )
    assert persisted == final
    assert llm.task_names == [
        f"agent-run-{records[0].agent_id}-user_message",
        f"agent-run-{records[0].agent_id}-user_message",
    ]
    assert approval.calls == []
    assert len(echo_tool.calls) == 1
    assert lifecycle.started[0].status is RunStatus.RUNNING
    assert lifecycle.finished[0].status is RunStatus.COMPLETED
    assert lifecycle.persisted_status_at_started == [RunStatus.RUNNING]
    assert lifecycle.persisted_status_at_finished == [RunStatus.COMPLETED]
    assert lifecycle.live_at_started == [True]
    assert lifecycle.live_at_finished == [False]
    assert store.get_task(task.task_id).last_run_at == final.finished_at  # type: ignore[union-attr]
    assert manager.live_run_ids(task.task_id) == ()
    await manager.aclose()
    await runtime.aclose()


@pytest.mark.asyncio
async def test_running_persistence_failure_closes_live_owner_and_records_incident(
    tmp_path: Path,
) -> None:
    store = _FailFirstRunningStore(tmp_path)
    task = store.create_task(_task())
    reservation = store.reserve_due_tasks(now=task.next_run_at)[0]
    llm = _StubLLM()
    lifecycle = _LifecycleSink(store)
    agent_spec = AgentSpec(
        name="scheduled-e2e",
        instructions="",
        default_model="stub",
        tool_names=(),
        max_turns=2,
    )
    runtime = _build_runtime(llm=llm, agent_spec=agent_spec)
    bridge = ExecutionBridge(
        runtime=runtime,
        llm=runtime.llm,
        tools=runtime.tools,
        enabled_tool_names=runtime.enabled_tool_names,
        inner_approval=runtime.approval,
        session_factory=runtime.session_factory,
        event_sinks=runtime.event_sinks,
        agent_spec=runtime.agent_spec,
        store=store,
    )
    manager = ScheduledRunManager(
        bridge=bridge,
        dispatcher_factory=build_scheduled_run_dispatcher_factory(
            runtime,
        ),
        max_inflight=1,
        lifecycle_sink=lifecycle,
    )
    lifecycle.live_reader = manager.has_live_task

    receipt = await manager.submit_scheduled_run(reservation)
    final = await manager.wait_for_run(receipt.run_id)

    assert final.status is RunStatus.FAILED
    assert store.get_run(task.task_id, receipt.run_id) == final
    assert llm.calls == 0
    assert lifecycle.started == []
    assert lifecycle.persisted_status_at_finished == [RunStatus.FAILED]
    assert lifecycle.live_at_finished == [False]
    assert [item["action"] for item in store.list_incidents()] == ["run_start_failed"]
    assert store.get_task(task.task_id).last_run_at == final.finished_at  # type: ignore[union-attr]
    assert manager.live_run_ids(task.task_id) == ()
    await manager.aclose()
    await runtime.aclose()


@pytest.mark.asyncio
async def test_cancel_during_blocked_delivery_publishes_terminal_without_deadlock(
    tmp_path: Path,
) -> None:
    """真实 bridge 在 delivery await 期间被中断，durable cancel 仍完整收口。"""
    store = Store(tmp_path)
    task = store.create_task(
        replace(
            _task(),
            task_id="task-delivery-cancel",
            name="delivery cancel",
            target=TaskTarget(agent_name="default", input_text="finish then deliver"),
            delivery=ScheduleDelivery(channel=DeliveryChannel.WEB),
        )
    )
    llm = _StubLLM()
    lifecycle = _LifecycleSink(store)
    delivery_sink = _BlockingDeliverySink()
    agent_spec = AgentSpec(
        name="scheduled-e2e",
        instructions="",
        default_model="stub",
        tool_names=(),
        max_turns=2,
    )
    runtime = _build_runtime(llm=llm, agent_spec=agent_spec)
    bridge = ExecutionBridge(
        runtime=runtime,
        llm=runtime.llm,
        tools=runtime.tools,
        enabled_tool_names=runtime.enabled_tool_names,
        inner_approval=runtime.approval,
        session_factory=runtime.session_factory,
        event_sinks=runtime.event_sinks,
        agent_spec=runtime.agent_spec,
        store=store,
        dispatcher=DeliveryDispatcher(web_sink=delivery_sink),
    )
    manager = ScheduledRunManager(
        bridge=bridge,
        dispatcher_factory=build_scheduled_run_dispatcher_factory(runtime),
        max_inflight=1,
        lifecycle_sink=lifecycle,
    )
    lifecycle.live_reader = manager.has_live_task

    stats = await tick(store, manager, now=task.next_run_at)
    assert stats == {"due_count": 1, "spawned": 1}
    await delivery_sink.started.wait()
    run_id = manager.live_run_ids(task.task_id)[0]

    final = await asyncio.wait_for(manager.cancel_run(run_id), timeout=1.0)

    assert final.status is RunStatus.CANCELLED
    assert final.cancel_reason == "user_interrupt"
    assert store.get_run(task.task_id, run_id) == final
    assert store.get_task(task.task_id).last_run_at == final.finished_at  # type: ignore[union-attr]
    assert manager.live_run_ids(task.task_id) == ()
    assert lifecycle.live_at_finished == [False]
    await manager.aclose()
    await runtime.aclose()


@pytest.mark.asyncio
async def test_replace_cancels_real_runner_before_new_run_and_blocks_late_delivery(
    tmp_path: Path,
) -> None:
    """真实 Store/Manager/HostDispatcher/Runner 链证明 REPLACE 的 live owner 语义。"""
    store = Store(tmp_path)
    task = store.create_task(
        replace(
            _task(),
            task_id="task-real-replace",
            name="real replace",
            policy=replace(
                _task().policy,
                concurrency_policy=ConcurrencyPolicy.REPLACE,
            ),
            delivery=ScheduleDelivery(channel=DeliveryChannel.WEB),
        )
    )
    first_reservation = store.reserve_due_tasks(now=task.next_run_at)[0]
    second_reservation = replace(
        first_reservation,
        reservation_id=f"{first_reservation.reservation_id}-replacement",
    )
    llm = _BlockingLLM(require_first_cancel_before_second=True)
    lifecycle = _LifecycleSink(store)
    delivery_sink = _CapturingDeliverySink()
    runtime = _build_runtime(
        llm=llm,
        agent_spec=AgentSpec(
            name="scheduled-replace-e2e",
            instructions="",
            default_model="stub",
            tool_names=(),
            max_turns=2,
        ),
    )
    bridge = ExecutionBridge(
        runtime=runtime,
        llm=runtime.llm,
        tools=runtime.tools,
        enabled_tool_names=runtime.enabled_tool_names,
        inner_approval=runtime.approval,
        session_factory=runtime.session_factory,
        event_sinks=runtime.event_sinks,
        agent_spec=runtime.agent_spec,
        store=store,
        dispatcher=DeliveryDispatcher(web_sink=delivery_sink),
    )
    manager = ScheduledRunManager(
        bridge=bridge,
        dispatcher_factory=build_scheduled_run_dispatcher_factory(runtime),
        max_inflight=1,
        lifecycle_sink=lifecycle,
    )
    lifecycle.live_reader = manager.has_live_task

    first = await manager.submit_scheduled_run(first_reservation)
    await llm.started.wait()
    [first_record] = manager.list_run_task_records(first.run_id)
    second = await manager.submit_scheduled_run(second_reservation)
    first_final = await asyncio.wait_for(
        manager.wait_for_run(first.run_id),
        timeout=1.0,
    )
    second_final = await asyncio.wait_for(
        manager.wait_for_run(second.run_id),
        timeout=1.0,
    )

    assert first_final.status is RunStatus.CANCELLED
    assert first_final.cancel_reason == "replaced_by_new_run"
    assert second_final.status is RunStatus.COMPLETED
    assert first.session_id != second.session_id
    assert store.get_run(task.task_id, first.run_id) == first_final
    assert store.get_run(task.task_id, second.run_id) == second_final
    assert [run.run_id for run in lifecycle.started] == [first.run_id, second.run_id]
    assert [run.run_id for run in lifecycle.finished] == [first.run_id, second.run_id]
    assert lifecycle.persisted_status_at_finished == [
        RunStatus.CANCELLED,
        RunStatus.COMPLETED,
    ]
    assert delivery_sink.run_ids == [second.run_id]
    assert llm.calls == 2
    assert llm.first_cancelled.is_set()
    assert llm.task_names[0] == (f"agent-run-{first_record.agent_id}-user_message")
    assert manager.live_run_ids(task.task_id) == ()
    await manager.aclose()
    await runtime.aclose()
