"""v0.3 cron-delivery M4：``WebDeliverySink`` + ``CronWSBroker`` 单元测试。

覆盖：

- WebDeliverySink.deliver 调用 broker.broadcast，返回 DELIVERED
- broadcast payload 字段与契约一致
- CronWSBroker.attach / detach / broadcast 基本行为
- broadcast 单连接 send 失败 → 自动 detach 该连接，不影响其他
- get_broker 单例语义；reset_broker_for_testing 隔离
- (smoke) /ws/cron endpoint 鉴权失败 close(1008)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from scheduler.delivery import DeliveryStatus
from scheduler.domain import (
    ConcurrencyPolicy,
    MisfirePolicy,
    RunStatus,
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
from web.cron_delivery import WebDeliverySink
from web.cron_ws import CronWSBroker, get_broker, reset_broker_for_testing

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_broker_singleton():
    """每个测试都拿到干净的 broker 单例。"""
    reset_broker_for_testing()
    yield
    reset_broker_for_testing()


def _make_task(*, name: str = "测试任务", task_id: str = "t-web") -> ScheduledTask:
    return ScheduledTask(
        task_id=task_id,
        name=name,
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
        ),
        target=TaskTarget(agent_name="default", input_text="ping", metadata={}),
        next_run_at=None,
        last_run_at=None,
        created_by="tester",
        created_at="2026-05-04T00:00:00+00:00",
        updated_at="2026-05-04T00:00:00+00:00",
    )


def _make_run(*, run_id: str = "r-web") -> ScheduledRun:
    return ScheduledRun(
        run_id=run_id,
        task_id="t-web",
        status=RunStatus.COMPLETED,
        scheduled_for="2026-05-04T00:00:00+00:00",
        started_at="2026-05-04T00:00:01+00:00",
        finished_at="2026-05-04T00:00:02+00:00",
        session_id=f"sess-{run_id}",
        result_status="completed",
        final_message_excerpt="ok",
        error_message=None,
        failure_reason=None,
        delivery_error=None,
        silent_suppressed=False,
    )


def _make_fake_ws() -> Any:
    """构造支持 send_json/close 的伪 WebSocket。"""
    ws = MagicMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    ws.accept = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# WebDeliverySink
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_websink_deliver_calls_broker_broadcast():
    """deliver 调 broker.broadcast 一次，传入符合契约的 payload。"""
    broker = MagicMock(spec=CronWSBroker)
    broker.broadcast = AsyncMock()
    sink = WebDeliverySink(broker)

    task = _make_task(name="每天9点早安", task_id="task-abc")
    run = _make_run(run_id="run-xyz")
    result = await sink.deliver(task, run, "早安")

    assert result.status is DeliveryStatus.DELIVERED
    broker.broadcast.assert_awaited_once()
    payload = broker.broadcast.await_args.args[0]
    assert payload["kind"] == "cron.run.completed"
    assert payload["task_id"] == "task-abc"
    assert payload["task_name"] == "每天9点早安"
    assert payload["run_id"] == "run-xyz"
    assert payload["final_message"] == "早安"
    assert payload["delivered_at_iso"] == run.finished_at
    assert payload["scheduled_for"] == run.scheduled_for


@pytest.mark.asyncio
async def test_websink_returns_delivered_even_when_no_subscribers():
    """没有任何 WS 订阅者时 broker.broadcast 不抛；sink 仍返回 DELIVERED。"""
    broker = CronWSBroker()  # 空连接池
    sink = WebDeliverySink(broker)

    result = await sink.deliver(_make_task(), _make_run(), "msg")

    assert result.status is DeliveryStatus.DELIVERED


# ---------------------------------------------------------------------------
# CronWSBroker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broker_attach_detach_count():
    """attach 增加 connection_count；detach 减回去。"""
    broker = CronWSBroker()
    ws1 = _make_fake_ws()
    ws2 = _make_fake_ws()

    assert broker.connection_count == 0
    await broker.attach(ws1)
    await broker.attach(ws2)
    assert broker.connection_count == 2

    await broker.detach(ws1)
    assert broker.connection_count == 1

    await broker.detach(ws2)
    assert broker.connection_count == 0


@pytest.mark.asyncio
async def test_broker_attach_idempotent():
    """重复 attach 同一连接幂等（set 去重）。"""
    broker = CronWSBroker()
    ws = _make_fake_ws()

    await broker.attach(ws)
    await broker.attach(ws)
    assert broker.connection_count == 1


@pytest.mark.asyncio
async def test_broker_broadcast_to_all_subscribers():
    """broadcast 调每个 WS 的 send_json，payload 一致。"""
    broker = CronWSBroker()
    ws1 = _make_fake_ws()
    ws2 = _make_fake_ws()
    ws3 = _make_fake_ws()
    await broker.attach(ws1)
    await broker.attach(ws2)
    await broker.attach(ws3)

    payload = {"kind": "test", "data": 42}
    await broker.broadcast(payload)

    ws1.send_json.assert_awaited_once_with(payload)
    ws2.send_json.assert_awaited_once_with(payload)
    ws3.send_json.assert_awaited_once_with(payload)


@pytest.mark.asyncio
async def test_broker_broadcast_no_subscribers_noop():
    """空连接池 broadcast 不抛、不调任何 send_json。"""
    broker = CronWSBroker()
    # 不应抛
    await broker.broadcast({"kind": "test"})


@pytest.mark.asyncio
async def test_broker_broadcast_failed_connection_auto_detach():
    """单连接 send_json 抛异常 → broadcast 不抛 + 自动 detach 该连接 +
    其他订阅者照常收到。"""
    broker = CronWSBroker()
    ok_ws = _make_fake_ws()
    bad_ws = _make_fake_ws()
    bad_ws.send_json = AsyncMock(side_effect=RuntimeError("ws closed"))

    await broker.attach(ok_ws)
    await broker.attach(bad_ws)
    assert broker.connection_count == 2

    # broadcast 不抛
    await broker.broadcast({"kind": "test"})

    # ok_ws 收到
    ok_ws.send_json.assert_awaited_once()
    # bad_ws 调过但抛错了
    bad_ws.send_json.assert_awaited_once()
    # bad_ws 已被自动移出
    assert broker.connection_count == 1


@pytest.mark.asyncio
async def test_broker_detach_unknown_ws_noop():
    """detach 一个没 attach 过的 WS 不抛（discard 语义）。"""
    broker = CronWSBroker()
    ws = _make_fake_ws()
    # 不应抛
    await broker.detach(ws)


# ---------------------------------------------------------------------------
# 单例 helper
# ---------------------------------------------------------------------------


def test_get_broker_returns_singleton():
    """get_broker 多次调用返回同一实例。"""
    b1 = get_broker()
    b2 = get_broker()
    assert b1 is b2


def test_reset_broker_for_testing_creates_new_instance():
    """reset 后 get_broker 返回新实例。"""
    b1 = get_broker()
    reset_broker_for_testing()
    b2 = get_broker()
    assert b1 is not b2


# ---------------------------------------------------------------------------
# /ws/cron 鉴权 smoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cron_ws_handler_rejects_missing_serializer():
    """``app.state.serializer`` 缺失时关连接 1008。"""
    from web.cron_ws import _cron_ws_handler

    websocket = MagicMock()
    # 模拟 app.state 没 serializer
    websocket.app.state = MagicMock(spec=[])  # 空 spec → getattr 走 default
    websocket.close = AsyncMock()
    websocket.cookies = {}

    await _cron_ws_handler(websocket)

    websocket.close.assert_awaited_once()
    code = websocket.close.await_args.kwargs.get("code")
    assert code == 1008


@pytest.mark.asyncio
async def test_cron_ws_handler_rejects_invalid_cookie():
    """没 cookie / cookie 验签失败 → 关连接 1008。"""
    from web.cron_ws import _cron_ws_handler

    websocket = MagicMock()
    websocket.app.state = MagicMock()
    websocket.app.state.serializer = MagicMock()  # 提供 serializer
    websocket.cookies = {}  # 没 cookie
    websocket.close = AsyncMock()

    await _cron_ws_handler(websocket)

    websocket.close.assert_awaited_once()
    code = websocket.close.await_args.kwargs.get("code")
    assert code == 1008
