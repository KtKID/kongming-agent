"""ScheduleTool 单元测试。

覆盖 6 个 action（create/list/pause/resume/run_now/remove）的正常+异常路径
+ runtime_factory 注入闭环。

测试约束：
- 使用 ``tmp_path`` 隔离 cron home
- 不调真 LLM；run_now 测试用 stub bridge / runtime
- 不依赖 :mod:`safety.*`
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from core.contracts import ToolContext, ToolResult
from scheduler.domain import (
    DueTaskReservation,
    RunStatus,
    ScheduledRun,
    ScheduledRunSubmitDisposition,
    ScheduledRunSubmitReceipt,
    TaskLifecycleState,
    TriggerType,
)
from scheduler.store import Store
from scheduler.timing import parse_iso, to_iso, utc_now
from tests.support.tool_calls import execute_prepared_tool
from tools.builtin.schedule_tool import ScheduleTool, build_schedule_tool


def _ctx() -> ToolContext:
    return ToolContext(run_id="run-test", session_id="sess-test", turn=1, call_id="call-1")


def _thread_ctx() -> ToolContext:
    return ToolContext(
        run_id="run-thread",
        session_id="thread-cc29272b9a9b",
        turn=1,
        call_id="call-thread",
    )


def _make_tool(tmp_path: Path) -> tuple[ScheduleTool, Store]:
    store = Store(home_dir=tmp_path / "cron")
    tool = build_schedule_tool(store, thread_provisioner=_FakeThreadProvisioner())
    return tool, store


class _FakeThreadProvisioner:
    """测试用 thread provisioner：给 schedule tool 注入专属 thread 创建能力。"""

    def __init__(self, thread_id: str = "thread-dddddddddddd") -> None:
        self.thread_id = thread_id
        self.created: list[dict[str, str]] = []
        self.deleted: list[str] = []

    async def create_scheduled_task_thread(
        self,
        *,
        task_id: str,
        name: str,
        preset_id: str,
        cwd: str = "",
    ) -> str:
        self.created.append({"task_id": task_id, "name": name, "preset_id": preset_id, "cwd": cwd})
        return self.thread_id

    async def delete_thread(self, thread_id: str, *, keep_history: bool = False) -> None:
        self.deleted.append(f"{thread_id}:{keep_history}")


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


class TestScheduleToolCreate:
    async def test_create_requires_thread_provisioner(self, tmp_path: Path) -> None:
        store = Store(home_dir=tmp_path / "cron")
        tool = build_schedule_tool(store)

        result = await execute_prepared_tool(
            tool,
            {
                "action": "create",
                "name": "missing provisioner",
                "schedule": "every 10s",
                "input": "x",
            },
            _ctx(),
        )

        assert result.ok is False
        assert result.error_message == "thread_provisioner_required"
        assert store.list_tasks() == []

    async def test_create_every_seconds_succeeds(self, tmp_path: Path) -> None:
        tool, store = _make_tool(tmp_path)
        r: ToolResult = await execute_prepared_tool(
            tool,
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
        assert task.lifecycle is TaskLifecycleState.SCHEDULED
        # audit 落了 create
        audits = store.list_audits(task_id=task_id)
        actions = [a["action"] for a in audits]
        assert "create" in actions

    async def test_create_iso8601_one_shot_sets_next_run(self, tmp_path: Path) -> None:
        tool, _store = _make_tool(tmp_path)
        r = await execute_prepared_tool(
            tool,
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

    async def test_create_cron_persists_default_timezone_and_thread_target(
        self, tmp_path: Path
    ) -> None:
        store = Store(home_dir=tmp_path / "cron")
        provisioner = _FakeThreadProvisioner("thread-eeeeeeeeeeee")
        tool = build_schedule_tool(
            store,
            default_timezone="Asia/Shanghai",
            default_delivery_channel="web",
            default_preset_id="preset-default",
            thread_provisioner=provisioner,
        )

        result = await execute_prepared_tool(
            tool,
            {
                "action": "create",
                "name": "daily-1012",
                "schedule": "12 10 * * *",
                "agent": "default",
                "input": "create 1012.txt if missing",
            },
            _thread_ctx(),
        )

        assert result.ok is True
        assert result.data is not None

        task_id = result.data["task_id"]
        task = store.get_task(task_id)
        assert task is not None
        assert task.trigger.trigger_type is TriggerType.CRON
        assert task.trigger.expr == "12 10 * * *"
        assert task.trigger.timezone == "Asia/Shanghai"
        assert task.delivery is not None
        assert task.delivery.channel.value == "web"
        assert task.delivery.target == "thread:thread-eeeeeeeeeeee"
        assert task.thread_id == "thread-eeeeeeeeeeee"
        assert result.data["thread_id"] == "thread-eeeeeeeeeeee"
        assert task.created_by == "agent:thread-cc29272b9a9b"
        assert provisioner.created[0]["task_id"] == task_id
        assert provisioner.created[0]["preset_id"] == "preset-default"

        raw = json.loads((tmp_path / "cron" / "scheduled_tasks.json").read_text(encoding="utf-8"))
        persisted = raw["tasks"][0]
        assert persisted["task_id"] == task_id
        assert persisted["trigger"]["trigger_type"] == "cron"
        assert persisted["trigger"]["expr"] == "12 10 * * *"
        assert persisted["trigger"]["timezone"] == "Asia/Shanghai"
        assert persisted["delivery"]["channel"] == "web"
        assert persisted["delivery"]["target"] == "thread:thread-eeeeeeeeeeee"
        assert persisted["created_by"] == "agent:thread-cc29272b9a9b"
        assert persisted["thread_id"] == "thread-eeeeeeeeeeee"

    async def test_create_once_in_past_returns_error_and_persists_nothing(
        self, tmp_path: Path
    ) -> None:
        tool, store = _make_tool(tmp_path)
        past_schedule = to_iso(utc_now() - timedelta(minutes=10))

        result = await execute_prepared_tool(
            tool,
            {
                "action": "create",
                "name": "past-once",
                "schedule": past_schedule,
                "input": "x",
            },
            _ctx(),
        )

        assert result.ok is False
        assert result.error_message == "schedule_in_past"
        assert store.list_tasks() == []

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
            r = await execute_prepared_tool(
                tool,
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
        r = await execute_prepared_tool(
            tool,
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
        r = await execute_prepared_tool(
            tool,
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
        r = await execute_prepared_tool(
            tool,
            {"action": "create", "schedule": "every 10s", "input": "x"},
            _ctx(),
        )
        assert r.ok is False
        assert r.error_message is not None
        assert "name" in r.error_message

    async def test_create_missing_schedule_returns_error(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        r = await execute_prepared_tool(
            tool,
            {"action": "create", "name": "n", "input": "x"},
            _ctx(),
        )
        assert r.ok is False
        assert "schedule" in (r.error_message or "")

    async def test_create_missing_input_returns_error(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        r = await execute_prepared_tool(
            tool,
            {"action": "create", "name": "n", "schedule": "every 10s"},
            _ctx(),
        )
        assert r.ok is False
        assert "input" in (r.error_message or "")

    async def test_create_invalid_concurrency_returns_error(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        r = await execute_prepared_tool(
            tool,
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
        r = await execute_prepared_tool(tool, {"action": "list"}, _ctx())
        assert r.ok is True
        assert r.content == "(no tasks)"
        assert r.data is not None
        assert r.data["count"] == 0

    async def test_list_returns_tasks(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        await execute_prepared_tool(
            tool,
            {"action": "create", "name": "t1", "schedule": "every 10s", "input": "x"},
            _ctx(),
        )
        await execute_prepared_tool(
            tool,
            {"action": "create", "name": "t2", "schedule": "every 30s", "input": "y"},
            _ctx(),
        )
        r = await execute_prepared_tool(tool, {"action": "list"}, _ctx())
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
        create = await execute_prepared_tool(
            tool,
            {"action": "create", "name": "p", "schedule": "every 10s", "input": "x"},
            _ctx(),
        )
        task_id = create.data["task_id"]

        r_pause = await execute_prepared_tool(tool, {"action": "pause", "task_id": task_id}, _ctx())
        assert r_pause.ok is True
        task = store.get_task(task_id)
        assert task is not None
        assert task.lifecycle is TaskLifecycleState.PAUSED
        # audit 落了 pause
        actions = [a["action"] for a in store.list_audits(task_id=task_id)]
        assert "pause" in actions

        r_resume = await execute_prepared_tool(
            tool, {"action": "resume", "task_id": task_id}, _ctx()
        )
        assert r_resume.ok is True
        task = store.get_task(task_id)
        assert task is not None
        assert task.lifecycle is TaskLifecycleState.SCHEDULED
        actions = [a["action"] for a in store.list_audits(task_id=task_id)]
        assert "resume" in actions

    async def test_pause_unknown_task(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        r = await execute_prepared_tool(tool, {"action": "pause", "task_id": "task-nosuch"}, _ctx())
        assert r.ok is False
        assert r.error_message == "task_not_found"

    async def test_resume_unknown_task(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        r = await execute_prepared_tool(
            tool, {"action": "resume", "task_id": "task-nosuch"}, _ctx()
        )
        assert r.ok is False
        assert r.error_message == "task_not_found"


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


class TestScheduleToolRemove:
    async def test_remove_existing(self, tmp_path: Path) -> None:
        tool, store = _make_tool(tmp_path)
        create = await execute_prepared_tool(
            tool,
            {"action": "create", "name": "to-rm", "schedule": "every 10s", "input": "x"},
            _ctx(),
        )
        task_id = create.data["task_id"]

        r = await execute_prepared_tool(tool, {"action": "remove", "task_id": task_id}, _ctx())
        assert r.ok is True
        assert store.get_task(task_id) is None
        actions = [a["action"] for a in store.list_audits(task_id=task_id)]
        assert "remove" in actions

    async def test_remove_unknown(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        r = await execute_prepared_tool(
            tool, {"action": "remove", "task_id": "task-nosuch"}, _ctx()
        )
        assert r.ok is False
        assert r.error_message == "task_not_found"


# ---------------------------------------------------------------------------
# run_now
# ---------------------------------------------------------------------------


class _StubScheduledRunManager:
    """run_now 测试用：捕获 reservation 并合成 COMPLETED ScheduledRun。"""

    def __init__(self) -> None:
        self.reservations: list[DueTaskReservation] = []
        self.runs: dict[str, ScheduledRun] = {}
        self.aclose_called = False

    async def submit_scheduled_run(
        self,
        reservation: DueTaskReservation,
    ) -> ScheduledRunSubmitReceipt:
        self.reservations.append(reservation)
        now = to_iso(utc_now())
        run = ScheduledRun(
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
        self.runs[run.run_id] = run
        return ScheduledRunSubmitReceipt(
            reservation_id=reservation.reservation_id,
            task_id=reservation.task.task_id,
            run_id=run.run_id,
            session_id=run.session_id or "",
            thread_id=reservation.task.thread_id,
            disposition=ScheduledRunSubmitDisposition.ACCEPTED,
        )

    async def wait_for_run(self, run_id: str) -> ScheduledRun:
        return self.runs[run_id]

    async def aclose(self) -> None:
        self.aclose_called = True


class TestScheduleToolRunNow:
    async def test_run_now_without_factory_returns_error(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        # 先 create 拿到一个 task_id
        create = await execute_prepared_tool(
            tool,
            {"action": "create", "name": "n", "schedule": "every 10s", "input": "x"},
            _ctx(),
        )
        task_id = create.data["task_id"]

        r = await execute_prepared_tool(tool, {"action": "run_now", "task_id": task_id}, _ctx())
        assert r.ok is False
        assert r.error_message == "runtime_factory_missing"

    async def test_run_now_unknown_task(self, tmp_path: Path) -> None:
        store = Store(home_dir=tmp_path / "cron")

        def factory(_: Store) -> _StubScheduledRunManager:
            return _StubScheduledRunManager()

        tool = build_schedule_tool(
            store,
            runtime_factory_fn=factory,
            thread_provisioner=_FakeThreadProvisioner(),
        )
        r = await execute_prepared_tool(
            tool, {"action": "run_now", "task_id": "task-nosuch"}, _ctx()
        )
        assert r.ok is False
        assert r.error_message == "task_not_found"

    async def test_run_now_invokes_shared_scheduled_run_manager(self, tmp_path: Path) -> None:
        store = Store(home_dir=tmp_path / "cron")
        stub_manager = _StubScheduledRunManager()

        def factory(s: Store) -> _StubScheduledRunManager:
            assert s is store
            return stub_manager

        tool = build_schedule_tool(
            store,
            runtime_factory_fn=factory,
            thread_provisioner=_FakeThreadProvisioner(),
        )
        # 先 create
        create = await execute_prepared_tool(
            tool,
            {"action": "create", "name": "n", "schedule": "every 10s", "input": "x"},
            _ctx(),
        )
        task_id = create.data["task_id"]

        r = await execute_prepared_tool(tool, {"action": "run_now", "task_id": task_id}, _ctx())
        assert r.ok is True
        assert r.data is not None
        assert r.data["status"] == RunStatus.COMPLETED.value
        assert r.data["task_id"] == task_id
        assert r.data["run_id"].startswith("run-stub-")
        assert len(stub_manager.reservations) == 1
        assert stub_manager.reservations[0].task.task_id == task_id
        assert stub_manager.aclose_called is False
        # audit 落 run_now
        actions = [a["action"] for a in store.list_audits(task_id=task_id)]
        assert "run_now" in actions

    async def test_run_now_surfaces_shared_manager_submit_error(self, tmp_path: Path) -> None:
        """共享 manager submit 抛错时 ToolResult 返回稳定错误。"""
        store = Store(home_dir=tmp_path / "cron")

        class _ExplodingManager:
            async def submit_scheduled_run(
                self,
                reservation: DueTaskReservation,
            ) -> ScheduledRunSubmitReceipt:
                del reservation
                raise RuntimeError("bridge boom")

            async def wait_for_run(self, run_id: str) -> ScheduledRun:
                raise AssertionError(f"unexpected wait: {run_id}")

            async def aclose(self) -> None:
                return None

        def factory(_: Store) -> _ExplodingManager:
            return _ExplodingManager()

        tool = build_schedule_tool(
            store,
            runtime_factory_fn=factory,
            thread_provisioner=_FakeThreadProvisioner(),
        )
        create = await execute_prepared_tool(
            tool,
            {"action": "create", "name": "n", "schedule": "every 10s", "input": "x"},
            _ctx(),
        )
        task_id = create.data["task_id"]

        r = await execute_prepared_tool(tool, {"action": "run_now", "task_id": task_id}, _ctx())
        assert r.ok is False
        assert r.error_message is not None
        assert "bridge boom" in r.error_message


# ---------------------------------------------------------------------------
# unknown action / 防御
# ---------------------------------------------------------------------------


class TestScheduleToolGuards:
    async def test_unknown_action(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        r = await execute_prepared_tool(tool, {"action": "fly_to_moon"}, _ctx())
        assert r.ok is False
        assert r.error_message == "invalid action"

    async def test_missing_action(self, tmp_path: Path) -> None:
        tool, _ = _make_tool(tmp_path)
        r = await execute_prepared_tool(tool, {}, _ctx())
        assert r.ok is False
        assert r.error_message == "missing required args: ['action']"


# ---------------------------------------------------------------------------
# cron run 隔离：单字 "schedule" 在 execution_bridge 工具裁剪中被裁
# ---------------------------------------------------------------------------


def test_schedule_tool_name_is_disallowed_in_cron_run() -> None:
    """ScheduleTool.name == 'schedule'；execution_bridge 应当把它裁掉，
    否则 cron run 内会递归创建任务。
    """
    from application.scheduled_runs.execution_bridge import _is_disallowed_tool_name

    assert _is_disallowed_tool_name(ScheduleTool.name) is True
    assert _is_disallowed_tool_name("cron") is True
    assert _is_disallowed_tool_name("schedule.create") is True
    assert _is_disallowed_tool_name("cron.list") is True
    # 正常工具不被裁
    assert _is_disallowed_tool_name("shell") is False
    assert _is_disallowed_tool_name("memory") is False
