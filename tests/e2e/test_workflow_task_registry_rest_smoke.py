"""SC_12：真实 HostDispatcher/AgentManager/TaskRegistry 到 REST 的薄 smoke。

本测试只替换 child LLM/runtime 返回值；spawn、账本状态机、host 门户和 FastAPI
路由均走生产实现。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.agents.subagent_tools import SpawnAgentRequest
from core.agent_spec import AgentSpec
from core.message import Message
from core.result import Result
from hosts.shared.host_dispatcher import HostDispatcher
from hosts.web.errors import KongmingWebError, kongming_error_handler
from hosts.web.routers.thread_subagents import router


class _ControlledChildRuntime:
    """只替换 child 模型完成时点的 runtime fake。"""

    def __init__(self) -> None:
        self.agent_spec = AgentSpec(
            name="root",
            instructions="",
            default_model="test-model",
            tool_names=(),
            max_turns=2,
        )
        self.tools: dict[str, object] = {}
        self.enabled_tools_snapshot: tuple[object, ...] = ()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, _text: str, **kwargs: object) -> Result:
        """等待测试释放后返回固定 child Result。"""
        self.started.set()
        await self.release.wait()
        session_id = str(kwargs["session_id"])
        return Result(
            run_id="child-run-1",
            session_id=session_id,
            status="completed",
            final_message=Message.assistant("child done"),
            turn_count=1,
        )


def _app(dispatcher: HostDispatcher, thread_id: str) -> FastAPI:
    """构造通过 ThreadManager.get_cell 暴露真实 HostDispatcher 的应用。"""
    cell = SimpleNamespace(host_dispatcher=dispatcher)
    app = FastAPI()
    app.state.thread_manager = SimpleNamespace(
        list_threads=lambda: [SimpleNamespace(id=thread_id)],
        get_cell=lambda requested: cell if requested == thread_id else None,
    )
    app.add_exception_handler(KongmingWebError, kongming_error_handler)
    app.include_router(router)
    return app


async def test_workflow_spawn_projects_same_task_identity_running_to_completed() -> None:
    """真实 spawn 在 running/completed 两次 REST 查询中保持同一 TaskRecord identity。"""
    thread_id = "thread-abc123abc123"
    runtime = _ControlledChildRuntime()
    dispatcher = HostDispatcher(runtime=runtime, session_id=thread_id)  # type: ignore[arg-type]
    await dispatcher.ensure_started()
    manager = dispatcher.agent_manager
    assert manager is not None
    assert manager.root_agent_id is not None
    request = SpawnAgentRequest(
        parent_agent_id=manager.root_agent_id,
        spec=AgentSpec(
            name="workflow-child",
            instructions="",
            default_model="test-model",
            tool_names=(),
            max_turns=1,
        ),
        seed_message=Message.user("review"),
        cwd="/tmp",
        child_session_id="subagent-thread-abc123abc123-wf-1-run-1",
        source_task_id="logical-task-1",
        metadata={
            "source": "workflow",
            "parent_session_id": thread_id,
            "workflow_id": "wf-1",
            "workflow_task_id": "logical-task-1",
            "task_run_id": "run-1",
            "task_name": "Child Review",
        },
        requested_tool_names=(),
    )

    try:
        spawn = manager.spawn(request)
        await asyncio.wait_for(runtime.started.wait(), timeout=2)
        client = TestClient(_app(dispatcher, thread_id))
        running_response = client.get(f"/api/threads/{thread_id}/subagents")
        assert running_response.status_code == 200
        running = running_response.json()["subagents"][0]
        assert running["task_id"] == spawn.task_id
        assert running["agent_id"] == spawn.child_id
        assert running["status"] == "running"

        runtime.release.set()
        for _ in range(100):
            records = dispatcher.list_task_records(include_finished=True, limit=10)
            if records and records[0].status == "completed":
                break
            await asyncio.sleep(0.01)

        completed_response = client.get(f"/api/threads/{thread_id}/subagents?include_finished=true")
        assert completed_response.status_code == 200
        completed = completed_response.json()["subagents"][0]
        for key in (
            "id",
            "task_id",
            "agent_id",
            "session_id",
            "workflow_id",
            "workflow_task_id",
            "task_run_id",
            "started_at_ms",
        ):
            assert completed[key] == running[key]
        assert completed["status"] == "completed"
        assert completed["finished_at_ms"] is not None
    finally:
        runtime.release.set()
        await dispatcher.aclose(drain=True)
