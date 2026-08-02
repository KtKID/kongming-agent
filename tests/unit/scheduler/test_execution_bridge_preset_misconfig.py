"""unit: application.scheduled_runs.execution_bridge._build_provider 错配抛错 → FAILED run 路径（v0.5.3）。

覆盖矩阵：

- W1：``preset_map`` 装配但 ``task.preset_id`` 不在 map 里 → execute 返回
  FAILED run（``failure_reason=RUNNER_EXCEPTION`` + ``error_message`` 含
  ``ValueError`` + preset key 名）；RUNNING 行被 supersede；audit 写
  ``run_failed``。
- W2：兼容路径 — ``task.preset_id=""`` 且 ``preset_map=None`` → 走默认
  ``self._llm`` 正常完成（不 regress 现有用户）。
- W3：兼容路径 — ``task.preset_id=""`` 但 ``preset_map`` 有 entry → 仍走
  默认 ``self._llm``（未声明 preset = 未启用 preset 体系，不查 map）。

设计动机参考 ``dev-pipeline/tasks/scheduler-preset-fallback-removal/README.md``。
不动 ``test_scheduler_execution_bridge.py``，独立文件避免触动现有 80+ 用例。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from application.scheduled_runs.execution_bridge import ExecutionBridge
from core import AgentSpec, InMemorySession
from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    EventSink,
    LLMRequest,
    LLMResponse,
    PreparedToolCall,
    Session,
    Tool,
)
from core.message import Message
from core.result import Result
from core.runner import Runner
from infrastructure.config.models import Config, ModelSelectionConfig
from scheduler.domain import (
    ConcurrencyPolicy,
    DueTaskReservation,
    MisfirePolicy,
    RunFailureReason,
    RunStatus,
    ScheduledTask,
    ScheduleTrigger,
    SessionMode,
    TaskExecutionPolicy,
    TaskLifecycleState,
    TaskOrigin,
    TaskTarget,
    TriggerType,
)
from scheduler.manager import SchedulerManager
from scheduler.store import Store
from scheduler.timing import to_iso, utc_now
from tests._helpers.model_runtime import CatalogModelFixture, make_model_catalog_manager
from tests.support.scheduled_runtime import (
    RunnerBackedScheduledRuntime,
    execute_bridge_for_test,
)

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _StubLLM:
    async def complete(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover
        return LLMResponse(message=Message(role="assistant", content=""))


class _AllowApproval:
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(outcome="approved")


@dataclass
class _NoopTool:
    name: str = "noop"
    description: str = "noop tool"
    input_schema: dict[str, Any] = field(default_factory=dict)

    async def execute(self, prepared: PreparedToolCall, ctx: Any) -> Any:
        del prepared, ctx
        raise NotImplementedError


class _SuccessRunner(Runner):
    """覆写 Runner.run：直接返回 completed Result，不真跑 LLM。

    ``captured_agent_specs`` 记录每次调用收到的 ``agent_spec``，供测试断言
    per-run agent_spec 替换是否生效（v0.5.3 preset.model 同步路径）。
    """

    def __init__(self) -> None:
        super().__init__(event_sinks=[])
        self.captured_agent_specs: list[AgentSpec] = []

    async def run(  # type: ignore[override]
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
        del event_sinks, tool_context_metadata
        self.captured_agent_specs.append(agent_spec)
        return Result(
            run_id=run_id or f"run-{session.session_id}-1",
            session_id=session.session_id,
            status="completed",
            final_message=Message(role="assistant", content="ok"),
            turn_count=1,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_task(*, preset_id: str = "") -> ScheduledTask:
    now = to_iso(utc_now())
    return ScheduledTask(
        task_id="task-misconfig",
        name="t",
        lifecycle=TaskLifecycleState.SCHEDULED,
        origin=TaskOrigin.CLI,
        trigger=ScheduleTrigger(
            trigger_type=TriggerType.INTERVAL,
            expr="5",
            timezone="UTC",
        ),
        policy=TaskExecutionPolicy(
            session_mode=SessionMode.FRESH_SESSION,
            concurrency_policy=ConcurrencyPolicy.FORBID,
            misfire_policy=MisfirePolicy.SKIP,
            max_turns=5,
            inactivity_timeout_seconds=600,
            wall_timeout_seconds=None,
            retry_limit=0,
            silent_marker_enabled=True,
        ),
        target=TaskTarget(agent_name="agent-x", input_text="do thing", metadata={}),
        next_run_at=now,
        last_run_at=None,
        created_by="cli",
        created_at=now,
        updated_at=now,
        preset_id=preset_id,
    )


def _make_reservation(task: ScheduledTask) -> DueTaskReservation:
    now = to_iso(utc_now())
    return DueTaskReservation(
        task=task,
        scheduled_for=task.next_run_at or now,
        reserved_at=now,
    )


def _make_agent_spec() -> AgentSpec:
    return AgentSpec(
        name="agent-x",
        instructions="you are an agent",
        default_model="m",
        max_turns=10,
    )


def _make_base_config(preset_id: str) -> Config:
    """构造只保存默认 preset 的 v0.6 配置。"""
    return Config(model=ModelSelectionConfig(preset_id=preset_id))


def _build_bridge(
    *,
    tmp_path: Path,
    store: Store,
    runner: Runner,
    catalog_models: tuple[CatalogModelFixture, ...] = (),
    event_sinks: Sequence[EventSink] = (),
) -> ExecutionBridge:
    catalog_manager = (
        make_model_catalog_manager(tmp_path, models=catalog_models) if catalog_models else None
    )
    base_config = _make_base_config(catalog_models[0].preset_id) if catalog_models else None
    default_model = (
        catalog_manager.resolve_runtime(base_config.model)
        if catalog_manager is not None and base_config is not None
        else None
    )
    approval = _AllowApproval()
    return ExecutionBridge(
        runtime=RunnerBackedScheduledRuntime(runner, approval=approval),
        llm=_StubLLM(),
        tools={},
        enabled_tool_names=(),
        inner_approval=approval,
        session_factory=lambda sid: InMemorySession(sid),
        event_sinks=event_sinks,
        agent_spec=_make_agent_spec(),
        store=store,
        watchdog_poll_interval_seconds=0.02,
        model_catalog_manager=catalog_manager,
        default_model=default_model,
        base_config=base_config,
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(home_dir=tmp_path / "cron")


# ---------------------------------------------------------------------------
# W1：preset_map 装配但 task.preset_id 不命中 → FAILED + 不 fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_w1_misconfigured_preset_id_yields_failed_run(store: Store, tmp_path: Path) -> None:
    """catalog 有 'foo' 但 task.preset_id='bar' → run 失败 + 写明原因。"""
    bridge = _build_bridge(
        tmp_path=tmp_path,
        store=store,
        runner=_SuccessRunner(),
        catalog_models=(CatalogModelFixture("foo", "Foo-Model"),),
    )
    task = _make_task(preset_id="bar")
    store.create_task(task)

    run = await execute_bridge_for_test(bridge, _make_reservation(task))

    # 1) 返回 FAILED + RUNNER_EXCEPTION（沿用 _build_exception_record 行为）
    assert run.status is RunStatus.FAILED, (
        f"expected FAILED, got {run.status} — misconfigured preset must not fallback"
    )
    assert run.failure_reason is RunFailureReason.RUNNER_EXCEPTION
    assert run.error_message is not None
    # error_message 包含稳定 catalog error 类型与错配 preset。
    assert "ModelProviderCatalogError" in run.error_message
    assert "'bar'" in run.error_message

    # 2) RUNNING 行被 supersede：list_runs 拿到的最终一行是 FAILED
    # store.list_runs 内部已过滤 _superseded=True，返回 list[ScheduledRun]
    runs = store.list_runs(task_id=task.task_id)
    assert len(runs) == 1, f"expected single non-superseded run, got {runs}"
    assert runs[0].status is RunStatus.FAILED
    assert runs[0].error_message == run.error_message

    # 3) audit 写了 run_failed + run_started（_emit_finishing_audit 路径）
    audits = store.list_audits(task_id=task.task_id)
    actions = [a["action"] for a in audits]
    assert "run_started" in actions
    assert "run_failed" in actions

    # SC_15：recurring lifecycle 与失败 run 正交；last_run_at 取 terminal finished_at。
    persisted = store.get_task(task.task_id)
    assert persisted is not None
    assert persisted.lifecycle is TaskLifecycleState.SCHEDULED
    assert persisted.last_run_at == run.finished_at
    projection = SchedulerManager(store).project_task(persisted)
    assert projection.latest_run_status is RunStatus.FAILED


@pytest.mark.asyncio
async def test_one_shot_execution_bridge_finishes_exhausted_task(
    store: Store,
    tmp_path: Path,
) -> None:
    """SC_16：真实 ExecutionBridge 收口 one-shot run 后才写 last_run_at。"""
    task = replace(
        _make_task(),
        task_id="task-one-shot-terminal",
        lifecycle=TaskLifecycleState.SCHEDULED,
        trigger=ScheduleTrigger(
            trigger_type=TriggerType.ONCE,
            expr="2026-07-29T00:00:00+00:00",
            timezone="UTC",
        ),
        next_run_at="2026-07-29T00:00:00+00:00",
        last_run_at=None,
    )
    store.create_task(task)
    reservation = store.reserve_due_tasks(now="2026-07-29T00:00:01+00:00")[0]
    reserved_task = store.get_task(task.task_id)
    assert reserved_task is not None
    assert reserved_task.lifecycle is TaskLifecycleState.EXHAUSTED
    assert reserved_task.last_run_at is None

    bridge = _build_bridge(
        tmp_path=tmp_path,
        store=store,
        runner=_SuccessRunner(),
    )
    run = await execute_bridge_for_test(bridge, reservation)

    persisted = store.get_task(task.task_id)
    assert persisted is not None
    assert run.status is RunStatus.COMPLETED
    assert run.finished_at is not None
    assert persisted.lifecycle is TaskLifecycleState.EXHAUSTED
    assert persisted.last_run_at == run.finished_at
    projection = SchedulerManager(store).project_task(persisted)
    assert projection.latest_run_status is RunStatus.COMPLETED
    assert projection.live_runtime_status is not None
    assert projection.live_runtime_status.value == "idle"


@pytest.mark.asyncio
async def test_w1_misconfigured_preset_does_not_invoke_runner(store: Store, tmp_path: Path) -> None:
    """preset 错配时 runner 不应被调用（_build_provider 抛错位于 runner 之前）。"""

    invoked = {"count": 0}

    class _CountingRunner(Runner):
        def __init__(self) -> None:
            super().__init__(event_sinks=[])

        async def run(self, *args: Any, **kwargs: Any) -> Result:  # type: ignore[override]
            invoked["count"] += 1
            raise RuntimeError("should not be called")

    bridge = _build_bridge(
        tmp_path=tmp_path,
        store=store,
        runner=_CountingRunner(),
        catalog_models=(CatalogModelFixture("foo", "Foo-Model"),),
    )
    run = await execute_bridge_for_test(bridge, _make_reservation(_make_task(preset_id="bar")))

    assert run.status is RunStatus.FAILED
    assert invoked["count"] == 0, "runner.run must not be invoked when preset misconfigured"


# ---------------------------------------------------------------------------
# W2 / W3：保留"未启用 preset"兼容路径不 regress
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_w2_empty_preset_without_catalog_override_uses_default_llm(
    store: Store, tmp_path: Path
) -> None:
    """preset_id="" 且无 catalog override → 走 self._llm，正常完成。"""
    bridge = _build_bridge(tmp_path=tmp_path, store=store, runner=_SuccessRunner())
    run = await execute_bridge_for_test(bridge, _make_reservation(_make_task(preset_id="")))

    assert run.status is RunStatus.COMPLETED, (
        "未启用 preset 体系应保留向后兼容（不抛错），现状被 regress"
    )
    assert run.failure_reason is None


@pytest.mark.asyncio
async def test_w3_empty_preset_with_catalog_uses_default_llm(store: Store, tmp_path: Path) -> None:
    """preset_id="" 且 catalog 已装配 → 仍走默认 snapshot/self._llm。

    语义：task 未声明 preset = 未启用 preset 体系，不查 map，**不算错配**。
    """
    bridge = _build_bridge(
        tmp_path=tmp_path,
        store=store,
        runner=_SuccessRunner(),
        catalog_models=(CatalogModelFixture("foo", "Foo-Model"),),
    )
    run = await execute_bridge_for_test(bridge, _make_reservation(_make_task(preset_id="")))

    assert run.status is RunStatus.COMPLETED
    assert run.failure_reason is None


# ---------------------------------------------------------------------------
# §W4：preset 命中时 agent_spec.default_model 同步替换（v0.5.3 修复 GLM 跑不通）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_w4_preset_hit_overrides_agent_spec_default_model(
    store: Store, tmp_path: Path
) -> None:
    """preset 命中时，runner 收到的 agent_spec.default_model 必须等于 preset.model。

    bug 复现历史：v0.4 切 preset 只换了 LLMProvider，agent_spec 没换，导致
    LLMRequest.model 仍是装配期默认 catalog model。结果：
    ``base_url=智谱 + model=本地默认``，后端回"模型不存在"。
    本测试断言修复后 runner 拿到的 agent_spec.default_model == preset.model。
    """
    runner = _SuccessRunner()
    bridge = _build_bridge(
        tmp_path=tmp_path,
        store=store,
        runner=runner,
        catalog_models=(
            CatalogModelFixture("default", "default-model"),
            CatalogModelFixture("glm5", "glm-5.1"),
        ),
    )

    run = await execute_bridge_for_test(bridge, _make_reservation(_make_task(preset_id="glm5")))

    assert run.status is RunStatus.COMPLETED
    assert len(runner.captured_agent_specs) == 1, "runner.run 应被调一次"
    captured = runner.captured_agent_specs[0]
    assert captured.default_model == "glm-5.1", (
        f"agent_spec.default_model 必须跟 preset 走，实际收到 {captured.default_model!r}。"
        " 这是 v0.4 切 preset 不彻底的核心 bug 修复点。"
    )


@pytest.mark.asyncio
async def test_w4_preset_with_reasoning_effort_overrides_spec(store: Store, tmp_path: Path) -> None:
    """catalog 默认 reasoning effort 会进入 per-run agent spec。"""
    runner = _SuccessRunner()
    bridge = _build_bridge(
        tmp_path=tmp_path,
        store=store,
        runner=runner,
        catalog_models=(
            CatalogModelFixture("default", "default-model"),
            CatalogModelFixture("glm5-high", "glm-5.1", "high"),
        ),
    )

    await execute_bridge_for_test(bridge, _make_reservation(_make_task(preset_id="glm5-high")))

    captured = runner.captured_agent_specs[0]
    assert captured.default_model == "glm-5.1"
    assert captured.reasoning_effort == "high"


@pytest.mark.asyncio
async def test_w4_preset_without_reasoning_effort_clears_spec_value(
    store: Store, tmp_path: Path
) -> None:
    """catalog 未声明 reasoning capability 时 per-run spec 使用 None。"""
    runner = _SuccessRunner()
    bridge = _build_bridge(
        tmp_path=tmp_path,
        store=store,
        runner=runner,
        catalog_models=(
            CatalogModelFixture("default", "default-model"),
            CatalogModelFixture("plain", "plain-model"),
        ),
    )

    await execute_bridge_for_test(bridge, _make_reservation(_make_task(preset_id="plain")))

    captured = runner.captured_agent_specs[0]
    assert captured.default_model == "plain-model"
    # _make_agent_spec 默认 reasoning_effort=None，replace 时不覆盖
    assert captured.reasoning_effort is None


@pytest.mark.asyncio
async def test_w4_no_preset_keeps_default_agent_spec(store: Store, tmp_path: Path) -> None:
    """preset_id="" 时 agent_spec 不变（向后兼容）。"""
    runner = _SuccessRunner()
    bridge = _build_bridge(tmp_path=tmp_path, store=store, runner=runner)

    await execute_bridge_for_test(bridge, _make_reservation(_make_task(preset_id="")))

    captured = runner.captured_agent_specs[0]
    # _make_agent_spec 设的 default_model="m"
    assert captured.default_model == "m"
