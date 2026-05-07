"""ScheduleTool 单元测试。

覆盖 6 个 action（create/list/pause/resume/run_now/remove）的正常+异常路径
+ runtime_factory 注入闭环。

测试约束：
- 使用 ``tmp_path`` 隔离 cron home
- 不调真 LLM；run_now 测试用 stub bridge / runtime
- 不依赖 :mod:`safety.*`
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.contracts import ToolContext, ToolResult
from scheduler.domain import (
    DueTaskReservation,
    RunStatus,
    ScheduledRun,
    TaskState,
    TriggerType,
)
from scheduler.store import Store
from scheduler.timing import parse_iso, to_iso, utc_now
from tools.schedule_tool import ScheduleTool, build_schedule_tool


def _ctx() -> ToolContext:
    return ToolContext(run_id="run-test", session_id="sess-test", turn=1, call_id="call-1")


def _make_tool(tmp_path: Path) -> tuple[ScheduleTool, Store]:
    store = Store(home_dir=tmp_path / "cron")
    tool = build_schedule_tool(store)
    return tool, store


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


class TestScheduleToolCreate:
    async def test_create_every_seconds_succeeds(self, tmp_path: Path) -> None:
        tool, store = _make_tool(tmp_path)
        r: ToolResult = await tool.execute(
            {
                "action": "create",
                "name": "drink water",
                "schedule": "every 10s",
                "agent": "default",
                "input": "remind me to drink water",
            },
            _ctx(),
        )
        assert r.ok is True
        assert r.data is not None
        task_id = r.data["task_id"]
        assert task_id.startswith("task-")
        assert r.data["trigger_type"] == TriggerType.SECONDS.value
        # store 中能读到
        task = store.get_task(task_id)
        assert task is not None
        assert task.name == "drink water"
        assert task.target.agent_name == "default"
        assert task.enabled is True
        # audit 落了 create
        audits = store.list_audits(task_id=task_id)
        actions = [a["action"] for a in audits]
        assert "create" in actions

    async def test_create_iso8601_one_shot_sets_next_run(self, tmp_path: Path) -> None:
        tool, _store = _make_tool(tmp_path)
        r = await tool.execute(
            {
                "action": "create",
                "name": "future",
                "schedule": "2099-01-02T03:04:05+00:00",
                "input": "future task",
            },
            _ctx(),
        )
        assert r.ok is True
        assert r.data["trigger_type"] == TriggerType.ONCE.value
        assert r.data["next_run_at"] is not None  # ONCE 写入 next_run_at

    async def test_create_recurring_task_has_next_run_at_set(self, tmp_path: Path) -> None:
        """v0.2.1 P0 修复回归：recurring 任务（INTERVAL/CRON/SECONDS）创建后
        next_run_at 必须被算出，否则 store.reserve_due_tasks 会跳过它，
        任务永远不会触发。

        覆盖四种 schedule 表达式：
        - ``every 30s``：SECONDS
        - ``every 5m``：INTERVAL
        - ``0 9 * * *``：5 字段 cron
        - ``*/30 * * * * *``：6 字段 cron（首字段秒）
        """
        for schedule_expr in ["every 30s", "every 5m", "0 9 * * *", "*/30 * * * * *"]:
            tool, store = _make_tool(tmp_path / schedule_expr.replace(" ", "_").replace("*", "x"))
            r = await tool.execute(
                {
                    "action": "create",
                    "name": f"test {schedule_expr}",
                    "schedule": schedule_expr,
                    "agent": "default",
                    "input": "noop",
                },
                _ctx(),
            )
            assert r.ok, f"create failed for {schedule_expr}: {r.content}"
            assert r.data is not None
            # ToolResult.data 暴露 next_run_at
            assert r.data["next_run_at"] is not None, (
                f"{schedule_expr} 创建后 next_run_at 仍为 None — recurring 任务"
                f"会被 store.reserve_due_tasks 跳过，永不触发"
            )
            # store 中的 task 也得有 next_run_at
            tasks = store.list_tasks()
            assert len(tasks) == 1
            latest = tasks[0]
            assert latest.next_run_at is not None, (
                f"{schedule_expr} store 持久化后 next_run_at 仍为 None"
            )
            # 解析回去确认是合法 ISO8601（带时区）
            parsed = parse_iso(latest.next_run_at)
            assert parsed.tzinfo is not None

    async def test_create_invalid_cron_returns_error(self, tmp_path: Path) -> None:
        """parse_schedule 通过但 compute_first_run_at 失败时（极少数边界 cron）
        应返回结构化错误而不是抛异常。"""
        tool, _ = _make_tool(tmp_path)
        # parse_schedule 接受 5 字段；用 croniter 拒绝的字段值（month=13）
        r = await tool.execute(
            {
                "action": "create",
                "name": "bad-cron",
                "schedule": "0 9 1 13 *",
                "input": "x",
            },
            _ctx(),
        )
        # parse 阶段 / compute 阶段任一失败都应进入 ok=False，不抛
        assert r.ok is False
        assert r.error_message is not None

    async def test_create_invalid_schedule_returns_error(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        r = await tool.execute(
            {
                "action": "create",
                "name": "bad",
                "schedule": "foobar",
                "input": "x",
            },
            _ctx(),
        )
        assert r.ok is False
        assert r.error_message is not None
        assert "unrecognized" in r.error_message or "invalid" in r.error_message

    async def test_create_missing_name_returns_error(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        r = await tool.execute(
            {"action": "create", "schedule": "every 10s", "input": "x"},
            _ctx(),
        )
        assert r.ok is False
        assert r.error_message is not None
        assert "name" in r.error_message

    async def test_create_missing_schedule_returns_error(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        r = await tool.execute(
            {"action": "create", "name": "n", "input": "x"},
            _ctx(),
        )
        assert r.ok is False
        assert "schedule" in (r.error_message or "")

    async def test_create_missing_input_returns_error(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        r = await tool.execute(
            {"action": "create", "name": "n", "schedule": "every 10s"},
            _ctx(),
        )
        assert r.ok is False
        assert "input" in (r.error_message or "")

    async def test_create_invalid_concurrency_returns_error(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        r = await tool.execute(
            {
                "action": "create",
                "name": "n",
                "schedule": "every 10s",
                "input": "x",
                "concurrency": "bogus",
            },
            _ctx(),
        )
        assert r.ok is False
        assert "concurrency" in (r.error_message or "")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestScheduleToolList:
    async def test_list_empty(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        r = await tool.execute({"action": "list"}, _ctx())
        assert r.ok is True
        assert r.content == "(no tasks)"
        assert r.data is not None
        assert r.data["count"] == 0

    async def test_list_returns_tasks(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        await tool.execute(
            {"action": "create", "name": "t1", "schedule": "every 10s", "input": "x"},
            _ctx(),
        )
        await tool.execute(
            {"action": "create", "name": "t2", "schedule": "every 30s", "input": "y"},
            _ctx(),
        )
        r = await tool.execute({"action": "list"}, _ctx())
        assert r.ok is True
        assert r.data is not None
        assert r.data["count"] == 2
        names = {t["name"] for t in r.data["tasks"]}
        assert names == {"t1", "t2"}
        # content 文本含 task_id 前缀
        assert "task-" in r.content


# ---------------------------------------------------------------------------
# pause / resume
# ---------------------------------------------------------------------------


class TestScheduleToolPauseResume:
    async def test_pause_then_resume(self, tmp_path: Path) -> None:
        tool, store = _make_tool(tmp_path)
        create = await tool.execute(
            {"action": "create", "name": "p", "schedule": "every 10s", "input": "x"},
            _ctx(),
        )
        task_id = create.data["task_id"]

        r_pause = await tool.execute({"action": "pause", "task_id": task_id}, _ctx())
        assert r_pause.ok is True
        task = store.get_task(task_id)
        assert task is not None
        assert task.enabled is False
        assert task.state is TaskState.PAUSED
        # audit 落了 pause
        actions = [a["action"] for a in store.list_audits(task_id=task_id)]
        assert "pause" in actions

        r_resume = await tool.execute({"action": "resume", "task_id": task_id}, _ctx())
        assert r_resume.ok is True
        task = store.get_task(task_id)
        assert task is not None
        assert task.enabled is True
        assert task.state is TaskState.SCHEDULED
        actions = [a["action"] for a in store.list_audits(task_id=task_id)]
        assert "resume" in actions

    async def test_pause_unknown_task(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        r = await tool.execute({"action": "pause", "task_id": "task-nosuch"}, _ctx())
        assert r.ok is False
        assert r.error_message == "task_not_found"

    async def test_resume_unknown_task(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        r = await tool.execute({"action": "resume", "task_id": "task-nosuch"}, _ctx())
        assert r.ok is False
        assert r.error_message == "task_not_found"


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


class TestScheduleToolRemove:
    async def test_remove_existing(self, tmp_path: Path) -> None:
        tool, store = _make_tool(tmp_path)
        create = await tool.execute(
            {"action": "create", "name": "to-rm", "schedule": "every 10s", "input": "x"},
            _ctx(),
        )
        task_id = create.data["task_id"]

        r = await tool.execute({"action": "remove", "task_id": task_id}, _ctx())
        assert r.ok is True
        assert store.get_task(task_id) is None
        actions = [a["action"] for a in store.list_audits(task_id=task_id)]
        assert "remove" in actions

    async def test_remove_unknown(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        r = await tool.execute({"action": "remove", "task_id": "task-nosuch"}, _ctx())
        assert r.ok is False
        assert r.error_message == "task_not_found"


# ---------------------------------------------------------------------------
# run_now
# ---------------------------------------------------------------------------


class _StubBridge:
    """run_now 测试用：捕获 reservation 并合成 COMPLETED ScheduledRun。"""

    def __init__(self) -> None:
        self.reservations: list[DueTaskReservation] = []

    async def execute(self, reservation: DueTaskReservation) -> ScheduledRun:
        self.reservations.append(reservation)
        now = to_iso(utc_now())
        return ScheduledRun(
            run_id=f"run-stub-{reservation.task.task_id}",
            task_id=reservation.task.task_id,
            status=RunStatus.COMPLETED,
            scheduled_for=reservation.scheduled_for,
            started_at=now,
            finished_at=now,
            session_id="stub-sess",
            result_status="completed",
            final_message_excerpt="ok",
            error_message=None,
            failure_reason=None,
            delivery_error=None,
            silent_suppressed=False,
        )


class _StubRuntime:
    """run_now 测试用：仅暴露 aclose。"""

    def __init__(self) -> None:
        self.aclose_called = False

    async def aclose(self) -> None:
        self.aclose_called = True


class TestScheduleToolRunNow:
    async def test_run_now_without_factory_returns_error(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        # 先 create 拿到一个 task_id
        create = await tool.execute(
            {"action": "create", "name": "n", "schedule": "every 10s", "input": "x"},
            _ctx(),
        )
        task_id = create.data["task_id"]

        r = await tool.execute({"action": "run_now", "task_id": task_id}, _ctx())
        assert r.ok is False
        assert r.error_message == "runtime_factory_missing"

    async def test_run_now_unknown_task(self, tmp_path: Path) -> None:
        store = Store(home_dir=tmp_path / "cron")

        def factory(_: Store) -> tuple[Any, Any]:
            return _StubRuntime(), _StubBridge()

        tool = build_schedule_tool(store, runtime_factory_fn=factory)
        r = await tool.execute({"action": "run_now", "task_id": "task-nosuch"}, _ctx())
        assert r.ok is False
        assert r.error_message == "task_not_found"

    async def test_run_now_invokes_bridge_and_closes_runtime(self, tmp_path: Path) -> None:
        store = Store(home_dir=tmp_path / "cron")
        stub_bridge = _StubBridge()
        stub_runtime = _StubRuntime()

        def factory(s: Store) -> tuple[Any, Any]:
            assert s is store
            return stub_runtime, stub_bridge

        tool = build_schedule_tool(store, runtime_factory_fn=factory)
        # 先 create
        create = await tool.execute(
            {"action": "create", "name": "n", "schedule": "every 10s", "input": "x"},
            _ctx(),
        )
        task_id = create.data["task_id"]

        r = await tool.execute({"action": "run_now", "task_id": task_id}, _ctx())
        assert r.ok is True
        assert r.data is not None
        assert r.data["status"] == RunStatus.COMPLETED.value
        assert r.data["task_id"] == task_id
        assert r.data["run_id"].startswith("run-stub-")
        # bridge.execute 被调一次
        assert len(stub_bridge.reservations) == 1
        assert stub_bridge.reservations[0].task.task_id == task_id
        # runtime.aclose 被调
        assert stub_runtime.aclose_called is True
        # audit 落 run_now
        actions = [a["action"] for a in store.list_audits(task_id=task_id)]
        assert "run_now" in actions

    async def test_run_now_closes_runtime_on_bridge_error(self, tmp_path: Path) -> None:
        """bridge.execute 抛错时 runtime.aclose 仍然被调。"""
        store = Store(home_dir=tmp_path / "cron")
        stub_runtime = _StubRuntime()

        class _ExplodingBridge:
            async def execute(self, reservation: DueTaskReservation) -> ScheduledRun:
                raise RuntimeError("bridge boom")

        def factory(_: Store) -> tuple[Any, Any]:
            return stub_runtime, _ExplodingBridge()

        tool = build_schedule_tool(store, runtime_factory_fn=factory)
        create = await tool.execute(
            {"action": "create", "name": "n", "schedule": "every 10s", "input": "x"},
            _ctx(),
        )
        task_id = create.data["task_id"]

        r = await tool.execute({"action": "run_now", "task_id": task_id}, _ctx())
        assert r.ok is False
        assert r.error_message is not None
        assert "bridge boom" in r.error_message
        # 即便失败 aclose 仍走 finally
        assert stub_runtime.aclose_called is True


# ---------------------------------------------------------------------------
# unknown action / 防御
# ---------------------------------------------------------------------------


class TestScheduleToolGuards:
    async def test_unknown_action(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        r = await tool.execute({"action": "fly_to_moon"}, _ctx())
        assert r.ok is False
        assert r.error_message == "invalid action"

    async def test_missing_action(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        r = await tool.execute({}, _ctx())
        assert r.ok is False
        assert r.error_message == "missing action"


# ---------------------------------------------------------------------------
# cron run 隔离：单字 "schedule" 在 execution_bridge 工具裁剪中被裁
# ---------------------------------------------------------------------------


def test_schedule_tool_name_is_disallowed_in_cron_run() -> None:
    """ScheduleTool.name == 'schedule'；execution_bridge 应当把它裁掉，
    否则 cron run 内会递归创建任务。
    """
    from scheduler.execution_bridge import _is_disallowed_tool_name

    assert _is_disallowed_tool_name(ScheduleTool.name) is True
    assert _is_disallowed_tool_name("cron") is True
    assert _is_disallowed_tool_name("schedule.create") is True
    assert _is_disallowed_tool_name("cron.list") is True
    # 正常工具不被裁
    assert _is_disallowed_tool_name("shell") is False
    assert _is_disallowed_tool_name("memory") is False
