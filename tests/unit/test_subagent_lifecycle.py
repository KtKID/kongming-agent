"""Sub-agent lifecycle registry and manager tests."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from application.subagents.lifecycle import (
    SubAgentLifecycleEvent,
    SubAgentLifecycleRegistry,
    SubAgentLifecycleStore,
)
from application.subagents.manager import SubAgentManager, SubAgentTask
from core.message import Message
from core.result import Result


class _FakeRunner:
    """Fake runner that returns a configured result or raises an exception."""

    def __init__(self, outcome: Result | BaseException) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def run(self, *args: Any, **kwargs: Any) -> Result:
        self.calls.append({"args": args, "kwargs": kwargs})
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _runtime(outcome: Result | BaseException) -> SimpleNamespace:
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
async def test_subagent_manager_uses_source_as_workflow_fallback() -> None:
    registry = SubAgentLifecycleRegistry()
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

    await manager.run_task(
        workflow_id=None,
        parent_session_id="thread-abc123abc123",
        source="chat",
        task=_task(),
    )

    record = registry.list_thread("thread-abc123abc123")[0]
    assert record.workflow_id == "chat"
    assert record.session_id.startswith("subagent-thread-abc123abc123-chat-run-1")


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
async def test_subagent_manager_records_cancelled_error() -> None:
    registry = SubAgentLifecycleRegistry()
    events: list[SubAgentLifecycleEvent] = []
    registry.register(events.append)
    manager = SubAgentManager(
        _runtime(asyncio.CancelledError()),
        lifecycle_registry=registry,
    )

    with pytest.raises(asyncio.CancelledError):
        await manager.run_task(
            workflow_id="wf-1",
            parent_session_id="thread-abc123abc123",
            task=_task(),
        )

    assert [event.event_type for event in events] == ["started", "cancelled"]
    record = registry.list_thread("thread-abc123abc123")[0]
    assert record.status == "cancelled"
    assert record.error_message == "cancelled"


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


def test_subagent_lifecycle_warns_when_finish_arrives_before_start(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = SubAgentLifecycleRegistry()

    with caplog.at_level(logging.WARNING):
        registry.record_finished(
            thread_id="thread-abc123abc123",
            source="workflow",
            workflow_id="wf-1",
            task_id="task-1",
            task_run_id="run-1",
            task_name="Child Review",
            session_id="subagent-thread-abc123abc123-wf-1-run-1",
            status="failed",
            error_message="late failure",
        )

    assert "subagent lifecycle finished before start" in caplog.text
    record = registry.list_thread("thread-abc123abc123")[0]
    assert record.started_at == record.finished_at
    assert record.status == "failed"


def test_subagent_lifecycle_prune_preserves_running_records() -> None:
    store = SubAgentLifecycleStore(max_records_per_thread=2)
    thread_id = "thread-abc123abc123"
    store.record_started(
        thread_id=thread_id,
        source="workflow",
        workflow_id="wf-running",
        task_id="task-running",
        task_run_id="run-running",
        task_name="Running Child",
        session_id="subagent-thread-abc123abc123-wf-running-run-running",
    )
    for index in range(3):
        store.record_finished(
            thread_id=thread_id,
            source="workflow",
            workflow_id=f"wf-{index}",
            task_id=f"task-{index}",
            task_run_id=f"run-{index}",
            task_name=f"Finished Child {index}",
            session_id=f"subagent-thread-abc123abc123-wf-{index}-run-{index}",
            status="completed",
        )

    records = store.list_thread(thread_id, limit=10)

    assert len(records) == 2
    assert any(record.status == "running" for record in records)
