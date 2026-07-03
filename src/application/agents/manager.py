"""AgentManager — agent 树生命周期门户（agent-tree-v0.1 模块 A · 边界类）。

功能：管理主 + 子所有 agent 的创建、销毁、查询；``spawn`` 异步派生原语（统一
异步，立即返回 ``dispatched``，子结果走 ``child_result`` Mail 回灌父 mailbox）；
``cancel_subtree`` 树级打断唯一编排点（registry 砍靶 + bump epoch + purge mailbox
+ 取消子树 pending approval future）。

作用：把主 / 子差异收敛到 :class:`AgentCell` 字段（``parent_id`` / ``lifecycle`` /
``depth``），让 spawn 只有异步一种语义——父 tool_result 立即返回 ``dispatched``，
父永不阻塞等待；子 agent（``single_shot``）的最终结果走 ``child_result`` Mail
在父下一 run 注入。这是 agent-tree-v0.1 的最后一步、主链路门户。

关键执行流程：
- ``spawn``：校验 ``parent.depth < cfg.max_spawn_depth`` → 生成 ``agent_id``（树内
  唯一）→ 建 ``single_shot`` AgentCell（depth=parent.depth+1）→ **先原子登记
  TaskRecord(pending) 再**启动子 agent_loop（``create_task``）→ 立即返回
  ``SpawnResult{child_id, dispatched}``。关门标志下 spawn 被拒（防孤儿子）。
- ``cancel_subtree``：``registry.cancel_subtree``（后序砍靶，task-3 产物）→
  ``bump_epoch`` → purge 各 mailbox 内部消息（旧 epoch 被门卫拦截）→ 取消子树
  pending approval future。
- ``close_cell``：``single_shot`` 终态且 ``registry.no_live_descendants`` 时注销
  + 关 mailbox。

关键不变量：
- **登记先于暴露**：spawn 先原子登记 TaskRecord(pending) 再返回 SpawnResult；
  cancel_subtree 入口 ``registry.close_registry`` 置关门标志拒绝新登记。
- **spawn 纯异步**：SpawnResult 固定 ``dispatched``，绝不阻塞（无 wait=True）。
- **退出条件通用式**：``single_shot 终态 ∧ registry.no_live_descendants``——
  v1 深度 1 时无子孙恒真，将来放开只改配置不改代码。

范围收窄决策（task-5，监督 agent 判定）：本边界类作为 spawn 主路径新增，**不删**
``SubAgentManager`` / subagents 包 / workflow strategies（它们深度依赖
``SubAgentTask`` 作 task spec 载体，spec 同时要求「不破坏 workflow」）。完整淘汰
``SubAgentManager`` 全家桶推迟到「workflow 收编为 policy agent」（v2）。本 task
仅新增 ``AgentManager`` 并存；SubAgentManager 标记 DEPRECATED。

事实源：``docs/spec/agent-tree-v0.1/02-module-breakdown.md``（模块 A）+
``04-data-and-state.md``（AgentCell / Mail / SpawnResult / agent_loop 伪代码）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from application.agents.cell import AgentCell
from application.agents.loop import (
    DeliverSink,
    _purge_stale_internal_mails,
    agent_loop,
)
from application.agents.registry import TaskRegistry
from core.agent_spec import AgentSpec
from core.message import Message
from core.outcome import (
    Cancelled,
    Completed,
    Exhausted,
    Failed,
    Outcome,
    deliver_failure_up,
    deliver_partial_up,
    deliver_up,
)
from core.result import Result

logger = logging.getLogger(__name__)

# SpawnResult.status 固定字面值（spawn 永不阻塞）。
DispatchedStatus = Literal["dispatched"]

# AgentManager 默认最大 spawn 深度（v1 固定 1 层，子 cell 无 spawn 工具）。
_DEFAULT_MAX_SPAWN_DEPTH = 1


# ---------------------------------------------------------------------------
# SpawnResult — spawn 同步返回值（不阻塞）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpawnResult:
    """spawn 同步返回值（固定 ``dispatched``，绝不阻塞）。

    Attributes:
        child_id: 新建子 agent_id（``uuid4().hex[:8]``）。子最终结果走
            ``child_result`` Mail（recipient=父 agent_id, sender=子 agent_id,
            task_id=spawn 的 TaskRecord.task_id）投父 mailbox。
        status: 固定 ``"dispatched"``——spawn 立即返回，子 agent_loop 后台跑；
            父永不悬空等待。
        task_id: 关联 spawn 的 TaskRecord.task_id（父消费 child_result 时可回溯
            是哪个 spawn 调用）。task-5 范围内这是登记的 pending TaskRecord。
    """

    child_id: str
    status: DispatchedStatus = "dispatched"
    task_id: str = ""


# ---------------------------------------------------------------------------
# 注入式依赖 Protocol（让 AgentManager 可单测、不硬耦合 runtime/hosts）
# ---------------------------------------------------------------------------


class SpawnRunFn(Protocol):
    """子 agent run 的执行协议（注入式依赖，封装 runtime/runner）。

    AgentManager 不 import runtime_assembly / hosts（import-linter Contract 3 分层
    禁止 application → runtime_assembly / hosts）。宿主装配层（ThreadManager）
    构造本闭包传入：封装 ``runtime.run``（或 ``bridge.run_once``），把子
    ``agent_id`` 透传给 runner（让 Event / ToolContext 带 agent_id 坐标）。

    签名对齐 :data:`application.agents.loop.RunFn`：``async (user_input, **kw) -> Result``。
    子 agent 的 session 管理 / event_sinks 注入 / agent_id 透传由本闭包承担，
    agent_loop 只拿 Result 走 Outcome 分发。
    """

    def __call__(self, user_input: str, **kw: Any) -> asyncio.Future[Result]:
        """执行一条 seed 触发的子 agent run，返回 runner 收口后的 Result。"""
        ...


@dataclass
class SpawnContext:
    """spawn 装配束：注入 run_fn + deliver_sink + agent_loop 启动所需依赖。

    让 AgentManager.spawn 不直接依赖 runtime / ThreadCell，由宿主装配层注入。
    所有字段都是 callable / sink，不持有可变全局状态。

    Attributes:
        run_fn_builder: 给定子 cell，返回该 cell 的 run_fn 闭包（封装 runtime.run，
            透传 session_id / agent_id）。
        deliver_sink_builder: 给定子 cell + spawn 的 task_id + 父 mailbox，
            返回投父 mailbox 的 DeliverSink（child_result Mail 投递）。
        current_epoch_getter: 实时读取当前树世代（从 ThreadCell.epoch 读）。
        registry: 该树的 TaskRegistry（登记子 run_task / close_cell）。
        max_spawn_depth: 最大 spawn 深度（v1 固定 1，配置真源）。
    """

    run_fn_builder: Callable[[AgentCell], SpawnRunFn]
    deliver_sink_builder: Callable[[AgentCell, str, asyncio.Queue[Any]], DeliverSink]
    current_epoch_getter: Callable[[], int]
    registry: TaskRegistry
    max_spawn_depth: int = _DEFAULT_MAX_SPAWN_DEPTH


# ---------------------------------------------------------------------------
# 子 agent DeliverSink：投父 mailbox
# ---------------------------------------------------------------------------


@dataclass
class ChildDeliverSink(DeliverSink):
    """子 agent（single_shot）的 DeliverSink：把 child_result Mail 投父 mailbox。

    子 agent 没有「推 UI」语义——run 的 Event 由 bridge 通过 event_sinks 实时推过。
    本 sink 在 run 结束（Outcome match）时构造 ``child_result`` Mail 投进父 mailbox：
    - Completed → ``deliver_up``（子最终结果回灌）
    - Cancelled(tree_wide=True) → emit_only（父也被砍，不上投）
    - Cancelled(tree_wide=False) → 投 cancelled notice（局部取消必须通知父）
    - Failed → ``deliver_failure_up``
    - Exhausted → ``deliver_partial_up``

    关键不变量：投递的 Mail.epoch = 子 run 的 run_epoch（cancel_subtree bump 后
    旧 epoch 投递被父 mailbox 门卫拦截——段3 既成事实不回滚，只拦上投）。
    """

    child: AgentCell
    task_id: str
    parent_mailbox: asyncio.Queue[Any] = field(repr=False)
    parent_agent_id: str = field(repr=False)

    def deliver_up_or_ui(
        self,
        cell: AgentCell,
        outcome: Outcome,
        *,
        result: Result,
        run_epoch: int,
    ) -> None:
        """Completed / Cancelled(tree_wide=False) / Failed / Exhausted → 投父 mailbox。"""
        mail = _build_child_result_mail(
            self.child,
            outcome,
            result=result,
            run_epoch=run_epoch,
            task_id=self.task_id,
            parent_agent_id=self.parent_agent_id,
        )
        if mail is None:
            return
        try:
            self.parent_mailbox.put_nowait(mail)
        except asyncio.QueueFull:  # pragma: no cover - 默认无界 Queue
            logger.warning(
                "parent mailbox full; child_result dropped agent_id=%s parent=%s",
                self.child.agent_id,
                self.parent_agent_id,
            )

    def emit_only(
        self,
        cell: AgentCell,
        outcome: Cancelled,
        *,
        result: Result,
        run_epoch: int,
    ) -> None:
        """Cancelled(tree_wide=True)：父也被砍，不上投，只记日志。"""
        logger.debug(
            "child emit_only (cancelled tree_wide) agent_id=%s run_epoch=%d",
            self.child.agent_id,
            run_epoch,
        )


def _build_child_result_mail(
    child: AgentCell,
    outcome: Outcome,
    *,
    result: Result,
    run_epoch: int,
    task_id: str,
    parent_agent_id: str,
) -> Any:
    """按 Outcome 类型构造 child_result Mail（投父 mailbox），返回 Mail 或 None。

    Completed → deliver_up；Failed → deliver_failure_up；Exhausted →
    deliver_partial_up；Cancelled(tree_wide=False) → 退化 notice（deliver_cancelled_up
    留 task-4 消费侧实现，这里手工构造 notice Mail）。tree_wide=True 走 emit_only
    不该进本函数（返回 None 兜底）。
    """
    if isinstance(outcome, Completed):
        return deliver_up(
            outcome,
            result=result,
            sender=child.agent_id,
            recipient_agent_id=parent_agent_id,
            task_id=task_id,
            epoch=run_epoch,
        )
    if isinstance(outcome, Failed):
        return deliver_failure_up(
            outcome,
            sender=child.agent_id,
            recipient_agent_id=parent_agent_id,
            task_id=task_id,
            epoch=run_epoch,
        )
    if isinstance(outcome, Exhausted):
        return deliver_partial_up(
            outcome,
            result=result,
            sender=child.agent_id,
            recipient_agent_id=parent_agent_id,
            task_id=task_id,
            epoch=run_epoch,
        )
    if isinstance(outcome, Cancelled):
        # tree_wide=False 走 deliver_up_or_ui → 本分支构造 cancelled notice Mail。
        # tree_wide=True 走 emit_only 不该进本函数；兜底返回 None。
        if outcome.tree_wide:
            return None
        from core.mail import Mail

        return Mail(
            kind="child_result",
            sender=child.agent_id,
            recipient_agent_id=parent_agent_id,
            task_id=task_id,
            epoch=run_epoch,
            payload=Message(
                role="assistant",
                content=f"[child cancelled: {outcome.reason}]",
                metadata={
                    "child_cancel_reason": outcome.reason,
                    "child_error_class": "cancelled",
                },
            ),
        )
    # 兜底（Outcome 封闭集合外，理论不可达）。
    return None


# ---------------------------------------------------------------------------
# AgentManager 边界类
# ---------------------------------------------------------------------------


class AgentManager:
    """主 + 子 agent 生命周期门户（资源 / 生命周期域边界类）。

    spawn 原语唯一入口，cancel_subtree 树级打断唯一编排点。主 / 子零代码分支——
    差异仅在 :class:`AgentCell` 字段（``parent_id`` / ``lifecycle`` / ``depth``）。

    范围收窄（task-5）：本类作为 spawn 主路径新增，与 :class:`SubAgentManager`
    （workflow 兼容路径）并存。完整淘汰 SubAgentManager 全家桶推迟到 v2。

    Attributes:
        _ctx: spawn 装配束（run_fn / deliver_sink / epoch getter / registry）。
        _cells: agent_id → AgentCell 注册表（树内所有 agent）。
        _children: agent_id → set[child_id] 父子索引（spawn 注册，close_cell 注销）。
        _approval_canceller: 取消子树 pending approval future 的 callable（注入，
            None 时不取消——单测可省略）。键为 agent_id，取消该 agent 名下 pending。
        _loop_tasks: agent_loop 协程集合（shutdown / cancel_subtree 收口）。
    """

    def __init__(
        self,
        ctx: SpawnContext,
        *,
        approval_canceller: Callable[[str], int] | None = None,
    ) -> None:
        """构造 AgentManager，输入为 spawn 装配束，输出为可用门户。

        Args:
            ctx: spawn 装配束（run_fn_builder / deliver_sink_builder / epoch getter /
                registry / max_spawn_depth）。
            approval_canceller: 可选，取消某 agent_id 名下 pending approval future
                的 callable，返回取消数。cancel_subtree 编排时调用；None 时跳过
                （单测可省略，生产由 ThreadCell.approval_manager 注入）。
        """
        self._ctx = ctx
        self._cells: dict[str, AgentCell] = {}
        self._children: dict[str, set[str]] = {}
        self._approval_canceller = approval_canceller
        self._loop_tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------
    # spawn（异步派生原语）
    # ------------------------------------------------------------------

    def spawn(
        self,
        parent_cell: AgentCell,
        spec: AgentSpec,
        skill_names: tuple[str, ...],
        seed_message: Message,
        *,
        cwd: str,
        role_id: str | None,
        session_id: str | None = None,
    ) -> SpawnResult:
        """异步派生子 agent（统一异步，立即返回 dispatched）。

        关键链路：校验深度 → 生成 agent_id（树内唯一）→ 建 single_shot AgentCell
        → 先登记 TaskRecord(pending) 再启动 agent_loop → 立即返回 SpawnResult。
        子最终结果走 child_result Mail 投父 mailbox（父下一 run 注入）。

        Args:
            parent_cell: 父 AgentCell（主 agent / 已有子 agent）。校验
                ``parent.depth < max_spawn_depth``。
            spec: 子 agent 的 :class:`AgentSpec`（独立 name / instructions / tools）。
            skill_names: 子 agent 启用技能名元组。
            seed_message: 子 agent 首条输入（投子 mailbox 作 user_message，epoch=0
                不过门卫）。
            cwd: 子 agent 工作目录（``agents/<task_run_id>/work/``，非父 cwd）。
            role_id: 子 agent 角色身份查询键（主 agent=None，子=AgentRolePreset 键）。
            session_id: 子 agent 独立 session id；None 时自动生成。

        Returns:
            SpawnResult{child_id, "dispatched", task_id}。

        Raises:
            SpawnRejected: 深度超限 / registry 关门（防孤儿子）/ agent_id 树内重复。
        """
        max_depth = self._ctx.max_spawn_depth
        if parent_cell.depth >= max_depth:
            raise SpawnRejected(
                f"spawn depth exceeded: parent.depth={parent_cell.depth} "
                f">= max_spawn_depth={max_depth}"
            )
        # 关门标志下 spawn 被拒（防 dispatched 与登记间被打断漏杀孤儿子）。
        if self._ctx.registry.is_closed:
            raise SpawnRejected("registry is closed; cannot spawn (tree is being torn down)")

        # 生成 agent_id（uuid4().hex[:8]），校验树内唯一。
        agent_id = uuid.uuid4().hex[:8]
        if agent_id in self._cells:
            # 极低概率碰撞（8 hex）；兜底重生成（最多 8 次）。
            for _ in range(8):
                agent_id = uuid.uuid4().hex[:8]
                if agent_id not in self._cells:
                    break
            else:
                raise SpawnRejected("agent_id collision after retries")

        # 建子 AgentCell（single_shot, depth=parent.depth+1）。
        child = AgentCell(
            agent_id=agent_id,
            parent_id=parent_cell.agent_id,
            child_ids=[],
            depth=parent_cell.depth + 1,
            spec=spec,
            skill_names=tuple(skill_names),
            session_id=session_id or f"{parent_cell.session_id}-{agent_id}",
            cwd=cwd,
            role_id=role_id,
            lifecycle="single_shot",
            state="idle",
        )

        # 投子 mailbox 首条 seed（user_message，epoch=0 不过门卫）。
        child.mailbox.put_nowait(_seed_mail(seed_message, child.agent_id))

        # 先原子登记 TaskRecord(pending) 再启动 agent_loop（不变量：登记先于暴露）。
        # register_pending 在 run_task 启动前就登记一条 pending 记录：cancel_subtree
        # 与 spawn 间的竞态（cancel 发生在 spawn 返回与 run_task 启动之间）也能砍到靶子
        # （关门标志兜底 + no_live_descendants 把 pending 视为存活）。
        record = self._ctx.registry.register_pending(
            agent_id=child.agent_id,
            parent_task_id=None,
        )
        task_id = record.task_id

        # 注册进 AgentCell 注册表 + 父子索引。
        self._cells[child.agent_id] = child
        self._children.setdefault(parent_cell.agent_id, set()).add(child.agent_id)
        parent_cell.child_ids.append(child.agent_id)

        # 启动子 agent_loop（run_fn 封装 runtime，deliver_sink 投父 mailbox）。
        run_fn = self._ctx.run_fn_builder(child)
        deliver_sink = self._ctx.deliver_sink_builder(child, task_id, parent_cell.mailbox)
        loop_task = asyncio.create_task(
            agent_loop(
                child,
                run_fn=run_fn,  # type: ignore[arg-type]
                registry=self._ctx.registry,
                current_epoch_getter=self._ctx.current_epoch_getter,
                deliver_sink=deliver_sink,
                parent_task_id=None,
                conversation_id=parent_cell.session_id,
                attach_task_id=task_id,
            ),
            name=f"agent-loop-child-{child.agent_id}",
        )
        self._loop_tasks.add(loop_task)
        loop_task.add_done_callback(self._loop_tasks.discard)
        child.run_task = loop_task  # cancel 靶子（含 agent_loop 协程）

        logger.info(
            "spawn dispatched child agent_id=%s parent=%s depth=%d task_id=%s",
            child.agent_id,
            parent_cell.agent_id,
            child.depth,
            task_id,
        )
        return SpawnResult(child_id=child.agent_id, status="dispatched", task_id=task_id)

    # ------------------------------------------------------------------
    # cancel_subtree（树级打断唯一编排点）
    # ------------------------------------------------------------------

    async def cancel_subtree(self, agent_id: str) -> None:
        """树级打断：registry 砍靶 + bump epoch + purge mailbox + 取消 pending future。

        关键链路（顺序）：
        1. ``registry.close_registry``（置关门标志，防新登记漏杀孤儿子）。
        2. 遍历子树各 agent（后序：先叶子后根），对每个调 ``registry.cancel_subtree``
           （task-3 砍该 agent 名下 run_task + bash PID kill+wait 兜底）。
           注：task-3 的 registry.cancel_subtree 是单 agent 退化形态（只砍该 agent_id
           名下记录，不递归子 agent），多 agent 树遍历由本方法负责。
        3. 读 bump 后的 epoch（由调用方 ThreadManager.bump_epoch 执行；本方法读最新值
           做 purge）。
        4. purge 各子树 mailbox 内部消息（旧 epoch 被门卫拦截，段1 兜底）。
        5. 取消子树 pending approval future（approval_canceller）。

        幂等：已终态的子树无副作用。未知 agent_id 静默无操作。

        Args:
            agent_id: 要打断的子树根 agent_id（通常是主 agent）。
        """
        # 1. 关门标志（防新登记漏杀孤儿子）。
        self._ctx.registry.close_registry()

        # 2. 后序遍历子树各 agent，逐个砍靶（task-3 registry.cancel_subtree 是单 agent
        # 退化形态，只砍该 agent_id 名下记录；多 agent 树遍历由本方法负责）。
        # 后序：先叶子后根，确保子 agent 先收口再砍父（避免孤儿 run_task）。
        descendants = self._descendants_inclusive(agent_id)
        for descendant_id in reversed(descendants):
            try:
                await self._ctx.registry.cancel_subtree(descendant_id)
            except Exception:
                logger.warning(
                    "cancel_subtree registry.cancel_subtree failed for agent_id=%s",
                    descendant_id,
                    exc_info=True,
                )

        # 3. 读 bump 后的 epoch（调用方先 bump_epoch，本方法读最新值做 purge）。
        current_epoch = self._ctx.current_epoch_getter()

        # 4. purge 子树各 cell 的 mailbox 旧世代内部消息（段1 兜底）。
        for descendant_id in descendants:
            cell = self._cells.get(descendant_id)
            if cell is not None:
                _purge_stale_internal_mails(cell, current_epoch)

        # 4. 取消子树 pending approval future。
        if self._approval_canceller is not None:
            cancelled = 0
            for descendant_id in self._descendants_inclusive(agent_id):
                cancelled += self._approval_canceller(descendant_id)
            if cancelled:
                logger.info(
                    "cancel_subtree cancelled %d pending approvals for agent_id=%s",
                    cancelled,
                    agent_id,
                )

        logger.info("cancel_subtree done agent_id=%s", agent_id)

    # ------------------------------------------------------------------
    # AgentCell 注册表 + 查询
    # ------------------------------------------------------------------

    def get_agent(self, agent_id: str) -> AgentCell | None:
        """查询单个 agent，输入为 agent_id，输出为 AgentCell 或 None（未知）。"""
        return self._cells.get(agent_id)

    def list_agents(self) -> list[AgentCell]:
        """列出树内所有 agent，输入为空，输出为 AgentCell 列表。"""
        return list(self._cells.values())

    def list_children(self, agent_id: str) -> list[AgentCell]:
        """列出某 agent 的直接子 agent，输入为 agent_id，输出为子 AgentCell 列表。"""
        child_ids = self._children.get(agent_id, set())
        children = [self._cells[cid] for cid in child_ids if cid in self._cells]
        return children

    def close_cell(self, agent_id: str) -> None:
        """注销 agent（single_shot 终态且无存活子孙时），输入为 agent_id，输出注销后状态。

        退出条件通用式：``single_shot 终态 ∧ registry.no_live_descendants``。
        v1 深度 1 时子 agent 无子孙恒真，将来放开只改配置不改代码。
        注销：从 _cells / 父 _children 移除；registry.close_cell（task-3 幂等）。
        幂等：重复注销安全。
        """
        cell = self._cells.get(agent_id)
        if cell is None:
            return
        # 校验退出条件（single_shot 终态 + 无存活子孙）。
        if cell.lifecycle != "single_shot":
            return  # persistent agent 不进 closed
        if not self._ctx.registry.no_live_descendants(agent_id):
            return  # 仍有存活子孙，不注销

        self._cells.pop(agent_id, None)
        # 从父的 _children 索引移除。
        if cell.parent_id is not None:
            siblings = self._children.get(cell.parent_id)
            if siblings is not None:
                siblings.discard(agent_id)
                if not siblings:
                    self._children.pop(cell.parent_id, None)
        # 父 cell 的 child_ids 同步移除。
        parent = self._cells.get(cell.parent_id) if cell.parent_id else None
        if parent is not None and agent_id in parent.child_ids:
            parent.child_ids.remove(agent_id)
        self._ctx.registry.close_cell(agent_id)
        logger.debug("close_cell agent_id=%s", agent_id)

    # ------------------------------------------------------------------
    # 内部 helper
    # ------------------------------------------------------------------

    def _descendants_inclusive(self, agent_id: str) -> list[str]:
        """收集某 agent 及其所有子孙 agent_id（BFS），输入为 agent_id，输出为 id 列表。

        包含 agent_id 自身（cancel_subtree 砍整棵子树 + purge 所有 mailbox）。
        """
        result: list[str] = []
        queue = [agent_id]
        seen: set[str] = set()
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            result.append(current)
            queue.extend(self._children.get(current, set()))
        return result


# ---------------------------------------------------------------------------
# SpawnRejected 异常（spawn 拒绝不打断父 run）
# ---------------------------------------------------------------------------


class SpawnRejected(Exception):
    """spawn 被拒绝（深度超限 / registry 关门 / agent_id 碰撞）。

    工具入口捕获本异常，返回拒绝 tool_result（不打断父 run）。
    """


# ---------------------------------------------------------------------------
# 内部 helper
# ---------------------------------------------------------------------------


def _new_task_id() -> str:
    """生成唯一 task_id（``uuid4().hex[:12]``），输入为空，输出为 12 位 hex。"""
    return uuid.uuid4().hex[:12]


def _seed_mail(seed: Message, recipient_agent_id: str) -> Any:
    """构造子 agent 的首条 seed Mail（user_message，epoch=0 不过门卫）。

    输入为 seed Message + 子 agent_id，输出为 Mail（kind=user_message, sender="parent",
    recipient=子 agent_id, task_id="", epoch=0）。子 agent_loop 从队头取此 mail 启动首 run。
    """
    from core.mail import Mail

    return Mail(
        kind="user_message",
        sender="parent",
        recipient_agent_id=recipient_agent_id,
        task_id="",
        epoch=0,
        payload=seed,
    )


__all__ = [
    "AgentManager",
    "ChildDeliverSink",
    "DispatchedStatus",
    "SpawnContext",
    "SpawnRejected",
    "SpawnResult",
    "SpawnRunFn",
]
