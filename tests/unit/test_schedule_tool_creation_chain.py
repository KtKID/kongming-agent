from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from core.contracts import ToolContext
from scheduler.domain import TriggerType
from scheduler.store import Store
from scheduler.timing import to_iso, utc_now
from tools.schedule_tool import build_schedule_tool


def _ctx() -> ToolContext:
    return ToolContext(run_id="run-test", session_id="sess-test", turn=1, call_id="call-1")


def _thread_ctx() -> ToolContext:
    return ToolContext(
        run_id="run-thread",
        session_id="thread-cc29272b9a9b",
        turn=1,
        call_id="call-thread",
    )


async def test_create_cron_persists_default_timezone_and_thread_target(tmp_path: Path) -> None:
    store = Store(home_dir=tmp_path / "cron")
    tool = build_schedule_tool(
        store,
        default_timezone="Asia/Shanghai",
        default_delivery_channel="web",
    )

    result = await tool.execute(
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
    assert task.delivery.target == "thread:thread-cc29272b9a9b"
    assert task.created_by == "agent:thread-cc29272b9a9b"

    raw = json.loads((tmp_path / "cron" / "scheduled_tasks.json").read_text(encoding="utf-8"))
    persisted = raw["tasks"][0]
    assert persisted["task_id"] == task_id
    assert persisted["trigger"]["trigger_type"] == "cron"
    assert persisted["trigger"]["expr"] == "12 10 * * *"
    assert persisted["trigger"]["timezone"] == "Asia/Shanghai"
    assert persisted["delivery"]["channel"] == "web"
    assert persisted["delivery"]["target"] == "thread:thread-cc29272b9a9b"
    assert persisted["created_by"] == "agent:thread-cc29272b9a9b"


async def test_create_once_in_past_returns_error_and_persists_nothing(tmp_path: Path) -> None:
    store = Store(home_dir=tmp_path / "cron")
    tool = build_schedule_tool(store)
    past_schedule = to_iso(utc_now() - timedelta(minutes=10))

    result = await tool.execute(
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
