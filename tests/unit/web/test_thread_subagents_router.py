"""Thread subagents REST router tests."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.subagents.lifecycle import SubAgentLifecycleRegistry
from hosts.web.errors import KongmingWebError, kongming_error_handler
from hosts.web.routers.thread_subagents import router


def _app(registry: SubAgentLifecycleRegistry, *thread_ids: str) -> FastAPI:
    app = FastAPI()
    app.state.thread_manager = SimpleNamespace(
        list_threads=lambda: [SimpleNamespace(id=thread_id) for thread_id in thread_ids]
    )
    app.state.subagent_lifecycle_registry = registry
    app.add_exception_handler(KongmingWebError, kongming_error_handler)
    app.include_router(router)
    return app


def test_thread_subagents_router_lists_current_thread_only() -> None:
    registry = SubAgentLifecycleRegistry()
    thread_id = "thread-abc123abc123"
    other_thread_id = "thread-def456def456"
    registry.record_started(
        thread_id=thread_id,
        source="workflow",
        workflow_id="wf-1",
        task_id="task-1",
        task_run_id="run-1",
        task_name="Child Review",
        session_id="subagent-thread-abc123abc123-wf-1-run-1",
    )
    registry.record_finished(
        thread_id=thread_id,
        source="workflow",
        workflow_id="wf-finished",
        task_id="task-finished",
        task_run_id="run-finished",
        task_name="Finished Child",
        session_id="subagent-thread-abc123abc123-wf-finished-run-finished",
        status="completed",
    )
    registry.record_finished(
        thread_id=other_thread_id,
        source="workflow",
        workflow_id="wf-2",
        task_id="task-2",
        task_run_id="run-2",
        task_name="Other Child",
        session_id="subagent-thread-def456def456-wf-2-run-2",
        status="completed",
    )

    response = TestClient(_app(registry, thread_id, other_thread_id)).get(
        f"/api/threads/{thread_id}/subagents"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["thread_id"] == thread_id
    assert len(body["subagents"]) == 1
    item = body["subagents"][0]
    assert item["id"] == (
        "source:workflow|workflow:wf-1|task_run:run-1|"
        "session:subagent-thread-abc123abc123-wf-1-run-1"
    )
    assert item["thread_id"] == thread_id
    assert item["source"] == "workflow"
    assert item["workflow_id"] == "wf-1"
    assert item["task_id"] == "task-1"
    assert item["task_run_id"] == "run-1"
    assert item["task_name"] == "Child Review"
    assert item["status"] == "running"
    assert isinstance(item["started_at_ms"], int)
    assert isinstance(item["updated_at_ms"], int)
    assert item["started_at_ms"] <= item["updated_at_ms"]
    assert item["finished_at_ms"] is None


def test_thread_subagents_router_returns_finished_ms_fields() -> None:
    registry = SubAgentLifecycleRegistry()
    thread_id = "thread-abc123abc123"
    registry.record_started(
        thread_id=thread_id,
        source="workflow",
        workflow_id="wf-1",
        task_id="task-1",
        task_run_id="run-1",
        task_name="Child Review",
        session_id="subagent-thread-abc123abc123-wf-1-run-1",
    )
    registry.record_finished(
        thread_id=thread_id,
        source="workflow",
        workflow_id="wf-1",
        task_id="task-1",
        task_run_id="run-1",
        task_name="Child Review",
        session_id="subagent-thread-abc123abc123-wf-1-run-1",
        status="completed",
    )

    response = TestClient(_app(registry, thread_id)).get(
        f"/api/threads/{thread_id}/subagents?include_finished=true"
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["subagents"]) == 1
    item = body["subagents"][0]
    assert item["status"] == "completed"
    assert isinstance(item["started_at_ms"], int)
    assert isinstance(item["updated_at_ms"], int)
    assert isinstance(item["finished_at_ms"], int)
    assert item["started_at_ms"] <= item["updated_at_ms"]
    assert item["updated_at_ms"] == item["finished_at_ms"]


def test_thread_subagents_router_rejects_missing_thread() -> None:
    registry = SubAgentLifecycleRegistry()

    response = TestClient(_app(registry, "thread-abc123abc123")).get(
        "/api/threads/thread-def456def456/subagents"
    )

    assert response.status_code == 404
    assert response.json()["message"] == "thread not found: thread-def456def456"
