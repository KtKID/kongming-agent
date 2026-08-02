"""TaskRegistry 单元测试（agent-tree-v0.1 task-3 验证闭环）。

覆盖：
1. register_run/register_external 写账本 + 返回 TaskRecord（字段齐全）。
2. cancel_subtree 后序顺序：external PID 先 kill+wait，run_task 后 cancel（SM-001）。
3. 关门标志：close_registry 后 register_* 抛错（SM-002）。
4. no_live_descendants 判定（SM-003）。
5. kill+wait 真实 subprocess 无僵尸（SM-004）。
6. 单 agent 退化：cancel_subtree(root) 等价于 cancel root run_task。
7. 幂等：cancel 已 done 的 task 安全；kill 已死 PID 静默吞掉。

验证命令：``uv run pytest tests/unit/test_task_registry.py -v``
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest

from application.agents.registry import (
    PidHandle,
    TaskRecord,
    TaskRegistry,
)

# ---------------------------------------------------------------------------
# #1 register：写账本 + TaskRecord 字段齐全
# ---------------------------------------------------------------------------


async def test_register_run_returns_record_with_fields() -> None:
    """register_run 返回 TaskRecord，字段齐全（kind=agent_run, resources=[]）。"""
    registry = TaskRegistry()

    async def _sleeper() -> str:
        await asyncio.sleep(100)
        return "done"

    task = asyncio.create_task(_sleeper())
    try:
        record = registry.register_run(task, agent_id="root", parent_task_id=None)

        assert isinstance(record, TaskRecord)
        assert record.kind == "agent_run"
        assert record.agent_id == "root"
        assert record.parent_task_id is None
        assert record.status == "running"
        assert record.handle is task
        assert record.resources == []
        assert record.started_at > 0
        assert record.finished_at is None
        assert record.error_message is None
        assert len(record.task_id) == 12
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_register_external_returns_record_with_pid() -> None:
    """register_external 返回 TaskRecord，kind=external_process, handle=None, resources=[PidHandle]。"""
    registry = TaskRegistry()
    record = registry.register_external(
        pid=99999, agent_id="root", parent_task_id=None, kind="bash"
    )

    assert record.kind == "external_process"
    assert record.handle is None
    assert len(record.resources) == 1
    assert record.resources[0] == PidHandle(pid=99999, kind="bash")
    assert record.status == "running"


# ---------------------------------------------------------------------------
# #2 cancel_subtree 后序顺序：external 先 kill+wait，run_task 后 cancel（SM-001）
# ---------------------------------------------------------------------------


async def test_cancel_subtree_postorder_external_before_run() -> None:
    """后序顺序：external PID kill 先于 run_task cancel（SM-001）。

    构造 registry，register_run + register_external 同 agent，记录 kill/cancel
    调用顺序，断言 kill+wait 先于 task.cancel。
    """
    registry = TaskRegistry()
    call_order: list[str] = []

    async def _sleeper() -> str:
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            call_order.append("task_cancelled_received")
            raise
        return "done"

    task = asyncio.create_task(_sleeper())
    registry.register_run(task, agent_id="root", parent_task_id=None)
    registry.register_external(pid=99999, agent_id="root", parent_task_id="t1")

    # patch os.kill 记录 kill 调用（pid 99999 不存在 → ProcessLookupError 静默吞）。
    real_os_kill = os.kill

    def _spy_kill(pid: int, sig: int) -> None:
        if pid == 99999:
            call_order.append(f"kill_{sig}")
        try:
            real_os_kill(pid, sig)
        except ProcessLookupError:
            pass

    with patch("application.agents.registry.os.kill", side_effect=_spy_kill):
        await registry.cancel_subtree("root")

    # 后序断言：所有 kill 调用都在 task_cancelled_received 之前。
    kill_indices = [i for i, e in enumerate(call_order) if e.startswith("kill_")]
    cancel_index = call_order.index("task_cancelled_received")
    assert kill_indices, "expected at least one kill call"
    assert all(i < cancel_index for i in kill_indices), (
        f"kill must precede cancel; order={call_order}"
    )


# ---------------------------------------------------------------------------
# #3 关门标志：close_registry 后 register 抛错（SM-002）
# ---------------------------------------------------------------------------


async def test_close_registry_blocks_register_run() -> None:
    """close_registry 后 register_run 抛 RuntimeError（SM-002）。"""
    registry = TaskRegistry()
    registry.close_registry()
    assert registry.is_closed is True

    async def _noop() -> None:
        pass

    task = asyncio.create_task(_noop())
    try:
        with pytest.raises(RuntimeError, match="closed"):
            registry.register_run(task, agent_id="root", parent_task_id=None)
    finally:
        task.cancel()


async def test_close_registry_blocks_register_external() -> None:
    """close_registry 后 register_external 抛 RuntimeError（SM-002）。"""
    registry = TaskRegistry()
    registry.close_registry()

    with pytest.raises(RuntimeError, match="closed"):
        registry.register_external(pid=12345, agent_id="root", parent_task_id=None)


async def test_cancel_subtree_sets_close_flag() -> None:
    """cancel_subtree 入口即置关门标志（防漏杀）。"""
    registry = TaskRegistry()
    await registry.cancel_subtree("unknown")
    assert registry.is_closed is True


# ---------------------------------------------------------------------------
# #4 no_live_descendants 判定（SM-003）
# ---------------------------------------------------------------------------


async def test_no_live_descendants_running_then_done() -> None:
    """no_live_descendants：register_run 未完成→False；task done→True（SM-003）。"""
    registry = TaskRegistry()

    async def _quick() -> str:
        return "done"

    task = asyncio.create_task(_quick())
    registry.register_run(task, agent_id="root", parent_task_id=None)

    # 未完成：有存活 run_task → False。
    assert registry.no_live_descendants("root") is False

    await task  # task 完成 → done callback 回写 status=completed
    await asyncio.sleep(0)  # 让 done callback 跑

    assert registry.no_live_descendants("root") is True


@pytest.mark.parametrize("result_status", ["failed", "cancelled"])
async def test_run_result_terminal_status_is_preserved(result_status: str) -> None:
    """正常返回的 Result-like 对象按其业务终态回写 TaskRecord。"""
    registry = TaskRegistry()

    async def _finish() -> object:
        return type("RunResult", (), {"status": result_status, "error": None})()

    task = asyncio.create_task(_finish())
    record = registry.register_run(task, agent_id="child", parent_task_id=None)
    await task
    await asyncio.sleep(0)

    assert record.status == result_status


async def test_no_live_descendants_external_running() -> None:
    """no_live_descendants：有 running external → False。"""
    registry = TaskRegistry()
    registry.register_external(pid=99999, agent_id="root", parent_task_id=None)
    assert registry.no_live_descendants("root") is False


async def test_no_live_descendants_unknown_agent() -> None:
    """no_live_descendants：未知 agent_id → True（无记录即无存活）。"""
    registry = TaskRegistry()
    assert registry.no_live_descendants("nobody") is True


async def test_no_live_descendants_after_cancel() -> None:
    """no_live_descendants：cancel_subtree 后 → True（已全部 cancelled）。"""
    registry = TaskRegistry()

    async def _sleeper() -> str:
        await asyncio.sleep(100)
        return "done"

    task = asyncio.create_task(_sleeper())
    registry.register_run(task, agent_id="root", parent_task_id=None)
    registry.register_external(pid=99999, agent_id="root", parent_task_id="t1")

    await registry.cancel_subtree("root")
    assert registry.no_live_descendants("root") is True


# ---------------------------------------------------------------------------
# #5 kill+wait 真实 subprocess 无僵尸（SM-004）
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_kill_and_wait_real_subprocess_no_zombie() -> None:
    """kill+wait 真实 subprocess 无僵尸（SM-004）。

    起真实 ``asyncio.create_subprocess_exec("sleep","30")``，register_external，
    await cancel_subtree，断言进程被 kill 且最终 reap（无僵尸）。
    """
    registry = TaskRegistry()
    proc = await asyncio.create_subprocess_exec(
        "sleep",
        "30",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    pid = proc.pid
    registry.register_external(pid=pid, agent_id="root", parent_task_id=None)

    # 确认进程存活。
    assert proc.returncode is None

    await registry.cancel_subtree("root")

    # cancel_subtree 用 os.kill 收口；但 asyncio subprocess 的 reap 需 await
    # proc.wait() 触发 transport 回收。registry 只登记 pid（契约），所以这里
    # 额外 await proc.wait() 完成 reap（模拟真实 runner 持有 Process 引用时的行为）。
    await proc.wait()
    assert proc.returncode is not None, "process must be reaped (no zombie)"
    # returncode 为负 = 被信号终止（-15=SIGTERM, -9=SIGKILL）。
    assert proc.returncode < 0, f"expected signal termination, got {proc.returncode}"


@pytest.mark.slow
async def test_kill_and_wait_real_subtree_does_not_block() -> None:
    """cancel_subtree 杀真实子进程后不挂起（kill+wait 收口，超时保护）。"""
    registry = TaskRegistry()
    proc = await asyncio.create_subprocess_exec(
        "sleep",
        "30",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    registry.register_external(pid=proc.pid, agent_id="root", parent_task_id=None)

    # cancel_subtree 必须在合理时间内返回（kill+wait 收口，不卡死）。
    await asyncio.wait_for(registry.cancel_subtree("root"), timeout=10.0)
    await proc.wait()
    assert proc.returncode is not None


# ---------------------------------------------------------------------------
# #6 单 agent 退化：cancel_subtree(root) 等价于 cancel root run_task
# ---------------------------------------------------------------------------


async def test_single_agent_degenerate_cancel_equivalent() -> None:
    """单 agent 退化：cancel_subtree(root) 等价于 cancel 根 run_task。

    树上只有根节点（一个 agent_run task），cancel_subtree 应让该 task 收到
    CancelledError 并收口。
    """
    registry = TaskRegistry()
    cancel_event = asyncio.Event()

    async def _long_run() -> str:
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            cancel_event.set()
            raise
        return "done"

    task = asyncio.create_task(_long_run())
    registry.register_run(task, agent_id="root", parent_task_id=None)
    await asyncio.sleep(0)  # 让 task 启动进入 await sleep（模拟真实运行态中断）

    await registry.cancel_subtree("root")

    # task 收到 CancelledError。
    assert cancel_event.is_set()
    assert task.cancelled() or task.done()


# ---------------------------------------------------------------------------
# #7 幂等性 + 边界
# ---------------------------------------------------------------------------


async def test_cancel_subtree_unknown_agent_idempotent() -> None:
    """cancel_subtree 未知 agent_id 静默无操作（幂等）。"""
    registry = TaskRegistry()
    # 不抛错。
    await registry.cancel_subtree("nonexistent")
    assert registry.is_closed is True


async def test_cancel_done_task_safe() -> None:
    """cancel 已 done 的 run_task 安全（asyncio.Task.cancel 对 done task 无效，静默跳过）。"""
    registry = TaskRegistry()

    async def _quick() -> str:
        return "done"

    task = asyncio.create_task(_quick())
    registry.register_run(task, agent_id="root", parent_task_id=None)
    await task  # 先完成
    await asyncio.sleep(0)

    # cancel_subtree 不抛错（done task 跳过）。
    await registry.cancel_subtree("root")
    assert task.done()


async def test_kill_dead_pid_silent() -> None:
    """kill 已死 PID 静默吞 ProcessLookupError（幂等）。"""
    registry = TaskRegistry()
    # PID 1 (init) 在大多数系统上存在但不可杀 → 用一个确定不存在的极大 PID。
    registry.register_external(pid=2_000_000, agent_id="root", parent_task_id=None)
    # 不抛错。
    await registry.cancel_subtree("root")


async def test_cancel_subtree_idempotent_repeat() -> None:
    """重复 cancel_subtree 安全（幂等）。"""
    registry = TaskRegistry()

    async def _sleeper() -> str:
        await asyncio.sleep(100)
        return "done"

    task = asyncio.create_task(_sleeper())
    registry.register_run(task, agent_id="root", parent_task_id=None)

    await registry.cancel_subtree("root")
    # 第二次不抛错。
    await registry.cancel_subtree("root")
    assert task.done()


# ---------------------------------------------------------------------------
# 审计字段：done callback 回写
# ---------------------------------------------------------------------------


async def test_done_callback_writes_completed_status() -> None:
    """run_task 正常完成 → done callback 回写 status=completed + finished_at。"""
    registry = TaskRegistry()

    async def _quick() -> str:
        return "done"

    task = asyncio.create_task(_quick())
    record = registry.register_run(task, agent_id="root", parent_task_id=None)
    await task
    await asyncio.sleep(0)  # 让 done callback 跑

    assert record.status == "completed"
    assert record.finished_at is not None
    assert record.error_message is None


async def test_done_callback_writes_failed_status() -> None:
    """run_task 抛异常 → done callback 回写 status=failed + error_message。"""
    registry = TaskRegistry()

    async def _boom() -> None:
        raise ValueError("kaboom")

    task = asyncio.create_task(_boom())
    record = registry.register_run(task, agent_id="root", parent_task_id=None)
    with pytest.raises(ValueError):
        await task
    await asyncio.sleep(0)  # 让 done callback 跑

    assert record.status == "failed"
    assert record.finished_at is not None
    assert record.error_message is not None
    assert "ValueError" in record.error_message


async def test_done_callback_writes_cancelled_status() -> None:
    """run_task 被 cancel → done callback 回写 status=cancelled。"""
    registry = TaskRegistry()

    async def _sleeper() -> str:
        await asyncio.sleep(100)
        return "done"

    task = asyncio.create_task(_sleeper())
    record = registry.register_run(task, agent_id="root", parent_task_id=None)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)  # 让 done callback 跑

    assert record.status == "cancelled"
    assert record.finished_at is not None


# ---------------------------------------------------------------------------
# 对抗式审查 P1-1：cancel_subtree 与 done-callback 竞态终态守护
# ---------------------------------------------------------------------------


async def test_cancel_subtree_cancelled_not_overridden_by_done_callback() -> None:
    """cancel_subtree 提前写 cancelled 后，done-callback 不覆盖为 completed（P1-1）。

    竞态场景：cancel_subtree 调 _mark_finished(record, "cancelled") + task.cancel()，
    但 task 在 cancel 生效前已正常完成（task.cancelled()=False, exception=None）。
    无终态守护时 done-callback 走 else 分支把 status 重写成 completed；守护后
    应保留 cancel_subtree 写的 cancelled。
    """
    registry = TaskRegistry()

    async def _quick() -> str:
        return "done"

    task = asyncio.create_task(_quick())
    record = registry.register_run(task, agent_id="root", parent_task_id=None)
    # 模拟 cancel_subtree：task 正常完成前手写 cancelled 终态（_mark_finished 语义）。
    # 用 _mark_finished 保持与生产一致（终态幂等跳过）。
    from application.agents.registry import _mark_finished

    _mark_finished(record, status="cancelled")
    assert record.status == "cancelled"

    await task  # task 正常完成（非 cancelled）
    await asyncio.sleep(0)  # 让 done callback 跑

    # 终态守护：done-callback 不应把 cancelled 覆盖为 completed。
    assert record.status == "cancelled", f"done-callback overrode cancelled; got {record.status}"
    assert record.finished_at is not None


# ---------------------------------------------------------------------------
# close_cell 签名（task-5 正式启用，本 task 先验证签名可用）
# ---------------------------------------------------------------------------


async def test_close_cell_idempotent() -> None:
    """close_cell 注销 agent，幂等（重复调用安全）。"""
    registry = TaskRegistry()
    registry.close_cell("root")
    # 已注销 agent → no_live_descendants 快速返回 True。
    assert registry.no_live_descendants("root") is True
    # 重复注销不抛错。
    registry.close_cell("root")


# ---------------------------------------------------------------------------
# register 后 task_id 唯一性
# ---------------------------------------------------------------------------


async def test_register_generates_unique_task_ids() -> None:
    """多次 register 生成不同 task_id。"""
    registry = TaskRegistry()
    r1 = registry.register_external(pid=1, agent_id="a", parent_task_id=None)
    r2 = registry.register_external(pid=2, agent_id="a", parent_task_id=None)
    assert r1.task_id != r2.task_id


# ---------------------------------------------------------------------------
# 多 external PID 同 agent
# ---------------------------------------------------------------------------


async def test_cancel_subtree_multiple_external_pids() -> None:
    """同 agent 多个 external PID，cancel_subtree 全部 kill（后序）。"""
    registry = TaskRegistry()
    registry.register_external(pid=2_000_001, agent_id="root", parent_task_id=None)
    registry.register_external(pid=2_000_002, agent_id="root", parent_task_id=None)

    kill_calls: list[int] = []
    real_os_kill = os.kill

    def _spy_kill(pid: int, sig: int) -> None:
        kill_calls.append(pid)
        try:
            real_os_kill(pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass

    with patch("application.agents.registry.os.kill", side_effect=_spy_kill):
        await registry.cancel_subtree("root")

    # 两个 PID 都被 kill（每个 PID 会有 SIGTERM + 可能 SIGKILL）。
    assert 2_000_001 in kill_calls
    assert 2_000_002 in kill_calls


# ---------------------------------------------------------------------------
# nightly-p1 SC_12 / SC_13：稳定投影、终态幂等与 live-safe retention
# ---------------------------------------------------------------------------


def test_workflow_task_projection_keeps_identity_across_terminal_transition() -> None:
    """同一 workflow child 的 running/completed 投影共用冻结 identity。"""
    registry = TaskRegistry()
    record = registry.register_pending(
        agent_id="child-agent",
        parent_task_id="parent-task",
        thread_id="thread-abc123abc123",
        source="workflow",
        workflow_id="wf-1",
        workflow_task_id="logical-task-1",
        task_run_id="run-1",
        task_name="Child Review",
        session_id="subagent-thread-abc123abc123-wf-1-run-1",
    )

    running = registry.list_thread_tasks(
        "thread-abc123abc123",
        include_finished=True,
        limit=10,
    )[0]
    assert running.task_id == record.task_id
    assert running.agent_id == "child-agent"
    assert running.status == "pending"

    assert registry.finish_task(record.task_id, status="completed") is True
    completed = registry.list_thread_tasks(
        "thread-abc123abc123",
        include_finished=True,
        limit=10,
    )[0]

    assert completed.task_id == running.task_id
    assert completed.agent_id == running.agent_id
    assert completed.session_id == running.session_id
    assert completed.workflow_id == running.workflow_id
    assert completed.started_at == running.started_at
    assert completed.status == "completed"
    assert completed.finished_at is not None
    first_finished_at = completed.finished_at

    assert registry.finish_task(record.task_id, status="failed", error_message="late") is False
    repeated = registry.list_thread_tasks(
        "thread-abc123abc123",
        include_finished=True,
        limit=10,
    )[0]
    assert repeated.status == "completed"
    assert repeated.finished_at == first_finished_at
    assert repeated.error_message is None


async def test_retention_prunes_only_oldest_terminal_records() -> None:
    """A 溢出只裁剪 A 最旧终态；B 与 A 的 pending/running live 全量保留。"""
    registry = TaskRegistry(max_terminal_records=200)
    thread_id = "thread-abc123abc123"
    other_thread_id = "thread-def456def456"
    pending = registry.register_pending(
        agent_id="live-pending",
        parent_task_id=None,
        thread_id=thread_id,
        source="workflow",
        workflow_id="wf-live",
        workflow_task_id="live-pending",
        task_run_id="live-pending-run",
        task_name="Live pending",
        session_id="live-pending-session",
    )
    running_handle = asyncio.create_task(asyncio.sleep(30.0))
    running = registry.register_run(
        running_handle,
        agent_id="live-running",
        parent_task_id=None,
        thread_id=thread_id,
        source="workflow",
        workflow_id="wf-live",
        workflow_task_id="live-running",
        task_run_id="live-running-run",
        task_name="Live running",
        session_id="live-running-session",
    )
    live_ids = {pending.task_id, running.task_id}
    other_terminal_ids: set[str] = set()
    for index in range(3):
        other = registry.register_pending(
            agent_id=f"other-{index}",
            parent_task_id=None,
            thread_id=other_thread_id,
            source="workflow",
            workflow_id="wf-other",
            workflow_task_id=f"other-task-{index}",
            task_run_id=f"other-run-{index}",
            task_name=f"Other {index}",
            session_id=f"other-session-{index}",
        )
        other_terminal_ids.add(other.task_id)
        assert registry.finish_task(other.task_id, status="failed") is True
    terminal_ids: list[str] = []
    for index in range(205):
        record = registry.register_pending(
            agent_id=f"done-{index}",
            parent_task_id=None,
            thread_id=thread_id,
            source="workflow",
            workflow_id="wf-done",
            workflow_task_id=f"done-task-{index}",
            task_run_id=f"done-run-{index}",
            task_name=f"Done {index}",
            session_id=f"done-session-{index}",
        )
        terminal_ids.append(record.task_id)
        assert registry.finish_task(record.task_id, status="completed") is True

    all_records = registry.list_thread_tasks(
        thread_id,
        include_finished=True,
        limit=500,
    )
    returned_ids = {record.task_id for record in all_records}
    returned_terminal = [record for record in all_records if record.status == "completed"]

    assert live_ids <= returned_ids
    assert len(returned_terminal) == 200
    assert set(terminal_ids[:5]).isdisjoint(returned_ids)
    assert set(terminal_ids[5:]) <= returned_ids
    assert {
        record.task_id
        for record in registry.list_thread_tasks(
            thread_id,
            include_finished=False,
            limit=200,
        )
    } == live_ids
    assert {
        record.task_id
        for record in registry.list_thread_tasks(
            other_thread_id,
            include_finished=True,
            limit=20,
        )
    } == other_terminal_ids
    running_handle.cancel()
    await asyncio.gather(running_handle, return_exceptions=True)
