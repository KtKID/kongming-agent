"""Thread subagents REST router tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace

from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from application.subagents.lifecycle import SubAgentLifecycleRegistry
from hosts.web.errors import KongmingWebError, kongming_error_handler
from hosts.web.routers.thread_subagents import router


class _ThreadManager:
    def __init__(self, *thread_ids: str) -> None:
        self.thread_ids = set(thread_ids)
        self.get_calls = 0
        self.list_calls = 0

    def get_thread(self, thread_id: str) -> SimpleNamespace | None:
        self.get_calls += 1
        if thread_id not in self.thread_ids:
            return None
        return SimpleNamespace(id=thread_id)

    def list_threads(self) -> list[SimpleNamespace]:
        self.list_calls += 1
        return [SimpleNamespace(id=thread_id) for thread_id in self.thread_ids]


def _app(
    registry: SubAgentLifecycleRegistry,
    *thread_ids: str,
    manager: _ThreadManager | None = None,
) -> FastAPI:
    app = FastAPI()
    app.state.thread_manager = manager or _ThreadManager(*thread_ids)
    app.state.subagent_lifecycle_registry = registry

    @app.middleware("http")
    async def _fake_auth(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request.state.session_payload = SimpleNamespace(user_id="default")
        return await call_next(request)

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

    manager = _ThreadManager(thread_id, other_thread_id)
    response = TestClient(_app(registry, manager=manager)).get(
        f"/api/threads/{thread_id}/subagents"
    )

    assert response.status_code == 200
    assert manager.get_calls == 1
    assert manager.list_calls == 0
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


def test_thread_subagents_router_requires_authenticated_context() -> None:
    registry = SubAgentLifecycleRegistry()
    app = FastAPI()
    app.state.thread_manager = _ThreadManager("thread-abc123abc123")
    app.state.subagent_lifecycle_registry = registry
    app.add_exception_handler(KongmingWebError, kongming_error_handler)
    app.include_router(router)

    response = TestClient(app).get("/api/threads/thread-abc123abc123/subagents")

    assert response.status_code == 500
    assert response.json()["message"] == "authenticated session payload is missing"


def test_thread_subagents_router_truncates_long_error_message() -> None:
    registry = SubAgentLifecycleRegistry()
    thread_id = "thread-abc123abc123"
    registry.record_finished(
        thread_id=thread_id,
        source="workflow",
        workflow_id="wf-1",
        task_id="task-1",
        task_run_id="run-1",
        task_name="Child Review",
        session_id="subagent-thread-abc123abc123-wf-1-run-1",
        status="failed",
        error_message="x" * 3000,
    )

    response = TestClient(_app(registry, thread_id)).get(
        f"/api/threads/{thread_id}/subagents?include_finished=true"
    )

    assert response.status_code == 200
    item = response.json()["subagents"][0]
    assert len(item["error_message"]) == 2000
