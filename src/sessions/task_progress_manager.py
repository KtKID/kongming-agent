"""Session 任务进度公共 Manager。

本模块是当前 foreground workflow 进度的唯一状态 owner。
关键流程：workflow 初始化任务骨架，LLM 提交 start/next 命令，runtime 提交生命周期事实，Manager 在仓储锁内验证并迁移快照。
关键函数：open_workflow 初始化，start_llm_step 与 advance_llm_step 推进 LLM workflow，record_runtime_transition 收口真实终态。
"""

from __future__ import annotations

from collections.abc import Sequence

from infrastructure.config.models import Config
from sessions._task_progress_repository import SessionTaskProgressRepository
from sessions.task_progress_models import (
    RuntimeTaskProgressStatus,
    TaskProgressControlMode,
    TaskProgressItem,
    TaskProgressSnapshot,
    TaskProgressStatus,
    TaskProgressTaskDefinition,
    compute_counts,
    current_time_ms,
)

_TERMINAL_STATUSES = frozenset(
    {
        TaskProgressStatus.COMPLETED,
        TaskProgressStatus.FAILED,
        TaskProgressStatus.CANCELLED,
    }
)
_RUNTIME_STATUS_MAP: dict[RuntimeTaskProgressStatus, TaskProgressStatus] = {
    RuntimeTaskProgressStatus.ASSIGNED: TaskProgressStatus.PENDING,
    RuntimeTaskProgressStatus.RUNNING: TaskProgressStatus.IN_PROGRESS,
    RuntimeTaskProgressStatus.COMPLETED: TaskProgressStatus.COMPLETED,
    RuntimeTaskProgressStatus.FAILED: TaskProgressStatus.FAILED,
    RuntimeTaskProgressStatus.CANCELLED: TaskProgressStatus.CANCELLED,
}


class TaskProgressConflictError(ValueError):
    """表示当前快照坐标、控制模式或状态迁移与命令冲突。"""


class SessionTaskProgressManager:
    """Session 任务进度公共入口。"""

    def __init__(self, repository: SessionTaskProgressRepository) -> None:
        """绑定仓储，输入为唯一持久化 helper，输出为空。"""
        self._repository = repository

    @classmethod
    def from_config(cls, config: Config) -> SessionTaskProgressManager:
        """从配置构建 Manager，输入为 Config，输出为 session 进度入口。"""
        return cls(SessionTaskProgressRepository.from_config(config))

    @property
    def repository(self) -> SessionTaskProgressRepository:
        """暴露 repository 给测试检查路径。"""
        return self._repository

    def read_snapshot(self, session_id: str) -> TaskProgressSnapshot:
        """读取当前 session 快照，输入为 session ID，输出为 foreground 或空快照。"""
        return self._repository.read(session_id)

    def open_workflow(
        self,
        *,
        session_id: str,
        workflow_id: str,
        title: str,
        control_mode: TaskProgressControlMode,
        tasks: Sequence[TaskProgressTaskDefinition],
    ) -> TaskProgressSnapshot:
        """初始化并接管 foreground，输入为 workflow 骨架，输出为全 pending 的新快照。"""
        workflow_key = self._require_non_empty(workflow_id, "workflow_id")
        title_value = self._require_non_empty(title, "title")
        definitions = tuple(tasks)
        self._validate_definitions(definitions)
        now = current_time_ms()
        items = [
            TaskProgressItem(
                task_id=definition.task_id,
                task_run_id=definition.task_run_id,
                desc=definition.desc,
                depends_on=definition.depends_on,
                status=TaskProgressStatus.PENDING,
                display_order=definition.display_order,
                updated_at_ms=now,
            )
            for definition in definitions
        ]
        replacement = TaskProgressSnapshot(
            schema_version=2,
            session_id=session_id,
            workflow_id=workflow_key,
            title=title_value,
            control_mode=control_mode,
            updated_at_ms=now,
            tasks=items,
            counts=compute_counts(items),
        )

        def _replace(_current: TaskProgressSnapshot) -> TaskProgressSnapshot:
            """替换 foreground 快照，输入为旧快照，输出为新 workflow 快照。"""
            return replacement

        return self._repository.update(session_id, _replace)

    def start_llm_step(
        self,
        session_id: str,
        workflow_id: str,
        step_id: str,
    ) -> TaskProgressSnapshot:
        """激活 LLM workflow 的一个就绪步骤，输入为坐标和步骤，输出为迁移后快照。"""
        step_key = self._require_non_empty(step_id, "step_id")

        def _start(current: TaskProgressSnapshot) -> TaskProgressSnapshot:
            """执行 start 迁移，输入为当前快照，输出为激活或幂等快照。"""
            self._require_llm_workflow(current, workflow_id)
            target = self._task_by_id(current, step_key)
            if target.status is TaskProgressStatus.IN_PROGRESS:
                return current
            if target.status is not TaskProgressStatus.PENDING:
                raise TaskProgressConflictError(
                    f"step is not pending: workflow_id={workflow_id}, step_id={step_key}"
                )
            if self._in_progress_tasks(current.tasks):
                raise TaskProgressConflictError("an LLM workflow already has an in_progress step")
            if not self._dependencies_completed(current.tasks, target):
                raise TaskProgressConflictError(
                    f"step dependencies are not completed: step_id={step_key}"
                )
            return self._replace_task(
                current,
                step_key,
                status=TaskProgressStatus.IN_PROGRESS,
                error_message=None,
            )

        return self._repository.update(session_id, _start)

    def advance_llm_step(
        self,
        session_id: str,
        workflow_id: str,
        step_id: str,
        next_step_id: str | None,
    ) -> TaskProgressSnapshot:
        """完成当前 LLM 步骤并可激活下一步，输入为当前与下一步骤，输出为原子迁移结果。"""
        step_key = self._require_non_empty(step_id, "step_id")
        next_key = (
            self._require_non_empty(next_step_id, "next_step_id")
            if next_step_id is not None
            else None
        )

        def _advance(current: TaskProgressSnapshot) -> TaskProgressSnapshot:
            """执行 next 迁移，输入为当前快照，输出为推进、完成或幂等快照。"""
            self._require_llm_workflow(current, workflow_id)
            current_task = self._task_by_id(current, step_key)
            if current_task.status is TaskProgressStatus.COMPLETED:
                return self._replayed_advance(current, step_key, next_key)
            if current_task.status is not TaskProgressStatus.IN_PROGRESS:
                raise TaskProgressConflictError(
                    f"step is not in_progress: workflow_id={workflow_id}, step_id={step_key}"
                )
            active = self._in_progress_tasks(current.tasks)
            if len(active) != 1 or active[0].task_id != step_key:
                raise TaskProgressConflictError(
                    "LLM workflow requires exactly one current in_progress step"
                )

            completed = self._replace_task(
                current,
                step_key,
                status=TaskProgressStatus.COMPLETED,
                error_message=None,
            )
            if next_key is None:
                if all(task.status is TaskProgressStatus.COMPLETED for task in completed.tasks):
                    return completed
                raise TaskProgressConflictError(
                    "next_step_id is required while unfinished tasks remain"
                )

            next_task = self._task_by_id(completed, next_key)
            if next_task.status is not TaskProgressStatus.PENDING:
                raise TaskProgressConflictError(
                    f"next step is not pending: workflow_id={workflow_id}, step_id={next_key}"
                )
            if not self._dependencies_completed(completed.tasks, next_task):
                raise TaskProgressConflictError(
                    f"next step dependencies are not completed: step_id={next_key}"
                )
            return self._replace_task(
                completed,
                next_key,
                status=TaskProgressStatus.IN_PROGRESS,
                error_message=None,
            )

        return self._repository.update(session_id, _advance)

    def record_runtime_transition(
        self,
        session_id: str,
        workflow_id: str,
        task_id: str,
        runtime_status: RuntimeTaskProgressStatus,
        *,
        error_message: str | None = None,
    ) -> TaskProgressSnapshot:
        """记录 runtime 生命周期事实，输入为坐标、状态和错误，输出为状态机收口后的快照。"""
        task_key = self._require_non_empty(task_id, "task_id")

        def _record(current: TaskProgressSnapshot) -> TaskProgressSnapshot:
            """执行 runtime 迁移，输入为当前快照，输出为迁移或幂等快照。"""
            self._require_runtime_workflow(current, workflow_id)
            task = self._task_by_id(current, task_key)
            target_status = _RUNTIME_STATUS_MAP[runtime_status]
            if task.status in _TERMINAL_STATUSES:
                if task.status is target_status:
                    return current
                raise TaskProgressConflictError(
                    f"terminal task cannot regress: task_id={task_key}, status={task.status}"
                )
            if task.status is target_status:
                return current
            if (
                target_status is TaskProgressStatus.PENDING
                and task.status is not TaskProgressStatus.PENDING
            ):
                raise TaskProgressConflictError(
                    f"runtime assigned cannot regress task: task_id={task_key}, status={task.status}"
                )
            return self._replace_task(
                current,
                task_key,
                status=target_status,
                error_message=error_message,
            )

        return self._repository.update(session_id, _record)

    def _replayed_advance(
        self,
        snapshot: TaskProgressSnapshot,
        step_id: str,
        next_step_id: str | None,
    ) -> TaskProgressSnapshot:
        """识别相同 next 的重放，输入为已完成步骤与下一步，输出为幂等快照或冲突。"""
        if next_step_id is None:
            if all(task.status is TaskProgressStatus.COMPLETED for task in snapshot.tasks):
                return snapshot
            raise TaskProgressConflictError(f"stale next command: step_id={step_id}")
        next_task = self._task_by_id(snapshot, next_step_id)
        if next_task.status is TaskProgressStatus.IN_PROGRESS:
            return snapshot
        raise TaskProgressConflictError(f"stale next command: step_id={step_id}")

    def _require_llm_workflow(self, snapshot: TaskProgressSnapshot, workflow_id: str) -> None:
        """校验 LLM 命令坐标，输入为快照和 workflow ID，冲突时抛异常。"""
        self._require_current_workflow(snapshot, workflow_id)
        if snapshot.control_mode is not TaskProgressControlMode.LLM_STEPS:
            raise TaskProgressConflictError("current workflow does not accept LLM steps")

    def _require_runtime_workflow(self, snapshot: TaskProgressSnapshot, workflow_id: str) -> None:
        """校验 runtime 命令坐标，输入为快照和 workflow ID，冲突时抛异常。"""
        self._require_current_workflow(snapshot, workflow_id)
        if snapshot.control_mode is not TaskProgressControlMode.RUNTIME_LIFECYCLE:
            raise TaskProgressConflictError("current workflow does not accept runtime lifecycle")

    def _require_current_workflow(self, snapshot: TaskProgressSnapshot, workflow_id: str) -> None:
        """校验 foreground workflow，输入为快照和 workflow ID，冲突时抛异常。"""
        workflow_key = self._require_non_empty(workflow_id, "workflow_id")
        if snapshot.workflow_id != workflow_key:
            raise TaskProgressConflictError(
                f"current workflow differs: expected={snapshot.workflow_id}, actual={workflow_key}"
            )

    def _task_by_id(self, snapshot: TaskProgressSnapshot, task_id: str) -> TaskProgressItem:
        """查询任务，输入为快照和 task ID，输出为唯一任务项。"""
        for task in snapshot.tasks:
            if task.task_id == task_id:
                return task
        raise TaskProgressConflictError(f"unknown task_id: {task_id}")

    def _replace_task(
        self,
        snapshot: TaskProgressSnapshot,
        task_id: str,
        *,
        status: TaskProgressStatus,
        error_message: str | None,
    ) -> TaskProgressSnapshot:
        """替换单任务状态，输入为快照、任务和新状态，输出为同一事务内的新快照。"""
        now = current_time_ms()
        tasks = [
            task.model_copy(
                update={
                    "status": status,
                    "error_message": error_message,
                    "updated_at_ms": now,
                }
            )
            if task.task_id == task_id
            else task
            for task in snapshot.tasks
        ]
        return snapshot.model_copy(
            update={
                "updated_at_ms": now,
                "tasks": tasks,
                "counts": compute_counts(tasks),
            }
        )

    def _dependencies_completed(
        self,
        tasks: Sequence[TaskProgressItem],
        target: TaskProgressItem,
    ) -> bool:
        """判断依赖是否完成，输入为任务列表和目标任务，输出为就绪布尔值。"""
        statuses = {task.task_id: task.status for task in tasks}
        return all(
            statuses.get(dependency) is TaskProgressStatus.COMPLETED
            for dependency in target.depends_on
        )

    def _in_progress_tasks(self, tasks: Sequence[TaskProgressItem]) -> list[TaskProgressItem]:
        """筛选运行中任务，输入为任务列表，输出为运行中任务列表。"""
        return [task for task in tasks if task.status is TaskProgressStatus.IN_PROGRESS]

    def _validate_definitions(self, definitions: tuple[TaskProgressTaskDefinition, ...]) -> None:
        """校验 workflow 骨架，输入为定义元组，非法时抛 ValueError。"""
        if not definitions:
            raise ValueError("workflow tasks must be non-empty")
        if len(definitions) > 128:
            raise ValueError("workflow tasks must contain at most 128 items")
        task_ids = [definition.task_id for definition in definitions]
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("workflow tasks contain duplicate task_id")
        display_orders = [definition.display_order for definition in definitions]
        if len(set(display_orders)) != len(display_orders):
            raise ValueError("workflow tasks contain duplicate display_order")
        known_ids = set(task_ids)
        dependencies = {definition.task_id: definition.depends_on for definition in definitions}
        for task_id, required in dependencies.items():
            for dependency in required:
                if dependency not in known_ids:
                    raise ValueError(
                        f"unknown dependency: task_id={task_id}, dependency={dependency}"
                    )
        self._validate_acyclic_dependencies(dependencies)

    def _validate_acyclic_dependencies(self, dependencies: dict[str, tuple[str, ...]]) -> None:
        """校验依赖图无环，输入为 task 到依赖的映射，发现环时抛 ValueError。"""
        visiting: set[str] = set()
        visited: set[str] = set()

        def _visit(task_id: str) -> None:
            """深度遍历一个任务，输入为 task ID，发现环时抛 ValueError。"""
            if task_id in visited:
                return
            if task_id in visiting:
                raise ValueError(f"workflow task dependencies contain a cycle at: {task_id}")
            visiting.add(task_id)
            for dependency in dependencies[task_id]:
                _visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in dependencies:
            _visit(task_id)

    def _require_non_empty(self, value: str, field_name: str) -> str:
        """规范化必填标识，输入为值和字段名，输出为去空白后的字符串。"""
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string")
        return value.strip()


__all__ = ["SessionTaskProgressManager", "TaskProgressConflictError"]
