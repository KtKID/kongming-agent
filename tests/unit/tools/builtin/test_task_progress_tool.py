"""advance_task_progress 工具契约测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.contracts import ToolContext
from infrastructure.config.models import Config
from sessions import (
    SessionTaskProgressManager,
    TaskProgressControlMode,
    TaskProgressTaskDefinition,
)
from tests.support.tool_calls import execute_prepared_tool
from tools import ToolRegistry, register_task_progress_tool
from tools.builtin.task_progress_tool import build_task_progress_tool_from_config

_SESSION_ID = "thread-abc123abc123"
_WORKFLOW_ID = "wf-20260619T032911-c05b897f"


def _cfg(session_root: Path) -> Config:
    """构造 file session 配置，输入为根路径，输出为可装配的 Config。"""
    return Config.model_validate(
        {
            "model": {"preset_id": "local-gemma-4-e4b-it"},
            "session": {"backend": "file", "file_store_path": str(session_root)},
        }
    )


def _ctx(session_id: str = _SESSION_ID) -> ToolContext:
    """构造当前会话工具上下文，输入为 session ID，输出为 ToolContext。"""
    return ToolContext(run_id="r", session_id=session_id, turn=1, call_id="c")


def _seed_llm_workflow(session_root: Path, *, task_count: int = 2) -> None:
    """初始化 LLM 步骤 workflow，输入为根路径和任务数，输出为 v2 快照。"""
    tasks = [
        TaskProgressTaskDefinition(
            task_id=f"step-{index}",
            task_run_id=f"{index:03d}-step-{index}",
            desc=f"步骤 {index}",
            depends_on=(f"step-{index - 1}",) if index else (),
            display_order=index,
        )
        for index in range(task_count)
    ]
    SessionTaskProgressManager.from_config(_cfg(session_root)).open_workflow(
        session_id=_SESSION_ID,
        workflow_id=_WORKFLOW_ID,
        title="执行计划",
        control_mode=TaskProgressControlMode.LLM_STEPS,
        tasks=tasks,
    )


def test_registers_only_restricted_advance_tool(tmp_path: Path) -> None:
    """工具注册表公开受限命令名，旧自由更新名从注册表消失。"""
    registry = ToolRegistry()

    register_task_progress_tool(registry, _cfg(tmp_path / "sessions"))

    assert "advance_task_progress" in registry.names()
    assert "update_task_progress" not in registry.names()


@pytest.mark.asyncio
async def test_start_and_next_only_change_authorized_steps(tmp_path: Path) -> None:
    """start/next 穿过真实 Manager，并保留初始化骨架字段。"""
    session_root = tmp_path / "sessions"
    _seed_llm_workflow(session_root)
    tool = build_task_progress_tool_from_config(_cfg(session_root))

    started = await execute_prepared_tool(
        tool,
        {"action": "start", "workflow_id": _WORKFLOW_ID, "step_id": "step-0"},
        _ctx(),
    )
    advanced = await execute_prepared_tool(
        tool,
        {
            "action": "next",
            "workflow_id": _WORKFLOW_ID,
            "step_id": "step-0",
            "next_step_id": "step-1",
        },
        _ctx(),
    )

    assert started.ok is True
    assert advanced.ok is True
    assert advanced.data is not None
    assert advanced.data["counts"] == {
        "pending": 0,
        "in_progress": 1,
        "completed": 1,
        "failed": 0,
        "cancelled": 0,
        "total": 2,
    }
    tasks = advanced.data["tasks"]
    assert isinstance(tasks, list)
    assert [(task["task_id"], task["status"], task["desc"]) for task in tasks] == [
        ("step-0", "completed", "步骤 0"),
        ("step-1", "in_progress", "步骤 1"),
    ]
    assert "orchestration_task_id" not in tasks[0]
    path = session_root / _SESSION_ID / "task_progress.json"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["workflow_id"] == _WORKFLOW_ID
    assert persisted["control_mode"] == "llm_steps"


@pytest.mark.asyncio
async def test_final_next_completes_last_step_and_replays_idempotently(tmp_path: Path) -> None:
    """最后一个 next 自动完成，重复同命令保持同一终态。"""
    session_root = tmp_path / "sessions"
    _seed_llm_workflow(session_root, task_count=1)
    tool = build_task_progress_tool_from_config(_cfg(session_root))
    start = {"action": "start", "workflow_id": _WORKFLOW_ID, "step_id": "step-0"}
    finish = {"action": "next", "workflow_id": _WORKFLOW_ID, "step_id": "step-0"}

    await execute_prepared_tool(tool, start, _ctx())
    first = await execute_prepared_tool(tool, finish, _ctx())
    replay = await execute_prepared_tool(tool, finish, _ctx())

    assert first.ok is True
    assert replay.ok is True
    assert replay.data is not None
    assert replay.data["counts"]["completed"] == 1
    assert replay.data["tasks"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_recoverable_tool_argument_error_keeps_active_step_in_progress(
    tmp_path: Path,
) -> None:
    """普通工具参数错误不生成业务终态，当前 LLM 步骤可继续完成。"""
    session_root = tmp_path / "sessions"
    _seed_llm_workflow(session_root, task_count=1)
    manager = SessionTaskProgressManager.from_config(_cfg(session_root))
    tool = build_task_progress_tool_from_config(_cfg(session_root))
    start = {"action": "start", "workflow_id": _WORKFLOW_ID, "step_id": "step-0"}
    invalid = {**start, "status": "failed"}

    started = await execute_prepared_tool(tool, start, _ctx())
    failed_attempt = await execute_prepared_tool(tool, invalid, _ctx())
    snapshot = manager.read_snapshot(_SESSION_ID)

    assert started.ok is True
    assert failed_attempt.ok is False
    assert snapshot.tasks[0].status.value == "in_progress"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {
                "action": "start",
                "workflow_id": _WORKFLOW_ID,
                "step_id": "step-0",
                "status": "completed",
            },
            "unknown task progress fields",
        ),
        (
            {
                "action": "start",
                "workflow_id": _WORKFLOW_ID,
                "step_id": "step-0",
                "tasks": [],
            },
            "unknown task progress fields",
        ),
        (
            {
                "action": "start",
                "workflow_id": _WORKFLOW_ID,
                "step_id": "step-0",
                "next_step_id": "step-1",
            },
            "only accepted for action=next",
        ),
    ],
)
async def test_rejects_arbitrary_state_or_shape_fields(
    tmp_path: Path,
    arguments: dict[str, object],
    message: str,
) -> None:
    """工具拒绝任意状态、任务数组和不适用的推进字段。"""
    tool = build_task_progress_tool_from_config(_cfg(tmp_path / "sessions"))

    result = await execute_prepared_tool(tool, arguments, _ctx())

    assert result.ok is False
    assert message in (result.error_message or "")


@pytest.mark.asyncio
async def test_rejects_runtime_owned_workflow_and_missing_context_session(tmp_path: Path) -> None:
    """LLM 命令只能作用于 LLM 步骤 workflow，并要求当前 session。"""
    session_root = tmp_path / "sessions"
    manager = SessionTaskProgressManager.from_config(_cfg(session_root))
    manager.open_workflow(
        session_id=_SESSION_ID,
        workflow_id=_WORKFLOW_ID,
        title="运行时任务",
        control_mode=TaskProgressControlMode.RUNTIME_LIFECYCLE,
        tasks=[
            TaskProgressTaskDefinition(
                task_id="step-0",
                task_run_id="001-step-0",
                desc="运行时步骤",
                display_order=0,
            )
        ],
    )
    tool = build_task_progress_tool_from_config(_cfg(session_root))
    command = {"action": "start", "workflow_id": _WORKFLOW_ID, "step_id": "step-0"}

    runtime_result = await execute_prepared_tool(tool, command, _ctx())
    blank_context_result = await execute_prepared_tool(tool, command, _ctx(""))

    assert runtime_result.ok is False
    assert "does not accept LLM steps" in (runtime_result.error_message or "")
    assert blank_context_result.ok is False
    assert "ToolContext.session_id" in (blank_context_result.error_message or "")
