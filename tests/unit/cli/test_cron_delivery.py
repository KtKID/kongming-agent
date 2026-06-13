"""v0.3 cron-delivery M5：``CliDeliverySink`` 单元测试。

覆盖：

- deliver → buffer + 返回 DeliveryResult.delivered()
- drain_pending：空 buffer 返回 ""；非空清空 buffer 返回拼接字符串
- drain_pending 幂等（连续两次第二次返回 ""）
- buffer 上限 100 行；溢出丢最旧（FIFO）
- 并发 deliver 不破坏 buffer（asyncio.Lock 保护）
- 输出格式含 ``📌 [cron:<task.name>] <final_message>``
"""

from __future__ import annotations

import asyncio

import pytest

from hosts.cli.cron_delivery import CliDeliverySink
from scheduler.delivery import DeliveryResult, DeliveryStatus
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


def _make_task(*, name: str = "测试任务") -> ScheduledTask:
    return ScheduledTask(
        task_id="t-cli",
        name=name,
        enabled=True,
        state=TaskState.SCHEDULED,
        origin=TaskOrigin.TOOL,
        trigger=ScheduleTrigger(trigger_type=TriggerType.INTERVAL, expr="10", timezone="UTC"),
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


def _make_run(*, run_id: str = "r-cli") -> ScheduledRun:
    return ScheduledRun(
        run_id=run_id,
        task_id="t-cli",
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


# ---------------------------------------------------------------------------
# deliver / drain_pending 基本行为
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_returns_delivered_result():
    """deliver 始终返回 DELIVERED；status 与工厂方法一致。"""
    sink = CliDeliverySink()
    result = await sink.deliver(_make_task(), _make_run(), "hi")

    assert isinstance(result, DeliveryResult)
    assert result.status is DeliveryStatus.DELIVERED
    assert result.delivered_at is not None


@pytest.mark.asyncio
async def test_drain_pending_empty_buffer_returns_empty_string():
    """空 buffer drain 返回 ""，不抛错。"""
    sink = CliDeliverySink()
    assert await sink.drain_pending() == ""


@pytest.mark.asyncio
async def test_drain_pending_returns_concatenated_buffer():
    """deliver 多次 → drain 一次性拿到拼接结果，按 FIFO 顺序。"""
    sink = CliDeliverySink()
    await sink.deliver(_make_task(name="任务A"), _make_run(run_id="r-1"), "msg1")
    await sink.deliver(_make_task(name="任务B"), _make_run(run_id="r-2"), "msg2")

    pending = await sink.drain_pending()
    # 顺序：A 在前，B 在后
    assert pending.index("任务A") < pending.index("任务B")
    assert "msg1" in pending
    assert "msg2" in pending


@pytest.mark.asyncio
async def test_drain_pending_idempotent_clears_buffer():
    """drain 后 buffer 清空；连续两次第二次返回 ""。"""
    sink = CliDeliverySink()
    await sink.deliver(_make_task(), _make_run(), "once")

    first = await sink.drain_pending()
    second = await sink.drain_pending()

    assert first != ""
    assert "once" in first
    assert second == ""


# ---------------------------------------------------------------------------
# 输出格式
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_format_contains_marker_and_task_name():
    """格式约定：含 📌 标记 + [cron:<name>] 前缀 + final_message。"""
    sink = CliDeliverySink()
    await sink.deliver(_make_task(name="喝水提醒"), _make_run(), "该喝水啦")

    pending = await sink.drain_pending()
    assert "📌" in pending
    assert "[cron:喝水提醒]" in pending
    assert "该喝水啦" in pending


@pytest.mark.asyncio
async def test_deliver_format_wrapping_newlines():
    """每条投递前后都有 \\n，避免直接撞到 prompt。"""
    sink = CliDeliverySink()
    await sink.deliver(_make_task(), _make_run(), "hi")

    pending = await sink.drain_pending()
    assert pending.startswith("\n")
    assert pending.endswith("\n")


# ---------------------------------------------------------------------------
# buffer 上限：FIFO 丢最旧
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buffer_caps_at_max_lines_drops_oldest():
    """deque(maxlen=100) → 第 101 条进来挤掉第 1 条。"""
    sink = CliDeliverySink()
    n = sink.MAX_BUFFER_LINES + 5  # 105
    for i in range(n):
        await sink.deliver(_make_task(), _make_run(), f"msg-{i:03d}")

    pending = await sink.drain_pending()
    # 前 5 条（msg-000..msg-004）应被挤掉
    assert "msg-000" not in pending
    assert "msg-004" not in pending
    # 最后 100 条（msg-005..msg-104）应都在
    assert "msg-005" in pending
    assert "msg-104" in pending


@pytest.mark.asyncio
async def test_overflow_still_returns_delivered():
    """buffer 满溢出时 deliver 仍返回 DELIVERED（消息已被接收，只是队列丢最旧）。"""
    sink = CliDeliverySink()
    for _ in range(sink.MAX_BUFFER_LINES + 1):
        result = await sink.deliver(_make_task(), _make_run(), "x")
        assert result.status is DeliveryStatus.DELIVERED


# ---------------------------------------------------------------------------
# 并发安全
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_deliver_preserves_count():
    """50 个并发 deliver → buffer 完整保留 50 条（asyncio.Lock 保护写入）。"""
    sink = CliDeliverySink()
    n = 50

    async def one(i: int) -> None:
        await sink.deliver(_make_task(), _make_run(), f"concurrent-{i}")

    await asyncio.gather(*(one(i) for i in range(n)))

    pending = await sink.drain_pending()
    # 每条都能找到（顺序无所谓，并发不保证顺序）
    for i in range(n):
        assert f"concurrent-{i}" in pending


@pytest.mark.asyncio
async def test_concurrent_deliver_and_drain_no_loss():
    """deliver 与 drain 并发：所有 deliver 出去的内容最终都被 drain 出来或清空。

    实测：先发 100 条；中途随机 drain 几次；最后 drain。每次 drain 拿到的内容
    并集应覆盖所有发送内容（无丢失）。
    """
    sink = CliDeliverySink()
    n = 100
    drained_chunks: list[str] = []

    async def producer() -> None:
        for i in range(n):
            await sink.deliver(_make_task(), _make_run(), f"prod-{i:03d}")

    async def consumer() -> None:
        # 跑期周期 drain 几次，模拟 REPL 主循环每次 prompt 前 flush
        for _ in range(5):
            await asyncio.sleep(0.001)
            chunk = await sink.drain_pending()
            drained_chunks.append(chunk)

    await asyncio.gather(producer(), consumer())
    # 最终再 drain 一次拿剩余
    drained_chunks.append(await sink.drain_pending())

    combined = "".join(drained_chunks)
    for i in range(n):
        assert f"prod-{i:03d}" in combined, f"missing prod-{i:03d}"
