"""任务进度 v3 状态机测试。

本测试固定当前 workflow 快照的单一 owner、LLM start/next 命令、runtime 终态和并发重放语义。
关键流程：创建 v3 任务骨架，经 Manager 推进状态，再断言磁盘快照与内存结果一致。
关键函数：_open_llm_flow 构造 LLM 驱动 workflow，_task 构造不可变任务定义。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from infrastructure.config.models import Config
from sessions import (
    RuntimeTaskProgressStatus,
    SessionTaskProgressManager,
    TaskProgressConflictError,
    TaskProgressControlMode,
    TaskProgressTaskDefinition,
)


def _cfg(session_root: Path) -> Config:
    """构造文件 session 配置，输入为临时根目录，输出为 Config。"""
    return Config.model_validate(
        {
            "model": {"preset_id": "local-gemma-4-e4b-it"},
            "session": {"backend": "file", "file_store_path": str(session_root)},
        }
    )


def _task(
    task_id: str,
    order: int,
    *,
    depends_on: tuple[str, ...] = (),
) -> TaskProgressTaskDefinition:
    """构造任务定义，输入为步骤与依赖，输出为 Manager 初始化输入。"""
    return TaskProgressTaskDefinition(
        task_id=task_id,
        task_run_id=f"{order + 1:03d}-{task_id}",
        desc=f"执行 {task_id}",
        depends_on=depends_on,
        display_order=order,
    )


def _open_llm_flow(manager: SessionTaskProgressManager, session_id: str, workflow_id: str) -> None:
    """初始化两步 LLM workflow，输入为 Manager、session 与 workflow，输出为空。"""
    manager.open_workflow(
        session_id=session_id,
        workflow_id=workflow_id,
        title="两步任务流",
        control_mode=TaskProgressControlMode.LLM_STEPS,
        tasks=[_task("step-1", 0), _task("step-2", 1, depends_on=("step-1",))],
    )


def test_open_replaces_foreground_and_writes_schema_v2(tmp_path: Path) -> None:
    """验证新 workflow 接管 foreground，输入为连续两次初始化，输出为仅含新任务的 v2 快照。"""
    manager = SessionTaskProgressManager.from_config(_cfg(tmp_path / "sessions"))
    session_id = "thread-abc123abc123"
    _open_llm_flow(manager, session_id, "wf-a")

    snapshot = manager.open_workflow(
        session_id=session_id,
        workflow_id="wf-b",
        title="新任务流",
        control_mode=TaskProgressControlMode.LLM_STEPS,
        tasks=[_task("step-b", 0)],
    )

    assert snapshot.schema_version == 2
    assert snapshot.workflow_id == "wf-b"
    assert snapshot.title == "新任务流"
    assert [task.task_id for task in snapshot.tasks] == ["step-b"]
    assert snapshot.counts.model_dump() == {
        "pending": 1,
        "in_progress": 0,
        "completed": 0,
        "failed": 0,
        "cancelled": 0,
        "total": 1,
    }


def test_llm_start_and_next_are_single_atomic_transition(tmp_path: Path) -> None:
    """验证 start/next，输入为两步依赖链，输出为一次完成当前并激活下一步。"""
    manager = SessionTaskProgressManager.from_config(_cfg(tmp_path / "sessions"))
    session_id = "thread-abc123abc123"
    _open_llm_flow(manager, session_id, "wf-a")

    started = manager.start_llm_step(session_id, "wf-a", "step-1")
    advanced = manager.advance_llm_step(session_id, "wf-a", "step-1", "step-2")

    assert started.tasks[0].status == "in_progress"
    assert [task.status for task in advanced.tasks] == ["completed", "in_progress"]
    assert advanced.counts.completed == 1
    assert advanced.counts.in_progress == 1


def test_final_next_completes_flow_and_replay_is_idempotent(tmp_path: Path) -> None:
    """验证最后一步推进，输入为已激活末步骤，输出为完成快照与稳定重放结果。"""
    manager = SessionTaskProgressManager.from_config(_cfg(tmp_path / "sessions"))
    session_id = "thread-abc123abc123"
    _open_llm_flow(manager, session_id, "wf-a")
    manager.start_llm_step(session_id, "wf-a", "step-1")
    manager.advance_llm_step(session_id, "wf-a", "step-1", "step-2")

    completed = manager.advance_llm_step(session_id, "wf-a", "step-2", None)
    replayed = manager.advance_llm_step(session_id, "wf-a", "step-2", None)

    assert [task.status for task in completed.tasks] == ["completed", "completed"]
    assert replayed == completed


def test_rejects_invalid_dependency_and_stale_workflow_without_mutation(tmp_path: Path) -> None:
    """验证错误依赖和旧 workflow，输入为非法调用，输出为冲突且当前快照不变。"""
    manager = SessionTaskProgressManager.from_config(_cfg(tmp_path / "sessions"))
    session_id = "thread-abc123abc123"
    with pytest.raises(ValueError, match="unknown dependency"):
        manager.open_workflow(
            session_id=session_id,
            workflow_id="wf-invalid",
            title="非法依赖",
            control_mode=TaskProgressControlMode.LLM_STEPS,
            tasks=[_task("step-1", 0, depends_on=("missing",))],
        )

    _open_llm_flow(manager, session_id, "wf-a")
    manager.open_workflow(
        session_id=session_id,
        workflow_id="wf-b",
        title="新任务流",
        control_mode=TaskProgressControlMode.LLM_STEPS,
        tasks=[_task("step-b", 0)],
    )
    before = manager.read_snapshot(session_id)

    with pytest.raises(TaskProgressConflictError, match="current workflow"):
        manager.start_llm_step(session_id, "wf-a", "step-1")

    assert manager.read_snapshot(session_id) == before


def test_runtime_terminal_states_are_exact_and_cannot_regress(tmp_path: Path) -> None:
    """验证 runtime 终态，输入为 failed/cancelled 事件与晚到 running，输出为精确保留终态。"""
    manager = SessionTaskProgressManager.from_config(_cfg(tmp_path / "sessions"))
    session_id = "thread-abc123abc123"
    manager.open_workflow(
        session_id=session_id,
        workflow_id="wf-runtime",
        title="并行任务",
        control_mode=TaskProgressControlMode.RUNTIME_LIFECYCLE,
        tasks=[_task("failed-step", 0), _task("cancelled-step", 1)],
    )
    manager.record_runtime_transition(
        session_id,
        "wf-runtime",
        "failed-step",
        RuntimeTaskProgressStatus.FAILED,
        error_message="child exploded",
    )
    snapshot = manager.record_runtime_transition(
        session_id,
        "wf-runtime",
        "cancelled-step",
        RuntimeTaskProgressStatus.CANCELLED,
    )

    assert [task.status for task in snapshot.tasks] == ["failed", "cancelled"]
    assert snapshot.counts.failed == 1
    assert snapshot.counts.cancelled == 1
    with pytest.raises(TaskProgressConflictError, match="terminal"):
        manager.record_runtime_transition(
            session_id,
            "wf-runtime",
            "failed-step",
            RuntimeTaskProgressStatus.RUNNING,
        )


def test_llm_commands_reject_runtime_owned_snapshot(tmp_path: Path) -> None:
    """验证 control mode，输入为 runtime workflow 上的 LLM 命令，输出为 owner 冲突。"""
    manager = SessionTaskProgressManager.from_config(_cfg(tmp_path / "sessions"))
    manager.open_workflow(
        session_id="thread-abc123abc123",
        workflow_id="wf-runtime",
        title="并行任务",
        control_mode=TaskProgressControlMode.RUNTIME_LIFECYCLE,
        tasks=[_task("task-1", 0)],
    )

    with pytest.raises(TaskProgressConflictError, match="LLM steps"):
        manager.start_llm_step("thread-abc123abc123", "wf-runtime", "task-1")


def test_concurrent_identical_next_is_idempotent(tmp_path: Path) -> None:
    """验证并发重放，输入为两个相同 next，输出为一个进度迁移和一致结果。"""
    manager = SessionTaskProgressManager.from_config(_cfg(tmp_path / "sessions"))
    session_id = "thread-abc123abc123"
    _open_llm_flow(manager, session_id, "wf-a")
    manager.start_llm_step(session_id, "wf-a", "step-1")

    def advance() -> tuple[str, str]:
        """提交相同推进命令，输入为空，输出为两步状态字符串。"""
        snapshot = manager.advance_llm_step(session_id, "wf-a", "step-1", "step-2")
        return tuple(str(task.status) for task in snapshot.tasks)  # type: ignore[return-value]

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: advance(), range(2)))

    assert outcomes == [("completed", "in_progress"), ("completed", "in_progress")]
