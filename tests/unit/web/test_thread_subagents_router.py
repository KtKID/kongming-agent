"""Thread subagents TaskRegistry REST router tests."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.agents.registry import TaskRecord, TaskRegistry
from hosts.web.errors import KongmingWebError, kongming_error_handler
from hosts.web.routers.thread_subagents import router


def _app(registry: TaskRegistry, *thread_ids: str) -> FastAPI:
    """构造经 ThreadManager/HostDispatcher 查询 TaskRegistry 的测试应用。"""
    cells = {}
    for thread_id in thread_ids:
        dispatcher = SimpleNamespace(
            agent_manager=object(),
            list_task_records=lambda *, include_finished=False, limit=50, tid=thread_id: (
                registry.list_thread_tasks(
                    tid,
                    include_finished=include_finished,
                    limit=limit,
                )
            ),
        )
        cells[thread_id] = SimpleNamespace(host_dispatcher=dispatcher)
    app = FastAPI()
    app.state.thread_manager = SimpleNamespace(
        list_threads=lambda: [SimpleNamespace(id=thread_id) for thread_id in thread_ids],
        get_cell=lambda thread_id: cells.get(thread_id),
    )
    app.state.subagent_lifecycle_registry = SimpleNamespace(
        list_thread=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy lifecycle registry must not be read")
        )
    )
    app.add_exception_handler(KongmingWebError, kongming_error_handler)
    app.include_router(router)
    return app


def _register_workflow_task(
    registry: TaskRegistry,
    *,
    thread_id: str,
    workflow_id: str,
    task_run_id: str,
    task_name: str,
    status: str = "pending",
) -> TaskRecord:
    """登记一个可投影的 workflow child。"""
    record = registry.register_pending(
        agent_id=f"agent-{task_run_id}",
        parent_task_id="parent-task",
        thread_id=thread_id,
        source="workflow",
        workflow_id=workflow_id,
        workflow_task_id=f"logical-{task_run_id}",
        task_run_id=task_run_id,
        task_name=task_name,
        session_id=f"subagent-{thread_id}-{workflow_id}-{task_run_id}",
    )
    if status == "running":
        record.status = "running"
    elif status in {"completed", "failed", "cancelled"}:
        registry.finish_task(record.task_id, status=status)  # type: ignore[arg-type]
    return record


def test_thread_subagents_router_lists_current_thread_only() -> None:
    registry = TaskRegistry()
    thread_id = "thread-abc123abc123"
    other_thread_id = "thread-def456def456"
    current = _register_workflow_task(
        registry,
        thread_id=thread_id,
        workflow_id="wf-1",
        task_run_id="run-1",
        task_name="Child Review",
        status="running",
    )
    _register_workflow_task(
        registry,
        thread_id=thread_id,
        workflow_id="wf-finished",
        task_run_id="run-finished",
        task_name="Finished Child",
        status="completed",
    )
    _register_workflow_task(
        registry,
        thread_id=other_thread_id,
        workflow_id="wf-2",
        task_run_id="run-2",
        task_name="Other Child",
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
    assert item["id"] == current.task_id
    assert item["task_id"] == current.task_id
    assert item["agent_id"] == current.agent_id
    assert item["thread_id"] == thread_id
    assert item["source"] == "workflow"
    assert item["workflow_id"] == "wf-1"
    assert item["workflow_task_id"] == "logical-run-1"
    assert item["task_run_id"] == "run-1"
    assert item["task_name"] == "Child Review"
    assert item["status"] == "running"
    assert isinstance(item["started_at_ms"], int)
    assert isinstance(item["updated_at_ms"], int)
    assert item["started_at_ms"] <= item["updated_at_ms"]
    assert item["finished_at_ms"] is None


def test_thread_subagents_router_returns_same_identity_after_finish() -> None:
    registry = TaskRegistry()
    thread_id = "thread-abc123abc123"
    record = _register_workflow_task(
        registry,
        thread_id=thread_id,
        workflow_id="wf-1",
        task_run_id="run-1",
        task_name="Child Review",
        status="running",
    )
    running = (
        TestClient(_app(registry, thread_id))
        .get(f"/api/threads/{thread_id}/subagents")
        .json()["subagents"][0]
    )
    registry.finish_task(record.task_id, status="completed")

    response = TestClient(_app(registry, thread_id)).get(
        f"/api/threads/{thread_id}/subagents?include_finished=true"
    )

    assert response.status_code == 200
    item = response.json()["subagents"][0]
    assert item["status"] == "completed"
    assert item["id"] == running["id"]
    assert item["task_id"] == running["task_id"]
    assert item["agent_id"] == running["agent_id"]
    assert item["session_id"] == running["session_id"]
    assert item["workflow_id"] == running["workflow_id"]
    assert item["started_at_ms"] == running["started_at_ms"]
    assert isinstance(item["updated_at_ms"], int)
    assert isinstance(item["finished_at_ms"], int)
    assert item["updated_at_ms"] == item["finished_at_ms"]


def test_thread_subagents_router_returns_empty_for_unbooted_thread() -> None:
    registry = TaskRegistry()
    thread_id = "thread-abc123abc123"
    app = _app(registry, thread_id)
    app.state.thread_manager.get_cell = lambda _thread_id: None

    response = TestClient(app).get(f"/api/threads/{thread_id}/subagents")

    assert response.status_code == 200
    assert response.json()["subagents"] == []


def test_thread_subagents_router_rejects_missing_thread() -> None:
    registry = TaskRegistry()

    response = TestClient(_app(registry, "thread-abc123abc123")).get(
        "/api/threads/thread-def456def456/subagents"
    )

    assert response.status_code == 404
    assert response.json()["message"] == "thread not found: thread-def456def456"
