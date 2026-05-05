"""v0.3 cron-delivery M3：``DeliveryDispatcher`` + ``DeliveryResult`` 测试。

覆盖：

- ``DeliveryResult`` 三个工厂方法（delivered / failed / skipped）
- ``DeliveryDispatcher`` 路由 5 大分支：
  1. delivery=None → SKIPPED(no_delivery_config)
  2. silent_marker（policy 开关 + [SILENT] 前缀）→ SKIPPED(silent_marker)
  3. run.status=SILENT → SKIPPED(silent_marker)
  4. channel 路由（web / cli）→ 对应 sink 被调用
  5. sink 不存在 → SKIPPED(no_sink_registered)
- sink 抛异常 → 捕获转 FAILED；不破坏路由器
- silent_marker_enabled=False 时不再跳过 [SILENT] 前缀
"""

from __future__ import annotations

import pytest

from scheduler.delivery import (
    DeliveryDispatcher,
    DeliveryResult,
    DeliverySink,
)
from scheduler.domain import (
    ConcurrencyPolicy,
    DeliveryChannel,
    DeliveryStatus,
    MisfirePolicy,
    RunStatus,
    ScheduleDelivery,
    ScheduledRun,
    ScheduledTask,
    ScheduleTrigger,
    SessionMode,
    TaskExecutionPolicy,
    TaskOrigin,
    TaskState,
    TaskTarget,
    TriggerType,
)

# ---------------------------------------------------------------------------
# Fixtures：stub sink + task / run helper
# ---------------------------------------------------------------------------


class _StubSink(DeliverySink):
    """记录调用次数；返回固定 DeliveryResult。"""

    def __init__(self, *, return_result: DeliveryResult | None = None) -> None:
        self.calls: list[tuple[ScheduledTask, ScheduledRun, str]] = []
        self._return = return_result or DeliveryResult.delivered()

    async def deliver(self, task, run, final_message):  # type: ignore[no-untyped-def]
        self.calls.append((task, run, final_message))
        return self._return


class _RaisingSink(DeliverySink):
    """模拟投递失败：每次调用抛 RuntimeError。"""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error or RuntimeError("ws broker dropped connection")

    async def deliver(self, task, run, final_message):  # type: ignore[no-untyped-def]
        raise self._error


def _make_task(
    *,
    task_id: str = "t-disp",
    delivery: ScheduleDelivery | None = None,
    silent_marker_enabled: bool = True,
) -> ScheduledTask:
    return ScheduledTask(
        task_id=task_id,
        name=f"task-{task_id}",
        enabled=True,
        state=TaskState.SCHEDULED,
        origin=TaskOrigin.TOOL,
        trigger=ScheduleTrigger(
            trigger_type=TriggerType.INTERVAL, expr="10", timezone="UTC"
        ),
        policy=TaskExecutionPolicy(
            session_mode=SessionMode.FRESH_SESSION,
            concurrency_policy=ConcurrencyPolicy.FORBID,
            misfire_policy=MisfirePolicy.SKIP,
            silent_marker_enabled=silent_marker_enabled,
        ),
        target=TaskTarget(agent_name="default", input_text="ping", metadata={}),
        next_run_at=None,
        last_run_at=None,
        created_by="tester",
        created_at="2026-05-04T00:00:00+00:00",
        updated_at="2026-05-04T00:00:00+00:00",
        delivery=delivery,
    )


def _make_run(
    *,
    run_id: str = "r-disp",
    task_id: str = "t-disp",
    status: RunStatus = RunStatus.COMPLETED,
) -> ScheduledRun:
    return ScheduledRun(
        run_id=run_id,
        task_id=task_id,
        status=status,
        scheduled_for="2026-05-04T00:00:00+00:00",
        started_at="2026-05-04T00:00:01+00:00",
        finished_at="2026-05-04T00:00:02+00:00",
        session_id=f"sess-{run_id}",
        result_status="completed",
        final_message_excerpt="excerpt-test",
        error_message=None,
        failure_reason=None,
        delivery_error=None,
        silent_suppressed=False,
    )


# ---------------------------------------------------------------------------
# DeliveryResult 工厂方法
# ---------------------------------------------------------------------------


def test_delivery_result_delivered_factory():
    r = DeliveryResult.delivered()
    assert r.status is DeliveryStatus.DELIVERED
    assert r.delivered_at is not None  # ISO 字符串
    assert r.error_message is None
    assert r.skip_reason is None


def test_delivery_result_delivered_explicit_at():
    r = DeliveryResult.delivered(at="2026-05-04T01:00:00+00:00")
    assert r.delivered_at == "2026-05-04T01:00:00+00:00"


def test_delivery_result_failed_factory():
    r = DeliveryResult.failed("ws disconnected")
    assert r.status is DeliveryStatus.FAILED
    assert r.error_message == "ws disconnected"
    assert r.delivered_at is None
    assert r.skip_reason is None


def test_delivery_result_skipped_factory():
    r = DeliveryResult.skipped("silent_marker")
    assert r.status is DeliveryStatus.SKIPPED
    assert r.skip_reason == "silent_marker"
    assert r.delivered_at is None
    assert r.error_message is None


def test_delivery_result_is_frozen():
    """DeliveryResult 是 frozen dataclass。"""
    from dataclasses import FrozenInstanceError

    r = DeliveryResult.delivered()
    with pytest.raises(FrozenInstanceError):
        r.status = DeliveryStatus.FAILED  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DeliveryResult __post_init__ 不变量（v0.3 R2 fix 加固）
# ---------------------------------------------------------------------------


def test_delivery_result_delivered_without_at_raises():
    """status=DELIVERED 但 delivered_at=None → ValueError；防 M4/M5 实现者踩坑。"""
    with pytest.raises(ValueError, match=r"DELIVERED.*requires delivered_at"):
        DeliveryResult(status=DeliveryStatus.DELIVERED, delivered_at=None)


def test_delivery_result_failed_without_error_raises():
    """status=FAILED 但 error_message 空 → ValueError。"""
    with pytest.raises(ValueError, match=r"FAILED.*requires non-empty error_message"):
        DeliveryResult(status=DeliveryStatus.FAILED, error_message=None)
    with pytest.raises(ValueError, match=r"FAILED.*requires non-empty error_message"):
        DeliveryResult(status=DeliveryStatus.FAILED, error_message="")


def test_delivery_result_skipped_without_reason_raises():
    """status=SKIPPED 但 skip_reason 空 → ValueError。"""
    with pytest.raises(ValueError, match=r"SKIPPED.*requires non-empty skip_reason"):
        DeliveryResult(status=DeliveryStatus.SKIPPED, skip_reason=None)
    with pytest.raises(ValueError, match=r"SKIPPED.*requires non-empty skip_reason"):
        DeliveryResult(status=DeliveryStatus.SKIPPED, skip_reason="")


def test_delivery_result_pending_rejected_as_outcome():
    """status=PENDING 不允许作为投递返回值；PENDING 仅用于 ScheduledRun 默认值。"""
    with pytest.raises(ValueError, match="is not a valid delivery outcome"):
        DeliveryResult(status=DeliveryStatus.PENDING)


def test_delivery_result_status_must_be_enum():
    """status 必须是 DeliveryStatus 实例（非 str）。"""
    with pytest.raises(TypeError, match="status must be DeliveryStatus"):
        DeliveryResult(status="delivered")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Dispatcher 路由 — 1) 没有 delivery 配置
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_no_delivery_config_skipped():
    """task.delivery is None → SKIPPED(no_delivery_config)；sink 不被调用。"""
    web_sink = _StubSink()
    cli_sink = _StubSink()
    dispatcher = DeliveryDispatcher(web_sink=web_sink, cli_sink=cli_sink)

    task = _make_task(delivery=None)
    run = _make_run()
    result = await dispatcher.deliver(task, run, "hello")

    assert result.status is DeliveryStatus.SKIPPED
    assert result.skip_reason == "no_delivery_config"
    assert web_sink.calls == []
    assert cli_sink.calls == []


# ---------------------------------------------------------------------------
# Dispatcher 路由 — 2/3) silent_marker 命中
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_silent_marker_prefix_skipped():
    """policy.silent_marker_enabled=True + final_message 含 [SILENT] → SKIPPED。"""
    web_sink = _StubSink()
    dispatcher = DeliveryDispatcher(web_sink=web_sink)

    task = _make_task(
        delivery=ScheduleDelivery(channel=DeliveryChannel.WEB),
        silent_marker_enabled=True,
    )
    run = _make_run()
    result = await dispatcher.deliver(task, run, "[SILENT] internal note")

    assert result.status is DeliveryStatus.SKIPPED
    assert result.skip_reason == "silent_marker"
    assert web_sink.calls == []


@pytest.mark.asyncio
async def test_dispatcher_silent_marker_disabled_does_not_skip():
    """policy.silent_marker_enabled=False → [SILENT] 前缀不被跳过，正常投递。"""
    web_sink = _StubSink()
    dispatcher = DeliveryDispatcher(web_sink=web_sink)

    task = _make_task(
        delivery=ScheduleDelivery(channel=DeliveryChannel.WEB),
        silent_marker_enabled=False,
    )
    run = _make_run()
    result = await dispatcher.deliver(task, run, "[SILENT] but allowed through")

    assert result.status is DeliveryStatus.DELIVERED
    assert len(web_sink.calls) == 1


@pytest.mark.asyncio
async def test_dispatcher_run_status_silent_skipped():
    """run.status=SILENT → SKIPPED 即使 final_message 没含 [SILENT] 前缀。"""
    web_sink = _StubSink()
    dispatcher = DeliveryDispatcher(web_sink=web_sink)

    task = _make_task(delivery=ScheduleDelivery(channel=DeliveryChannel.WEB))
    run = _make_run(status=RunStatus.SILENT)
    # final_message 故意不含 [SILENT]：测的是 run.status 路径
    result = await dispatcher.deliver(task, run, "no marker here")

    assert result.status is DeliveryStatus.SKIPPED
    assert result.skip_reason == "silent_marker"
    assert web_sink.calls == []


# ---------------------------------------------------------------------------
# Dispatcher 路由 — 4) channel 路由到对应 sink
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_routes_to_web_sink():
    web_sink = _StubSink()
    cli_sink = _StubSink()
    dispatcher = DeliveryDispatcher(web_sink=web_sink, cli_sink=cli_sink)

    task = _make_task(delivery=ScheduleDelivery(channel=DeliveryChannel.WEB))
    run = _make_run()
    result = await dispatcher.deliver(task, run, "hi via web")

    assert result.status is DeliveryStatus.DELIVERED
    assert len(web_sink.calls) == 1
    assert cli_sink.calls == []
    # sink 收到的参数原样传递
    sink_task, sink_run, sink_msg = web_sink.calls[0]
    assert sink_task is task
    assert sink_run is run
    assert sink_msg == "hi via web"


@pytest.mark.asyncio
async def test_dispatcher_routes_to_cli_sink():
    web_sink = _StubSink()
    cli_sink = _StubSink()
    dispatcher = DeliveryDispatcher(web_sink=web_sink, cli_sink=cli_sink)

    task = _make_task(delivery=ScheduleDelivery(channel=DeliveryChannel.CLI))
    run = _make_run()
    result = await dispatcher.deliver(task, run, "hi via cli")

    assert result.status is DeliveryStatus.DELIVERED
    assert len(cli_sink.calls) == 1
    assert web_sink.calls == []


# ---------------------------------------------------------------------------
# Dispatcher 路由 — 5) sink 不存在
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_web_channel_no_sink_skipped():
    """task 配 web 但 dispatcher 没装 web_sink → SKIPPED(no_sink_registered)。"""
    dispatcher = DeliveryDispatcher(web_sink=None, cli_sink=_StubSink())

    task = _make_task(delivery=ScheduleDelivery(channel=DeliveryChannel.WEB))
    run = _make_run()
    result = await dispatcher.deliver(task, run, "msg")

    assert result.status is DeliveryStatus.SKIPPED
    assert result.skip_reason == "no_sink_registered"


@pytest.mark.asyncio
async def test_dispatcher_cli_channel_no_sink_skipped():
    dispatcher = DeliveryDispatcher(web_sink=_StubSink(), cli_sink=None)

    task = _make_task(delivery=ScheduleDelivery(channel=DeliveryChannel.CLI))
    run = _make_run()
    result = await dispatcher.deliver(task, run, "msg")

    assert result.status is DeliveryStatus.SKIPPED
    assert result.skip_reason == "no_sink_registered"


@pytest.mark.asyncio
async def test_dispatcher_no_sinks_at_all():
    """空 dispatcher（两个 sink 都为 None）→ 各 channel 都 SKIPPED；不抛错。"""
    dispatcher = DeliveryDispatcher()

    web_task = _make_task(delivery=ScheduleDelivery(channel=DeliveryChannel.WEB))
    cli_task = _make_task(delivery=ScheduleDelivery(channel=DeliveryChannel.CLI))
    run = _make_run()

    web_result = await dispatcher.deliver(web_task, run, "msg")
    cli_result = await dispatcher.deliver(cli_task, run, "msg")

    assert web_result.status is DeliveryStatus.SKIPPED
    assert cli_result.status is DeliveryStatus.SKIPPED


# ---------------------------------------------------------------------------
# Dispatcher 路由 — sink 抛异常
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_sink_exception_returns_failed():
    """sink 抛异常 → FAILED + error_message；dispatcher 不再上抛。"""
    web_sink = _RaisingSink(RuntimeError("ws disconnected"))
    dispatcher = DeliveryDispatcher(web_sink=web_sink)

    task = _make_task(delivery=ScheduleDelivery(channel=DeliveryChannel.WEB))
    run = _make_run()
    result = await dispatcher.deliver(task, run, "msg")

    assert result.status is DeliveryStatus.FAILED
    assert result.error_message is not None
    assert "RuntimeError" in result.error_message
    assert "ws disconnected" in result.error_message


@pytest.mark.asyncio
async def test_dispatcher_sink_cancelled_error_propagates():
    """v0.3 R2 加固：sink 抛 ``CancelledError`` 必须透传，不被 ``except Exception`` 吞。

    场景：用户 Ctrl+C / ticker 主任务被取消时，sink 协程会收到 CancelledError；
    dispatcher 不该把它包成 FAILED 业务结果，应该让 cancel 语义向上传播
    给 ticker / REPL 处理。
    """
    import asyncio

    class _CancelledSink(DeliverySink):
        async def deliver(self, task, run, msg):  # type: ignore[no-untyped-def]
            raise asyncio.CancelledError()

    dispatcher = DeliveryDispatcher(web_sink=_CancelledSink())
    task = _make_task(delivery=ScheduleDelivery(channel=DeliveryChannel.WEB))
    run = _make_run()

    with pytest.raises(asyncio.CancelledError):
        await dispatcher.deliver(task, run, "msg")


@pytest.mark.asyncio
async def test_dispatcher_sink_exception_does_not_pollute_dispatcher_state():
    """sink 第一次失败后，第二次正常调 sink 不受影响。"""
    web_sink = _RaisingSink()
    cli_sink = _StubSink()
    dispatcher = DeliveryDispatcher(web_sink=web_sink, cli_sink=cli_sink)

    web_task = _make_task(delivery=ScheduleDelivery(channel=DeliveryChannel.WEB))
    cli_task = _make_task(delivery=ScheduleDelivery(channel=DeliveryChannel.CLI))
    run = _make_run()

    fail_result = await dispatcher.deliver(web_task, run, "boom")
    ok_result = await dispatcher.deliver(cli_task, run, "ok")

    assert fail_result.status is DeliveryStatus.FAILED
    assert ok_result.status is DeliveryStatus.DELIVERED
    assert len(cli_sink.calls) == 1


# ---------------------------------------------------------------------------
# DeliverySink ABC 校验
# ---------------------------------------------------------------------------


def test_delivery_sink_is_abstract():
    """DeliverySink 是 ABC：缺 deliver 方法的子类不可实例化。"""
    with pytest.raises(TypeError, match="abstract"):
        # type: ignore[abstract]
        DeliverySink()  # type: ignore[abstract]


def test_delivery_sink_subclass_must_implement_deliver():
    """缺实现 deliver → TypeError。"""

    class _BadSink(DeliverySink):
        pass

    with pytest.raises(TypeError, match="abstract"):
        _BadSink()  # type: ignore[abstract]
