"""Sub-agent lifecycle registry and manager tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from application.subagents.lifecycle import (
    SubAgentLifecycleEvent,
    SubAgentLifecycleRegistry,
)
from application.subagents.manager import SubAgentManager, SubAgentTask
from core.message import Message
from core.result import Result


class _FakeRunner:
    """Fake runner that returns a configured result or raises an exception."""

    def __init__(self, outcome: Result | Exception) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def run(self, *args: Any, **kwargs: Any) -> Result:
        self.calls.append({"args": args, "kwargs": kwargs})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _runtime(outcome: Result | Exception) -> SimpleNamespace:
    return SimpleNamespace(
        session_factory=lambda session_id: SimpleNamespace(session_id=session_id),
        agent_spec=SimpleNamespace(default_model="fake", reasoning_effort=None),
        tools={},
        approval=object(),
        runner=_FakeRunner(outcome),
        llm=object(),
        config=SimpleNamespace(runner=SimpleNamespace(max_turns=8)),
    )


def _task() -> SubAgentTask:
    return SubAgentTask(
        task_id="task-1",
        task_name="Child Review",
        prompt="review it",
        metadata={"task_run_id": "run-1"},
    )


@pytest.mark.asyncio
async def test_subagent_manager_records_started_and_completed() -> None:
    registry = SubAgentLifecycleRegistry()
    events: list[SubAgentLifecycleEvent] = []
    registry.register(events.append)
    manager = SubAgentManager(
        _runtime(
            Result(
                run_id="parent-1",
                session_id="child",
                status="completed",
                final_message=Message.assistant("done"),
                turn_count=1,
            )
        ),
        lifecycle_registry=registry,
    )

    run = await manager.run_task(
        workflow_id="wf-1",
        parent_session_id="thread-abc123abc123",
        task=_task(),
    )

    assert run.status == "completed"
    assert [event.event_type for event in events] == ["started", "completed"]
    records = registry.list_thread("thread-abc123abc123")
    assert len(records) == 1
    record = records[0]
    assert record.thread_id == "thread-abc123abc123"
    assert record.source == "workflow"
    assert record.workflow_id == "wf-1"
    assert record.task_id == "task-1"
    assert record.task_run_id == "run-1"
    assert record.task_name == "Child Review"
    assert record.session_id.startswith("subagent-thread-abc123abc123-wf-1-run-1")
    assert record.status == "completed"
    assert record.started_at <= record.updated_at
    assert record.finished_at == record.updated_at
    assert record.error_message is None


@pytest.mark.asyncio
async def test_subagent_manager_ignores_lifecycle_listener_failure() -> None:
    registry = SubAgentLifecycleRegistry()
    events: list[SubAgentLifecycleEvent] = []

    def _broken_listener(event: SubAgentLifecycleEvent) -> None:
        events.append(event)
        raise RuntimeError("listener exploded")

    registry.register(_broken_listener)
    manager = SubAgentManager(
        _runtime(
            Result(
                run_id="parent-1",
                session_id="child",
                status="completed",
                final_message=Message.assistant("done"),
                turn_count=1,
            )
        ),
        lifecycle_registry=registry,
    )

    run = await manager.run_task(
        workflow_id="wf-1",
        parent_session_id="thread-abc123abc123",
        task=_task(),
    )

    assert run.status == "completed"
    assert [event.event_type for event in events] == ["started", "completed"]
    assert registry.list_thread("thread-abc123abc123")[0].status == "completed"


@pytest.mark.asyncio
async def test_subagent_manager_records_failed_exception() -> None:
    registry = SubAgentLifecycleRegistry()
    events: list[SubAgentLifecycleEvent] = []
    registry.register(events.append)
    manager = SubAgentManager(
        _runtime(RuntimeError("child exploded")),
        lifecycle_registry=registry,
    )

    with pytest.raises(RuntimeError, match="child exploded"):
        await manager.run_task(
            workflow_id="wf-1",
            parent_session_id="thread-abc123abc123",
            task=_task(),
        )

    assert [event.event_type for event in events] == ["started", "failed"]
    records = registry.list_thread("thread-abc123abc123")
    assert len(records) == 1
    record = records[0]
    assert record.status == "failed"
    assert record.error_message == "child exploded"
    assert record.finished_at == record.updated_at


@pytest.mark.asyncio
async def test_subagent_manager_records_setup_failure() -> None:
    registry = SubAgentLifecycleRegistry()
    events: list[SubAgentLifecycleEvent] = []
    registry.register(events.append)
    runtime = _runtime(
        Result(
            run_id="parent-1",
            session_id="child",
            status="completed",
            final_message=Message.assistant("done"),
            turn_count=1,
        )
    )
    manager = SubAgentManager(runtime, lifecycle_registry=registry)

    with pytest.raises(ValueError, match="unknown tool"):
        await manager.run_task(
            workflow_id="wf-1",
            parent_session_id="thread-abc123abc123",
            task=SubAgentTask(
                task_id="task-1",
                task_name="Child Review",
                prompt="review it",
                tool_names=("missing_tool",),
                metadata={"task_run_id": "run-1"},
            ),
        )

    assert [event.event_type for event in events] == ["started", "failed"]
    assert runtime.runner.calls == []
    record = registry.list_thread("thread-abc123abc123")[0]
    assert record.status == "failed"
    assert "unknown tool" in (record.error_message or "")
