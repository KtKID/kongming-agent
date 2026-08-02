"""Scheduler lifecycle 浏览器 E2E 专用真实后端。

功能：在隔离临时目录装配 FastAPI、SchedulerManager、ScheduledRunManager、
HostDispatcher、Runner、Store 和 ExecutionBridge，生成 recurring 失败与 one-shot
exhausted 两组基线，并让 Playwright 手动执行穿过真实 ticker 与 WS 生命周期。

关键函数：
- ``_seed_scheduler``：经真实 ExecutionBridge 和 reservation 构造验收状态。
- ``_build_seed_bridge``：只为基线播种替换 Runner。
- ``_build_live_runtime``：装配浏览器手动执行使用的真实 live owner 与 Runner 链。
- ``main``：装配 Web 应用并在 127.0.0.1:8080 启动。
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import uvicorn
from scheduled_runtime import (
    RunnerBackedScheduledRuntime,
    execute_bridge_for_test,
)
from web_thread_fork_e2e_server import (
    _FileRuntime,
    _write_model_catalog,
)

from application.scheduled_runs.execution_bridge import ExecutionBridge
from application.scheduled_runs.manager import ScheduledRunManager
from core import AgentSpec, InMemorySession
from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    EventSink,
    LLMRequest,
    LLMResponse,
    Session,
    Tool,
)
from core.message import Message
from core.result import Result
from core.runner import Runner
from hosts.shared.host_dispatcher import (
    HostDispatcher,
    build_scheduled_run_dispatcher_factory,
)
from hosts.web.app import create_app
from hosts.web.app_support.cron_delivery import WebDeliverySink
from hosts.web.app_support.host_adapter import WebHostAdapter
from hosts.web.auth.secrets import hash_password
from hosts.web.threads.manager import ThreadManager
from hosts.web.websocket.cron import get_broker
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.models import Config, ModelSelectionConfig
from runtime_assembly.session_engine import SessionEngine
from scheduler.delivery import DeliveryDispatcher
from scheduler.domain import (
    DueTaskReservation,
    RunStatus,
    ScheduledTask,
    ScheduleTrigger,
    TaskExecutionPolicy,
    TaskLifecycleState,
    TaskOrigin,
    TaskTarget,
    TriggerType,
)
from scheduler.store import Store

PASSWORD = "scheduler-e2e-pwd"


class _StubLLM:
    """浏览器 live run 的外部模型边界；短暂等待让 started 状态可见。"""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """返回固定 assistant 消息。"""
        del request
        await asyncio.sleep(0.2)
        return LLMResponse(message=Message.assistant("scheduler e2e"))


class _AllowApproval:
    """允许测试 run 的所有审批。"""

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """返回 approved 决议。"""
        del request
        return ApprovalDecision(outcome="approved")


class _SequencedRunner(Runner):
    """按调用次序返回 failed、completed 两个 Result。"""

    def __init__(self) -> None:
        super().__init__(event_sinks=[])
        self._statuses = ["failed", "completed"]

    async def run(
        self,
        user_input: str,
        *,
        session: Session,
        agent_spec: AgentSpec,
        llm: Any,
        tools: Any,
        approval: Any,
        max_turns: int | None = None,
        run_id: str | None = None,
        enabled_tools: Sequence[Tool] | None = None,
        event_sinks: Sequence[EventSink] | None = None,
        tool_context_metadata: dict[str, Any] | None = None,
    ) -> Result:
        """消费一个预设状态并返回 Runner 合同 Result。"""
        del (
            user_input,
            agent_spec,
            llm,
            tools,
            approval,
            max_turns,
            enabled_tools,
            event_sinks,
            tool_context_metadata,
        )
        status = self._statuses.pop(0)
        return Result(
            run_id=run_id or f"run-{session.session_id}",
            session_id=session.session_id,
            status=status,
            final_message=Message.assistant(
                "recurring failed" if status == "failed" else "one-shot completed"
            ),
            turn_count=1,
            metadata=(
                {"error_type": "RuntimeError", "error_message": "e2e failure"}
                if status == "failed"
                else {}
            ),
        )


def _task(
    *,
    task_id: str,
    name: str,
    trigger: ScheduleTrigger,
    next_run_at: str,
) -> ScheduledTask:
    """构造可由真实 Store 持久化的 scheduler task。"""
    now = "2026-07-28T00:00:00+00:00"
    return ScheduledTask(
        task_id=task_id,
        name=name,
        lifecycle=TaskLifecycleState.SCHEDULED,
        origin=TaskOrigin.WEB,
        trigger=trigger,
        policy=TaskExecutionPolicy(),
        target=TaskTarget(agent_name="default", input_text=name),
        next_run_at=next_run_at,
        last_run_at=None,
        created_by="user",
        created_at=now,
        updated_at=now,
        preset_id="",
        thread_id=(
            "thread-aaaaaaaaaaaa" if task_id == "recurring-failed" else "thread-bbbbbbbbbbbb"
        ),
    )


def _build_seed_bridge(store: Store) -> ExecutionBridge:
    """装配只替换外部 Runner/LLM 的真实 ExecutionBridge。"""
    approval = _AllowApproval()
    return ExecutionBridge(
        runtime=RunnerBackedScheduledRuntime(
            _SequencedRunner(),
            approval=approval,
        ),
        llm=_StubLLM(),
        tools={},
        enabled_tool_names=(),
        inner_approval=approval,
        session_factory=lambda session_id: InMemorySession(session_id),
        event_sinks=(),
        agent_spec=AgentSpec(
            name="default",
            instructions="scheduler e2e",
            default_model="fake",
        ),
        store=store,
        watchdog_poll_interval_seconds=0.01,
    )


def _build_live_runtime(
    store: Store,
) -> tuple[SessionEngine, ScheduledRunManager]:
    """装配浏览器手动执行使用的真实 SessionEngine/Runner/live owner/WS 链。"""
    approval = _AllowApproval()
    runtime = SessionEngine.build(
        Config(
            model=ModelSelectionConfig(
                preset_id="local-gemma-4-e4b-it",
            )
        ),
        llm_provider=_StubLLM(),
        approval=approval,
        tools={},
        enabled_tool_names=[],
        session_factory=lambda session_id: InMemorySession(session_id),
        agent_spec=AgentSpec(
            name="default",
            instructions="scheduler browser e2e",
            default_model="fake",
            max_turns=2,
        ),
    )
    web_sink = WebDeliverySink(get_broker())
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
        dispatcher=DeliveryDispatcher(web_sink=web_sink),
    )
    manager = ScheduledRunManager(
        bridge=bridge,
        dispatcher_factory=build_scheduled_run_dispatcher_factory(runtime),
        max_inflight=1,
        lifecycle_sink=web_sink,
    )
    return runtime, manager


async def _seed_scheduler(store: Store) -> None:
    """生成 recurring failed 与 exhausted completed 两组真实 durable 投影。"""
    recurring = _task(
        task_id="recurring-failed",
        name="Recurring failed",
        trigger=ScheduleTrigger(
            trigger_type=TriggerType.CRON,
            expr="0 9 * * *",
            timezone="Asia/Shanghai",
        ),
        next_run_at="2026-07-30T01:00:00+00:00",
    )
    one_shot = _task(
        task_id="oneshot-running",
        name="One-shot running",
        trigger=ScheduleTrigger(
            trigger_type=TriggerType.ONCE,
            expr="2026-07-29T02:00:00+00:00",
            timezone="UTC",
        ),
        next_run_at="2026-07-29T02:00:00+00:00",
    )
    store.create_task(recurring)
    store.create_task(one_shot)
    bridge = _build_seed_bridge(store)

    await execute_bridge_for_test(
        bridge,
        DueTaskReservation(
            task=recurring,
            scheduled_for="2026-07-29T01:00:00+00:00",
            reserved_at="2026-07-29T01:00:00+00:00",
        ),
    )
    reservation = store.reserve_due_tasks(now="2026-07-29T02:00:01+00:00")[0]
    terminal = await execute_bridge_for_test(bridge, reservation)
    if terminal.status is not RunStatus.COMPLETED:
        raise RuntimeError("one-shot E2E terminal seed failed")

    store.update_task(
        recurring.task_id,
        next_run_at="2099-07-30T01:00:00+00:00",
    )


def _config() -> Config:
    """构造启用隔离 ticker 与 Web dev mode 的配置。"""
    return Config.model_validate(
        {
            "model": {"preset_id": "preset-a"},
            "session": {
                "backend": "file",
                "file_store_path": ".kongming/sessions",
            },
            "web": {
                "enabled": True,
                "dev_mode": True,
                "host": "127.0.0.1",
                "port": 8080,
                "idle_timeout_seconds": 60,
                "idle_check_interval_seconds": 10,
            },
            "scheduler": {
                "enabled": True,
                "interval": 0.1,
                "max_inflight": 1,
            },
        }
    )


def main() -> None:
    """装配隔离 FastAPI 与 scheduler 状态并启动 uvicorn。"""
    temporary_home = tempfile.TemporaryDirectory(prefix="kongming-scheduler-e2e-")
    workspace = Path(temporary_home.name)
    home = workspace / ".kongming"
    home.mkdir(parents=True, exist_ok=True)
    _write_model_catalog(home)
    web_dir = home / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    (web_dir / "password.hash").write_text(
        hash_password(PASSWORD),
        encoding="utf-8",
    )
    config = _config()
    session_store = home / "sessions"
    model_catalog_manager = ModelCatalogManager(user_path=home / "model-providers.yaml")

    async def _runtime_factory(
        thread_id: str,
        preset_id: str,
        adapter: WebHostAdapter,
        event_sinks: list[Any],
    ) -> tuple[Any, Any]:
        """装配真实 FileSession runtime，并固定外部模型边界。"""
        del preset_id, adapter, event_sinks
        runtime = _FileRuntime(session_store, workspace)
        return runtime, HostDispatcher(runtime=runtime, session_id=thread_id)  # type: ignore[arg-type]

    thread_manager = ThreadManager(
        config,
        kongming_home=home,
        runtime_factory=_runtime_factory,
        model_catalog_manager=model_catalog_manager,
    )
    app = create_app(
        config,
        thread_manager,
        home_dir=home,
        scheduler_runtime_factory=_build_live_runtime,
        model_catalog_manager=model_catalog_manager,
        lifespan_shutdown_timeout=1.0,
    )
    scheduler_store = Store(home / "cron")
    asyncio.run(_seed_scheduler(scheduler_store))
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")


if __name__ == "__main__":
    main()
