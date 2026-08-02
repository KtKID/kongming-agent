"""TaskRegistry：资源/生命周期域账本边界类。

功能：登记 agent_run task（asyncio.Task）与 external_process PID，提供
``cancel_subtree`` 后序遍历砍靶（先 kill+wait 外部 PID，再 cancel run_task）
+ 关门标志防漏杀；单 agent 退化形态下树上只有根节点，退化为「cancel 根 run_task
+ kill+wait 根 external PID」。
作用：让 AgentManager（task-5）与 Web InterruptFrame 只依赖 TaskRegistry，
通过统一账本入口登记资源，cancel 时按后序遍历收口所有外部进程与 asyncio task。
关键执行流程：
- ``register_run`` / ``register_external``：写账本（task_id → TaskRecord），
  入口检查关门标志，关闭后拒绝新登记（防「dispatched 与登记之间被打断」漏杀孤儿子）。
- ``cancel_subtree``：入口即置关门标志 → 后序遍历该 agent 子树所有 TaskRecord →
  对每个 external_process record 取 ``resources`` 中的 PidHandle，``kill``+``wait``
  收口（无僵尸）→ 再对每个 agent_run record 的 ``handle`` 调 ``task.cancel()``
  （asyncio cancellation 沿 await 链自动传播到子孙）。
- ``close_registry``：置关门标志（bool），register 侧 double-check 拒绝。
- ``no_live_descendants``：single_shot 退出条件判定（无存活 run_task/PID）。
关键函数：``register_run`` / ``register_external`` / ``cancel_subtree`` /
``close_registry`` / ``no_live_descendants`` / ``close_cell``。

参考 spec：``docs/spec/agent-tree-v0.1/04-data-and-state.md``（TaskRecord/PidHandle
定义、状态流转、打断链三段时间线）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from typing import Literal

from core.clock import now_epoch_ms

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 类型别名
# ---------------------------------------------------------------------------

# 审计状态（迁移自 SubAgentLifecycleRecord 的 status 字段，扩展出 pending）。
TaskStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
TerminalTaskStatus = Literal["completed", "failed", "cancelled"]

# 资源种类：只 2 种（agent_run + external_process），无 detach 豁免（需求要点 12）。
TaskKind = Literal["agent_run", "external_process"]

# 外部进程种类（kill 信号策略可按 kind 分流，当前统一 SIGTERM→SIGKILL 兜底）。
PidKind = Literal["bash", "other"]


# ---------------------------------------------------------------------------
# 数据结构（spec 04-data-and-state.md）
# ---------------------------------------------------------------------------


@dataclass
class PidHandle:
    """external_process 的 kill 靶（新建）。

    职责：承载外部进程 PID 与类型，供 cancel_subtree 后序 kill+wait 收口。
    关键输入：register_external 传入 pid + kind。
    关键输出：cancel_subtree 读取 pid + kind 决定 kill 信号策略。
    """

    pid: int  # 进程 PID
    kind: PidKind  # 进程类型（bash / other；kill 信号策略可按 kind 分流）


@dataclass(frozen=True)
class TaskIdentity:
    """TaskRecord 创建后保持稳定的身份坐标。"""

    task_id: str
    agent_id: str
    parent_task_id: str | None
    kind: TaskKind
    thread_id: str
    source: str
    workflow_id: str | None
    workflow_task_id: str | None
    task_run_id: str
    task_name: str
    session_id: str
    started_at: int


@dataclass(frozen=True)
class TaskRegistrationContext:
    """一棵 root agent tree 的默认业务身份坐标。

    HostDispatcher 在创建 TaskRegistry 时冻结该上下文；agent_loop 继续通过
    ``register_run`` 登记真实 asyncio Task，Registry 负责把业务 ID 附着到同一条记录。
    """

    thread_id: str = ""
    source: str = "agent"
    workflow_id: str | None = None
    workflow_task_id: str | None = None
    task_run_id: str = ""
    task_name: str = ""
    session_id: str = ""


@dataclass(frozen=True)
class TaskProjection:
    """供 AgentManager/Host/Web 消费的不可变任务投影。"""

    task_id: str
    agent_id: str
    parent_task_id: str | None
    kind: TaskKind
    thread_id: str
    source: str
    workflow_id: str | None
    workflow_task_id: str | None
    task_run_id: str
    task_name: str
    session_id: str
    status: TaskStatus
    started_at: int
    updated_at: int
    finished_at: int | None
    error_message: str | None


@dataclass
class TaskRecord:
    """账本记录（新建，审计字段迁移自 SubAgentLifecycleRecord）。

    职责：承载单个 task 的唯一坐标（task_id / agent_id / parent_task_id）、
    资源种类（kind）、资源句柄（handle / resources）与审计字段。
    关键输入：register_run / register_external 构造初始记录。
    关键输出：cancel_subtree 读取 handle / resources 决定砍靶顺序；审计字段
    保留运行追溯能力。
    """

    identity: TaskIdentity
    status: TaskStatus  # pending/running/completed/failed/cancelled（审计保留）
    handle: (
        asyncio.Task[object] | None
    )  # agent_run 的 asyncio.Task（cancel 靶）；external_process = None
    resources: list[PidHandle]  # external_process 的 PID 列表（kill 靶）；agent_run = []
    updated_at: int
    finished_at: int | None  # 审计保留（epoch 毫秒）；未完成 = None
    error_message: str | None  # 审计保留

    @property
    def task_id(self) -> str:
        """返回冻结 task id。"""
        return self.identity.task_id

    @property
    def agent_id(self) -> str:
        """返回冻结 agent id。"""
        return self.identity.agent_id

    @property
    def parent_task_id(self) -> str | None:
        """返回冻结父 task id。"""
        return self.identity.parent_task_id

    @property
    def kind(self) -> TaskKind:
        """返回冻结资源种类。"""
        return self.identity.kind

    @property
    def thread_id(self) -> str:
        """返回冻结 thread id。"""
        return self.identity.thread_id

    @property
    def source(self) -> str:
        """返回冻结来源。"""
        return self.identity.source

    @property
    def workflow_id(self) -> str | None:
        """返回冻结 workflow id。"""
        return self.identity.workflow_id

    @property
    def workflow_task_id(self) -> str | None:
        """返回冻结 workflow 逻辑任务 id。"""
        return self.identity.workflow_task_id

    @property
    def task_run_id(self) -> str:
        """返回冻结 workflow task run id。"""
        return self.identity.task_run_id

    @property
    def task_name(self) -> str:
        """返回冻结展示名。"""
        return self.identity.task_name

    @property
    def session_id(self) -> str:
        """返回冻结子 session id。"""
        return self.identity.session_id

    @property
    def started_at(self) -> int:
        """返回冻结开始时间。"""
        return self.identity.started_at

    def project(self) -> TaskProjection:
        """生成不可变公开投影。"""
        return TaskProjection(
            task_id=self.task_id,
            agent_id=self.agent_id,
            parent_task_id=self.parent_task_id,
            kind=self.kind,
            thread_id=self.thread_id,
            source=self.source,
            workflow_id=self.workflow_id,
            workflow_task_id=self.workflow_task_id,
            task_run_id=self.task_run_id,
            task_name=self.task_name,
            session_id=self.session_id,
            status=self.status,
            started_at=self.started_at,
            updated_at=self.updated_at,
            finished_at=self.finished_at,
            error_message=self.error_message,
        )


# ---------------------------------------------------------------------------
# 边界类
# ---------------------------------------------------------------------------


class TaskRegistry:
    """资源/生命周期域账本边界类。

    职责：唯一账本入口，登记 agent_run task 与 external_process PID，提供
    cancel_subtree 后序砍靶与关门标志防漏杀。
    关键输入：register_run/register_external 的 task/pid + agent_id + parent_task_id；
    cancel_subtree/close_registry/no_live_descendants 的 agent_id。
    关键输出：register_* 返回 TaskRecord；cancel_subtree 后序 kill+wait PID +
    cancel run_task（返回 None）；no_live_descendants 返回 bool。
    """

    def __init__(
        self,
        *,
        max_terminal_records: int = 200,
        registration_context: TaskRegistrationContext | None = None,
    ) -> None:
        """初始化账本，输入为空，输出为空账本 + 关门标志关闭。"""
        if max_terminal_records < 1:
            raise ValueError("max_terminal_records must be positive")
        self._max_terminal_records = max_terminal_records
        self._registration_context = registration_context or TaskRegistrationContext()
        # task_id → TaskRecord 主账本。
        self._records: dict[str, TaskRecord] = {}
        # agent_id → 该 agent 关联的 task_id 集合（含 agent_run 与 external_process）。
        self._tasks_by_agent: dict[str, set[str]] = {}
        # 关门标志：close_registry 置 True 后 register 侧 double-check 拒绝新登记。
        # cancel_subtree 入口即置位，防「dispatched 与登记之间被打断」漏杀孤儿子。
        self._closed: bool = False
        # 已 close_cell 注销的 agent_id 集合（幂等保护 + no_live_descendants 快速判定）。
        self._closed_agents: set[str] = set()

    # ------------------------------------------------------------------
    # 登记侧
    # ------------------------------------------------------------------

    def register_run(
        self,
        task: asyncio.Task[object],
        agent_id: str,
        parent_task_id: str | None,
        *,
        thread_id: str = "",
        source: str = "agent",
        workflow_id: str | None = None,
        workflow_task_id: str | None = None,
        task_run_id: str = "",
        task_name: str = "",
        session_id: str = "",
    ) -> TaskRecord:
        """登记 agent_run task，输入为 asyncio.Task + agent_id + parent_task_id，输出为 TaskRecord。

        agent_run 无 PID（resources=[]），cancel 靶 = handle（asyncio.Task）。
        关门标志关闭时拒绝登记（防 cancel 与登记间竞态漏杀孤儿子）。
        """
        if self._closed:
            raise RuntimeError(
                f"TaskRegistry is closed; cannot register new run task (agent_id={agent_id})"
            )
        defaults = self._registration_context
        started_at = now_epoch_ms()
        record = TaskRecord(
            identity=_task_identity(
                agent_id=agent_id,
                parent_task_id=parent_task_id,
                kind="agent_run",
                thread_id=thread_id or defaults.thread_id,
                source=source if source != "agent" else defaults.source,
                workflow_id=(workflow_id if workflow_id is not None else defaults.workflow_id),
                workflow_task_id=(
                    workflow_task_id if workflow_task_id is not None else defaults.workflow_task_id
                ),
                task_run_id=task_run_id or defaults.task_run_id,
                task_name=task_name or defaults.task_name,
                session_id=session_id or defaults.session_id,
                started_at=started_at,
            ),
            status="running",
            handle=task,
            resources=[],
            updated_at=started_at,
            finished_at=None,
            error_message=None,
        )
        self._records[record.task_id] = record
        self._tasks_by_agent.setdefault(agent_id, set()).add(record.task_id)
        # 登记后挂 done 回调，回收审计字段（task 结束即写 finished_at/status）。
        _attach_done_callback(task, record, self)
        logger.debug("registered agent_run task_id=%s agent_id=%s", record.task_id, agent_id)
        return record

    def register_pending(
        self,
        agent_id: str,
        parent_task_id: str | None,
        *,
        thread_id: str = "",
        source: str = "agent",
        workflow_id: str | None = None,
        workflow_task_id: str | None = None,
        task_run_id: str = "",
        task_name: str = "",
        session_id: str = "",
    ) -> TaskRecord:
        """登记 pending TaskRecord（无 asyncio.Task 句柄），输入为 agent_id + parent_task_id，输出为 TaskRecord。

        agent-tree-v0.1 task-5：spawn 派生子 agent 时，在子 agent_loop 启动 run_task
        **之前**先原子登记一条 pending 记录（不变量「登记先于暴露」）。子 agent_loop
        启动后由 ``attach_run_task`` 把真实 asyncio.Task 绑定到该记录（status 升级
        running）。这样 cancel_subtree 与 spawn 间的竞态——cancel 发生在 spawn 返回
        与 run_task 启动之间——也能在 pending 记录上砍到靶子（关门标志兜底）。

        关门标志关闭时拒绝登记（防 cancel 与登记间竞态漏杀孤儿子）。

        Args:
            agent_id: 子 agent_id。
            parent_task_id: 父 task_id（spawn 链）；根 spawn = None。

        Returns:
            status=pending 的 TaskRecord（handle=None, kind=agent_run）。
        """
        if self._closed:
            raise RuntimeError(
                f"TaskRegistry is closed; cannot register pending task (agent_id={agent_id})"
            )
        defaults = self._registration_context
        started_at = now_epoch_ms()
        record = TaskRecord(
            identity=_task_identity(
                agent_id=agent_id,
                parent_task_id=parent_task_id,
                kind="agent_run",
                thread_id=thread_id or defaults.thread_id,
                source=source if source != "agent" else defaults.source,
                workflow_id=(workflow_id if workflow_id is not None else defaults.workflow_id),
                workflow_task_id=(
                    workflow_task_id if workflow_task_id is not None else defaults.workflow_task_id
                ),
                task_run_id=task_run_id or defaults.task_run_id,
                task_name=task_name or defaults.task_name,
                session_id=session_id or defaults.session_id,
                started_at=started_at,
            ),
            status="pending",
            handle=None,
            resources=[],
            updated_at=started_at,
            finished_at=None,
            error_message=None,
        )
        self._records[record.task_id] = record
        self._tasks_by_agent.setdefault(agent_id, set()).add(record.task_id)
        logger.debug("registered pending task_id=%s agent_id=%s", record.task_id, agent_id)
        return record

    def attach_run_task(self, task_id: str, task: asyncio.Task[object]) -> bool:
        """把真实 asyncio.Task 绑定到已登记的 pending 记录，输入为 task_id + task，输出是否绑定成功。

        agent-tree-v0.1 task-5：子 agent_loop 启动 run_task 后调用本方法，把
        ``register_pending`` 登记的 pending 记录升级为 running（handle=task，挂 done
        回调）。task_id 未知 / 记录非 pending 返回 False（调用方可改用 register_run）。

        Args:
            task_id: register_pending 返回的 task_id。
            task: 子 agent_loop 创建的 run_task。

        Returns:
            True=绑定成功；False=task_id 未知或记录非 pending（调用方 fallback）。
        """
        record = self._records.get(task_id)
        if record is None or record.status != "pending":
            return False
        record.handle = task
        record.status = "running"
        record.updated_at = now_epoch_ms()
        _attach_done_callback(task, record, self)
        logger.debug("attached run_task to task_id=%s", task_id)
        return True

    def register_external(
        self,
        pid: int,
        agent_id: str,
        parent_task_id: str | None,
        *,
        kind: PidKind = "bash",
    ) -> TaskRecord:
        """登记 external_process PID，输入为 pid + agent_id + parent_task_id，输出为 TaskRecord。

        external_process 无 asyncio.Task（handle=None），kill 靶 = resources 中的 PidHandle。
        关门标志关闭时拒绝登记（防 cancel 与登记间竞态漏杀孤儿子）。
        """
        if self._closed:
            raise RuntimeError(
                f"TaskRegistry is closed; cannot register new external process (agent_id={agent_id} pid={pid})"
            )
        started_at = now_epoch_ms()
        record = TaskRecord(
            identity=_task_identity(
                agent_id=agent_id,
                parent_task_id=parent_task_id,
                kind="external_process",
                thread_id="",
                source="external_process",
                workflow_id=None,
                workflow_task_id=None,
                task_run_id="",
                task_name="",
                session_id="",
                started_at=started_at,
            ),
            status="running",
            handle=None,
            resources=[PidHandle(pid=pid, kind=kind)],
            updated_at=started_at,
            finished_at=None,
            error_message=None,
        )
        self._records[record.task_id] = record
        self._tasks_by_agent.setdefault(agent_id, set()).add(record.task_id)
        logger.debug(
            "registered external_process task_id=%s agent_id=%s pid=%s",
            record.task_id,
            agent_id,
            pid,
        )
        return record

    # ------------------------------------------------------------------
    # 砍靶侧
    # ------------------------------------------------------------------

    async def cancel_subtree(self, agent_id: str) -> None:
        """后序砍靶：先 kill+wait external PID，再 cancel run_task。

        关键链路：收集该 agent 子树所有 TaskRecord → 按 kind 分组 →
        external_process 先（kill SIGTERM 兜底 SIGKILL + wait 收口，无僵尸）→
        agent_run 后（task.cancel，asyncio cancellation 沿 await 链传播）。
        整树关门由 :meth:`close_registry` 的调用方显式负责；局部 child 取消后
        registry 保持开放，允许 sibling 或父 agent 继续登记新任务。
        单 agent 退化形态下子树只有根节点，退化为「cancel 根 run_task」。
        幂等：已 cancel/done 的 task 跳过，已死 PID 静默吞 ProcessLookupError。
        未知 agent_id 静默无操作（幂等）。
        """
        task_ids = self._tasks_by_agent.get(agent_id, set())
        if not task_ids:
            # 未知 agent_id 或该 agent 无登记记录：静默无操作（幂等）。
            return

        # 收集该 agent 子树所有 TaskRecord（v1 单 agent 退化形态下 = 该 agent 自己的记录）。
        records: list[TaskRecord] = []
        for tid in task_ids:
            record = self._records.get(tid)
            if record is not None:
                records.append(record)

        # 后序遍历：先 external_process（kill+wait），再 agent_run（cancel task）。
        external_records = [r for r in records if r.kind == "external_process"]
        run_records = [r for r in records if r.kind == "agent_run"]

        # 第 1 段：kill+wait 所有 external PID（先叶子资源，最易泄漏僵尸）。
        for record in external_records:
            for pid_handle in record.resources:
                await _kill_and_wait_pid(pid_handle)
            self.finish_task(record.task_id, status="cancelled")

        # 第 2 段：cancel 所有 agent_run task（asyncio cancellation 沿 await 链传播）。
        cancelled_tasks: list[asyncio.Task[object]] = []
        for record in run_records:
            if record.handle is not None and not record.handle.done():
                record.handle.cancel()
                cancelled_tasks.append(record.handle)
            self.finish_task(record.task_id, status="cancelled")

        # 等待所有 cancel 的 task 真正收口（约束 16：runner 顶层吞 CancelledError）。
        # 屏蔽 CancelledError 以免污染本协程。
        for task in cancelled_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(
                    "cancelled run_task raised non-cancel error: %s",
                    type(exc).__name__,
                )

        logger.info(
            "cancel_subtree done agent_id=%s external=%d run=%d",
            agent_id,
            len(external_records),
            len(run_records),
        )

    # ------------------------------------------------------------------
    # 关门 / 查询
    # ------------------------------------------------------------------

    def close_registry(self) -> None:
        """置关门标志，输入为空，输出为账本关闭状态。

        关闭后 register_run/register_external 抛错拒绝。cancel_subtree 入口
        会自动调用此方法（防漏杀）。
        """
        self._closed = True

    @property
    def is_closed(self) -> bool:
        """返回关门标志状态，输入为空，输出为当前是否已关门。"""
        return self._closed

    def no_live_descendants(self, agent_id: str) -> bool:
        """判定 agent 是否无存活子孙，输入为 agent_id，输出为 bool。

        判定逻辑：该 agent 关联的所有 TaskRecord 都已终止（status != running/pending）
        → True（可 close_cell）；任一存活 → False。
        已 close_cell 注销的 agent 直接返回 True（幂等）。
        """
        if agent_id in self._closed_agents:
            return True
        task_ids = self._tasks_by_agent.get(agent_id, set())
        for tid in task_ids:
            record = self._records.get(tid)
            if record is not None and record.status in ("pending", "running"):
                return False
        return True

    def close_cell(self, agent_id: str) -> None:
        """注销 agent（agent_loop finally 用），输入为 agent_id，输出为注销后状态。

        v1 单 agent 退化形态下由 agent_loop finally 调用；本 task 先建签名，
        task-5 AgentManager 接入后正式启用。幂等：重复注销安全。
        """
        self._closed_agents.add(agent_id)
        logger.debug("close_cell agent_id=%s", agent_id)

    def finish_task(
        self,
        task_id: str,
        *,
        status: TerminalTaskStatus,
        error_message: str | None = None,
    ) -> bool:
        """幂等写入终态，输入为 task id/终态，输出为本次是否完成首次转换。"""
        record = self._records.get(task_id)
        if record is None or record.status in {"completed", "failed", "cancelled"}:
            return False
        finished_at = now_epoch_ms()
        record.status = status
        record.updated_at = finished_at
        record.finished_at = finished_at
        record.error_message = error_message
        self._prune_terminal_records(record.thread_id)
        return True

    def list_thread_tasks(
        self,
        thread_id: str,
        *,
        include_finished: bool = False,
        limit: int = 50,
    ) -> tuple[TaskProjection, ...]:
        """查询 thread 的不可变任务投影，输入为过滤条件，输出为 live 优先的快照。"""
        if limit < 1:
            raise ValueError("limit must be positive")
        records = [
            record
            for record in self._records.values()
            if record.thread_id == thread_id
            and (include_finished or record.status in {"pending", "running"})
        ]
        records.sort(
            key=lambda record: (
                record.status in {"pending", "running"},
                record.updated_at,
                record.task_id,
            ),
            reverse=True,
        )
        return tuple(record.project() for record in records[:limit])

    def _prune_terminal_records(self, thread_id: str) -> None:
        """只裁剪指定 thread 最旧终态，输入为 thread id，输出为 live 全量保留。"""
        terminals = [
            record
            for record in self._records.values()
            if record.thread_id == thread_id
            and record.status in {"completed", "failed", "cancelled"}
        ]
        overflow = len(terminals) - self._max_terminal_records
        if overflow <= 0:
            return
        terminals.sort(
            key=lambda record: (
                record.finished_at if record.finished_at is not None else record.updated_at
            )
        )
        for record in terminals[:overflow]:
            self._records.pop(record.task_id, None)
            agent_task_ids = self._tasks_by_agent.get(record.agent_id)
            if agent_task_ids is not None:
                agent_task_ids.discard(record.task_id)
                if not agent_task_ids:
                    self._tasks_by_agent.pop(record.agent_id, None)

    # ------------------------------------------------------------------
    # 内部：审计回写（done callback 用）
    # ------------------------------------------------------------------

    def _apply_task_done(self, record: TaskRecord, task: asyncio.Task[object]) -> None:
        """run_task 结束时回写审计字段，输入为记录与完成的 task，输出为更新后的记录。

        由 _attach_done_callback 注册的回调调用；不抛错（done callback 异常
        只写 warning，不影响 task 主流程，参照 lifecycle.py _notify 隔离模式）。
        """
        try:
            # 终态守护：cancel_subtree 等已写的终态（cancelled/failed/completed）不被
            # done-callback 覆盖。竞态场景：cancel_subtree 先 task.cancel() + _mark_finished
            # 写 cancelled，随后 asyncio 调本 done callback——若直接重算会按 task.cancelled()
            # 重写一遍（这里恰好同值），但 else 分支会覆盖 cancelled 为 completed。
            # 守护策略：终态已写则只补 finished_at（若缺），status 不动。
            if record.status in {"cancelled", "failed", "completed"}:
                if record.finished_at is None:
                    finished_at = now_epoch_ms()
                    record.finished_at = finished_at
                    record.updated_at = finished_at
                return
            if task.cancelled():
                record.status = "cancelled"
            elif task.exception() is not None:
                exc = task.exception()
                record.status = "failed"
                record.error_message = f"{type(exc).__name__}: {exc}"
            else:
                task_result = task.result()
                result_status = getattr(task_result, "status", None)
                if result_status in {"completed", "failed", "cancelled"}:
                    record.status = result_status
                    error = getattr(task_result, "error", None)
                    if error is not None:
                        record.error_message = str(error)
                else:
                    record.status = "completed"
            finished_at = now_epoch_ms()
            record.finished_at = finished_at
            record.updated_at = finished_at
            self._prune_terminal_records(record.thread_id)
        except Exception as exc:
            logger.warning(
                "TaskRegistry done callback failed for task_id=%s: %s",
                record.task_id,
                type(exc).__name__,
            )


# ---------------------------------------------------------------------------
# 内部 helper
# ---------------------------------------------------------------------------


def _new_task_id() -> str:
    """生成唯一 task_id，输入为空，输出为 uuid4().hex[:12]。"""
    import uuid

    return uuid.uuid4().hex[:12]


def _task_identity(
    *,
    agent_id: str,
    parent_task_id: str | None,
    kind: TaskKind,
    thread_id: str,
    source: str,
    workflow_id: str | None,
    workflow_task_id: str | None,
    task_run_id: str,
    task_name: str,
    session_id: str,
    started_at: int,
) -> TaskIdentity:
    """创建冻结身份，输入为登记坐标，输出为 TaskIdentity。"""
    return TaskIdentity(
        task_id=_new_task_id(),
        agent_id=agent_id,
        parent_task_id=parent_task_id,
        kind=kind,
        thread_id=thread_id.strip(),
        source=source.strip() or "agent",
        workflow_id=workflow_id.strip() if workflow_id and workflow_id.strip() else None,
        workflow_task_id=(
            workflow_task_id.strip() if workflow_task_id and workflow_task_id.strip() else None
        ),
        task_run_id=task_run_id.strip(),
        task_name=task_name.strip(),
        session_id=session_id.strip(),
        started_at=started_at,
    )


def _attach_done_callback(
    task: asyncio.Task[object],
    record: TaskRecord,
    registry: TaskRegistry,
) -> None:
    """给 run_task 挂 done 回调，输入为 task + 记录 + registry，输出为回调已注册。

    使用命名函数 + 默认参数冻结当前 record/registry（约束 18）。
    """

    def _on_done(
        t: asyncio.Task[object],
        rec: TaskRecord = record,
        reg: TaskRegistry = registry,
    ) -> None:
        reg._apply_task_done(rec, t)

    task.add_done_callback(_on_done)


def _mark_finished(record: TaskRecord, *, status: TaskStatus) -> None:
    """回写 record 终态审计字段，输入为记录与目标状态，输出为更新后的记录。

    cancel_subtree 砍靶后回写（external 与 run_task）；幂等：已终态则跳过。
    """
    if record.status in ("completed", "failed", "cancelled"):
        return
    finished_at = now_epoch_ms()
    record.status = status
    record.updated_at = finished_at
    record.finished_at = finished_at


async def _kill_and_wait_pid(pid_handle: PidHandle) -> None:
    """kill+wait 单个 PID，输入为 PidHandle，输出为进程已收口（无僵尸）。

    后序砍靶第 1 段核心：kill SIGTERM 优先，超时兜底 SIGKILL，最终 wait 收口。
    幂等：已死 PID（ProcessLookupError / ESRCH）静默吞掉。
    kill+wait 必须配对，不能只 kill 不 wait（约束 16，防僵尸）。
    """
    pid = pid_handle.pid
    # 第 1 步：SIGTERM 优雅终止。
    _try_kill(pid, signal.SIGTERM)
    # 第 2 步：等待进程退出（短超时兜底，超时升级 SIGKILL）。
    reaped = await _wait_pid_reaped(pid, timeout_s=2.0)
    if not reaped:
        # 第 3 步：SIGKILL 强制终止 + 再次 wait 收口。
        _try_kill(pid, signal.SIGKILL)
        await _wait_pid_reaped(pid, timeout_s=2.0)


def _try_kill(pid: int, sig: int) -> None:
    """对 pid 发信号，输入为 pid + signal，输出为信号已发或已死 PID 静默吞掉。

    幂等：ProcessLookupError / ESRCH 表示进程已死，静默跳过（防僵尸场景已 reap）。
    PermissionError 记 warning 但不中断（砍靶应尽量收口所有靶子）。
    """
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        # 进程已死（已 reap 或不存在）：幂等静默吞掉。
        pass
    except PermissionError as exc:
        logger.warning("kill(pid=%d, sig=%d) permission denied: %s", pid, sig, exc)


async def _wait_pid_reaped(pid: int, *, timeout_s: float) -> bool:
    """轮询等待 PID 被 reap，输入为 pid + 超时，输出为是否已 reap（True=已死）。

    用 os.kill(pid, 0) 探活：ProcessLookupError=已 reap（返回 True）；
    成功=仍存活（继续轮询）；PermissionError=进程存在但无权限（视为存活，继续轮询）。
    超时返回 False（调用方升级 SIGKILL）。
    """
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            # 进程已 reap（无僵尸）。
            return True
        except PermissionError:
            # 进程存在但无权限探活：视为存活，继续轮询。
            pass
        await asyncio.sleep(0.05)
    return False


__all__ = [
    "PidHandle",
    "PidKind",
    "TaskIdentity",
    "TaskKind",
    "TaskProjection",
    "TaskRecord",
    "TaskRegistrationContext",
    "TaskRegistry",
    "TaskStatus",
    "TerminalTaskStatus",
]
