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

workflow child 与普通 spawn child 共用本门户，生命周期事实统一写入 TaskRegistry。

事实源：``docs/spec/agent-tree-v0.1/02-module-breakdown.md``（模块 A）+
``04-data-and-state.md``（AgentCell / Mail / SpawnResult / agent_loop 伪代码）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Literal

from application.agents.cell import AgentCell, make_root_agent_cell
from application.agents.loop import (
    DeliverSink,
    MailRunBridge,
    _purge_stale_internal_mails,
    agent_loop,
)
from application.agents.registry import TaskProjection, TaskRegistry
from application.agents.subagent_tools import SpawnAgentRequest
from application.tool_scope import (
    clip_child_tool_snapshot,
    resolve_tool_snapshot,
)
from core.agent_spec import AgentSpec
from core.contracts import SteerRequest, Tool, ToolLookup
from core.mail import Mail
from core.message import Message
from core.outcome import Disposition, build_mail
from core.result import Result

logger = logging.getLogger(__name__)

# SpawnResult.status 固定字面值（spawn 永不阻塞）。
DispatchedStatus = Literal["dispatched"]

# AgentManager 默认最大 spawn 深度（v1 固定 1 层，子 cell 无 spawn 工具）。
_DEFAULT_MAX_SPAWN_DEPTH = 1


class SubmitMode(StrEnum):
    """``AgentManager.submit`` 的投递模式(字符串枚举,序列化为字面值)。

    用枚举而非裸字符串:调用方传 ``SubmitMode.QUEUE`` 而非 ``"queue"``,拼错
    在编辑器/mypy 立刻报错,不会静默走错分支。继承 ``str`` 让它 JSON 可序列化、
    与历史字符串字面值 ``"queue"``/``"immediate"`` 比较时仍相等(向后兼容)。
    """

    #: 排队独立新 run:构造 user_message Mail 投 root.mailbox,agent_loop 消费后跑。
    QUEUE = "queue"
    #: 立即注入当前活跃 run:调 steer_fn 把文本注入 runner 的 turn 边界(不打断)。
    IMMEDIATE = "immediate"


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
# 注入式依赖（让 AgentManager 可单测、不硬耦合 runtime/hosts）
# ---------------------------------------------------------------------------


@dataclass
class SpawnContext:
    """spawn 装配束：注入 mail_run_bridge + deliver_sink + agent_loop 启动所需依赖。

    让 AgentManager.spawn 不直接依赖 runtime / ThreadCell，由宿主装配层注入。
    所有字段都是 callable / sink，不持有可变全局状态。

    Attributes:
        mail_run_bridge_builder: 给定子 cell，返回该 cell 的 mail_run_bridge 闭包（封装
            runtime.run，透传 session_id / agent_id）。
        deliver_sink_builder: 给定子 cell + spawn 的 task_id + 父 mailbox，
            返回投父 mailbox 的 DeliverSink（child_result Mail 投递）。
        current_epoch_getter: 实时读取当前树世代（从 ThreadCell.epoch 读）。
        registry: 该树的 TaskRegistry（登记子 run_task / close_cell）。
        max_spawn_depth: 最大 spawn 深度（v1 固定 1，配置真源）。
    """

    mail_run_bridge_builder: Callable[[AgentCell], MailRunBridge]
    deliver_sink_builder: Callable[[AgentCell, str, asyncio.Queue[Any]], DeliverSink]
    current_epoch_getter: Callable[[], int]
    registry: TaskRegistry
    max_spawn_depth: int = _DEFAULT_MAX_SPAWN_DEPTH
    tool_lookup: ToolLookup | None = None


# ---------------------------------------------------------------------------
# 子 agent DeliverSink：投父 mailbox
# ---------------------------------------------------------------------------


@dataclass
class ChildDeliverSink(DeliverSink):
    """子 agent（single_shot）的 DeliverSink：把 child_result Mail 投父 mailbox。

    子 agent 没有「推 UI」语义——run 的 Event 由 bridge 通过 event_sinks 实时推过。
    本 sink 在 run 结束时按 :class:`Disposition` 的 ``action`` 二选一处理：
    - ``action == "deliver_up"``（completed / 预算耗尽 / 失败 / 局部 cancel）→
      ``deliver_up_or_ui`` 调 ``build_mail`` 构造 child_result Mail 投父 mailbox，
      payload 里带 reason 让父 agent 区分来源。
    - ``action == "emit_only"``（树级取消：tree_wide=True 的 cancel 类 reason）→
      ``emit_only`` 父也被砍，不上投只记日志。

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
        disposition: Disposition,
        *,
        result: Result,
        run_epoch: int,
    ) -> None:
        """action=deliver_up → build_mail 构造 child_result Mail 投父 mailbox。"""
        mail = _build_child_result_mail(
            self.child,
            disposition,
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
        disposition: Disposition,
        *,
        result: Result,
        run_epoch: int,
    ) -> None:
        """action=emit_only（树级取消）：父也被砍，不上投，只记日志。"""
        logger.debug(
            "child emit_only (cancelled tree_wide) agent_id=%s reason=%s run_epoch=%d",
            self.child.agent_id,
            disposition.reason,
            run_epoch,
        )


def _build_child_result_mail(
    child: AgentCell,
    disposition: Disposition,
    *,
    result: Result,
    run_epoch: int,
    task_id: str,
    parent_agent_id: str,
) -> Mail | None:
    """按 Disposition 构造 child_result Mail（投父 mailbox），返回 Mail 或 None。

    action=emit_only（树级取消）→ 返回 None（不该上投，调用方 ChildDeliverSink
        的 emit_only 已处理，不应进本函数）。
    action=deliver_up → build_mail 按 reason 构造 payload。
    """
    if disposition.action == "emit_only":
        return None  # 树级取消不上投；兜底
    return build_mail(
        disposition,
        result=result,
        sender=child.agent_id,
        recipient_agent_id=parent_agent_id,
        task_id=task_id,
        epoch=run_epoch,
    )


# ---------------------------------------------------------------------------
# AgentManager 边界类
# ---------------------------------------------------------------------------


class AgentManager:
    """主 + 子 agent 生命周期门户（资源 / 生命周期域边界类）。

    spawn 原语唯一入口，cancel_subtree 树级打断唯一编排点。主 / 子零代码分支——
    差异仅在 :class:`AgentCell` 字段（``parent_id`` / ``lifecycle`` / ``depth``）。

    Attributes:
        _ctx: spawn 装配束(mail_run_bridge / deliver_sink / epoch getter / registry)。
        _cells: agent_id → AgentCell 注册表(树内所有 agent)。
        _children: agent_id → set[child_id] 父子索引(spawn 注册,close_cell 注销)。
        _approval_canceller: 取消子树 pending approval future 的 callable(注入,
            None 时不取消——单测可省略)。键为 agent_id,取消该 agent 名下 pending。
        _loop_tasks: agent_loop 协程集合(shutdown / cancel_subtree 收口)。
        _root_agent_id: 主 agent(根 cell)的 agent_id。boot_root 装配后记住唯一根,
            submit/interrupt/teardown_root 不传 id(默认操作根)。None=未装配。
        _epoch: CLI 单 agent 退化形态的 epoch 自持计数器(interrupt 时 bump)。
            Web 场景从 SpawnContext.current_epoch_getter 注入读 ThreadCell.epoch,
            本字段仅 CLI bridge 用 boot_root 路径时生效。
        _steer_fn: submit(mode="immediate") 调用的 steer 函数(注入,避免 AgentManager
            import runtime)。bridge 传 runtime.steer（签名 (session_id, SteerRequest) -> bool）;
            None 时 submit(immediate) 永远返回 False(回落 queued)。
    """

    def __init__(
        self,
        ctx: SpawnContext,
        *,
        approval_canceller: Callable[[str], int] | None = None,
    ) -> None:
        """构造 AgentManager,输入为 spawn 装配束,输出为可用门户。

        Args:
            ctx: spawn 装配束(mail_run_bridge_builder / deliver_sink_builder / epoch getter /
                registry / max_spawn_depth)。
            approval_canceller: 可选,取消某 agent_id 名下 pending approval future
                的 callable,返回取消数。cancel_subtree 编排时调用;None 时跳过
                (单测可省略,生产由 ThreadCell.approval_manager 注入)。
        """
        self._ctx = ctx
        self._cells: dict[str, AgentCell] = {}
        self._children: dict[str, set[str]] = {}
        self._approval_canceller = approval_canceller
        self._loop_tasks: set[asyncio.Task[Any]] = set()
        # 主 agent(root)装配态:boot_root 装配后记住唯一根,submit/interrupt/teardown_root
        # 默认操作它。None=未装配(bridge 首次 run_once 时懒 boot_root)。
        self._root_agent_id: str | None = None
        # CLI 单 agent 退化形态的 epoch 自持(interrupt bump);Web 从 ctx 注入读 ThreadCell.epoch。
        self._epoch: int = 0
        # submit(immediate) 的 steer 函数注入(避免 import runtime);None 时永远 False。
        # 签名与 Runner.steer / SessionEngine.steer 对齐：(session_id, SteerRequest) -> bool。
        self._steer_fn: Callable[[str, SteerRequest], bool] | None = None

    @property
    def registry(self) -> TaskRegistry:
        """返回当前 agent 树任务注册表。

        输入为空；输出为 AgentManager 装配时注入的 TaskRegistry。宿主层用它判断
        tree interrupt 后的关门状态，不直接拆 SpawnContext 私有字段。
        """
        return self._ctx.registry

    @property
    def root_agent_id(self) -> str | None:
        """返回已 boot 的 root agent id，输入为空，输出为稳定父 identity 或 None。"""
        return self._root_agent_id

    # ------------------------------------------------------------------
    # 主 agent(root)装配 + 投递 + 打断(CLI bridge 主链路)
    # ------------------------------------------------------------------

    def boot_root(
        self,
        *,
        spec: AgentSpec,
        session_id: str,
        mail_run_bridge: MailRunBridge,
        deliver_sink: DeliverSink,
        steer_fn: Callable[[str, SteerRequest], bool] | None = None,
        enabled_tools: tuple[Tool, ...] | None = None,
    ) -> str:
        """装配 persistent 主 agent cell + 启动常驻 agent_loop,返回 root_agent_id。

        HostDispatcher 负责调用 boot_root + submit/interrupt；AgentManager 负责
        root cell、registry、loop 三件套。

        复用现有 _cells 注册表 + _loop_tasks 集合(spawn 路径用的同一套)。主 agent 是
        persistent 根节点(parent_id=None, depth=0),与 spawn 的 single_shot 子 agent
        共存于同一棵树——主 agent 是树根,子 agent 是叶子。

        Args:
            spec: 主 agent 的 AgentSpec(runtime.agent_spec)。
            session_id: 绑定的 session id。
            mail_run_bridge: 主 agent run 的执行闭包(封装 runtime.run,透传 session_id)。
            deliver_sink: 上投/UI 推送的 sink(bridge 传 _BridgeDeliverSink 把 Result
                回传给阻塞的 run_once;Web 传 RootAgentDeliverSink)。
            steer_fn: submit(mode="immediate") 调用的 steer 函数(bridge 传
                runtime.steer，签名 (session_id, SteerRequest) -> bool)。None 时
                submit(immediate) 永远返回 False(回落 queued)。注入式依赖,避免 AgentManager import runtime。
            enabled_tools: 主 Agent 默认 run 的实际工具快照，供后续 child 单调裁剪。

        Returns:
            root_agent_id(后续 submit/interrupt/teardown_root 默认操作它)。

        Raises:
            RuntimeError: 已 boot 过 root(主 agent 唯一;重建走 teardown_root 再 boot)。
        """
        if self._root_agent_id is not None:
            raise RuntimeError(
                f"root agent already booted (agent_id={self._root_agent_id}); "
                "call teardown_root before boot_root again"
            )

        root = make_root_agent_cell(
            spec=spec,
            session_id=session_id,
            enabled_tools=enabled_tools,
        )
        self._cells[root.agent_id] = root
        self._root_agent_id = root.agent_id
        self._steer_fn = steer_fn

        # 启动常驻 agent_loop(与 spawn 的子 agent_loop 共用同一套启动模式)。
        loop_task = asyncio.create_task(
            agent_loop(
                root,
                mail_run_bridge=mail_run_bridge,
                registry=self._ctx.registry,
                current_epoch_getter=self._ctx.current_epoch_getter,
                deliver_sink=deliver_sink,
                conversation_id=session_id,
            ),
            name=f"agent-loop-root-{root.agent_id}",
        )
        self._loop_tasks.add(loop_task)
        loop_task.add_done_callback(self._loop_tasks.discard)

        logger.info("boot_root dispatched agent_id=%s session_id=%s", root.agent_id, session_id)
        return root.agent_id

    def submit(
        self,
        text: str,
        *,
        mode: SubmitMode,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """投递用户输入到 root agent。

        统一入口,由 mode 决定走排队还是立即注入:

        - :attr:`SubmitMode.QUEUE`:构造 ``user_message`` Mail 投 ``root.mailbox``。返回 True
          (投递完成)。**Future 不由本方法创建**——同步 Result 契约胶水(``_BridgeDeliverSink``
          + FIFO deque)是 bridge 独有职责(A2 方案核心),AgentManager 不掺和。调用方
          (bridge run_once)自己创 future + append FIFO + await。
        - :attr:`SubmitMode.IMMEDIATE`:调 boot_root 注入的 ``steer_fn(session_id, SteerRequest)``,
          返回其结果。True=命中活跃 run 并写入 steer buffer;False=无活跃 run 或
          run 收尾期注不进,调用方回落 queued 路径。

        Args:
            text: 用户输入文本。
            mode: :class:`SubmitMode.QUEUE`(排队独立新 run) 或
                :class:`SubmitMode.IMMEDIATE`(注入当前活跃 run)。用枚举不用裸字符串,
                拼错在编辑器/mypy 立刻报错。
            metadata: QUEUE 模式写入 ``Message.user(...).metadata`` 的结构化参数。
                IMMEDIATE 模式只注入文本，复杂输入由调用方留在队列路径处理。

        Returns:
            queue → ``True``(投递完成);immediate → ``bool``。

        Raises:
            RuntimeError: root 未 boot(先 boot_root)。
        """
        if self._root_agent_id is None:
            raise RuntimeError("root agent not booted; call boot_root first")
        root = self._cells.get(self._root_agent_id)
        if root is None:
            raise RuntimeError(f"root agent missing from registry (agent_id={self._root_agent_id})")

        if mode is SubmitMode.IMMEDIATE:
            if self._steer_fn is None:
                return False
            return self._steer_fn(root.session_id, SteerRequest(text=text))

        # queue:投 user_message Mail(epoch=0 永不过门卫),agent_loop 消费后调 mail_run_bridge。
        mail = Mail(
            kind="user_message",
            sender="user",
            recipient_agent_id=root.agent_id,
            task_id="",
            epoch=0,
            payload=Message.user(text, metadata=metadata or {}),
        )
        root.mailbox.put_nowait(mail)
        return True

    async def interrupt(self) -> None:
        """打断当前 run:cancel_subtree(root.agent_id) + epoch bump。

        由 HostDispatcher 调用。复用现有 cancel_subtree(后序砍靶 +
        close_registry)。epoch bump 让消费侧门卫丢弃旧世代内部投递。

        cancel_subtree 会永久关闭 registry(再 register_run 会 raise),因此 interrupt
        后若要继续发消息,调用方需调 teardown_root 拆掉这套,再 boot_root 重建。
        """
        if self._root_agent_id is None:
            return
        self._epoch += 1
        await self.cancel_subtree(self._root_agent_id)

    async def teardown_root(self) -> None:
        """拆掉 root cell + loop_task + 注册表项,让下次 boot_root 重建全新一套。

        供 HostDispatcher.reset_for_reuse 调用(interrupt 后 registry 已关闭,不能复用)。
        调用方可弃用整个 AgentManager 实例重建一个；本方法支持复用同一个
        AgentManager 实例(保留 _epoch 单调递增的语义连续)。

        做的事:
        1. cancel 当前树内全部 agent_loop task，防止子 agent 在 bridge 关闭后继续跑。
        2. 从 _cells / _children / _loop_tasks 移除树态。
        3. 清空 _root_agent_id / _steer_fn(下次 boot_root 重新注入)。
        4. epoch 不清零(自持单调递增,interrupt 已 bump,语义连续)。

        幂等:未 boot 过 root 时直接返回。
        """
        if self._root_agent_id is None:
            return
        # 停当前 manager 持有的全部 agent_loop task。run_loop drain=True 已等待父 run
        # 收口；这里兜底取消仍在后台跑的 single_shot 子 agent，避免 runtime 关闭后孤儿任务
        # 继续访问 provider / session。
        for task in list(self._loop_tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            self._loop_tasks.discard(task)
        self._cells.clear()
        self._children.clear()
        self._root_agent_id = None
        self._steer_fn = None

    # ------------------------------------------------------------------
    # spawn（异步派生原语）
    # ------------------------------------------------------------------

    def spawn(self, request: SpawnAgentRequest) -> SpawnResult:
        """异步派生子 agent（统一异步，立即返回 dispatched）。

        关键链路：校验深度 → 生成 agent_id（树内唯一）→ 建 single_shot AgentCell
        → 先登记 TaskRecord(pending) 再启动 agent_loop → 立即返回 SpawnResult。
        子最终结果走 child_result Mail 投父 mailbox（父下一 run 注入）。

        Args:
            request: 子 agent 创建的唯一内部合同。``parent_agent_id`` 用于查父
                AgentCell；``spec`` / ``seed_message`` / ``cwd`` / ``role_id`` 写入子
                AgentCell；run 覆盖字段写入子 cell 供 host bridge 执行时读取；
                ``child_session_id`` 可指定 workflow deterministic session；
                ``parent_task_id`` 透传给 TaskRegistry 形成父链。

        Returns:
            SpawnResult{child_id, "dispatched", task_id}。

        Raises:
            SpawnRejected: 深度超限 / registry 关门（防孤儿子）/ agent_id 树内重复。
        """
        parent_cell = self._cells.get(request.parent_agent_id)
        if parent_cell is None:
            raise SpawnRejected(f"parent agent not found for agent_id={request.parent_agent_id!r}")
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

        parent_tools = self._parent_tool_snapshot(parent_cell)
        effective_tools = clip_child_tool_snapshot(
            parent_tools=parent_tools,
            requested_tool_names=request.requested_tool_names,
            scope_allowed_tool_names=request.scope_allowed_tool_names,
            requested_tools=request.enabled_tools,
        )
        effective_spec = replace(
            request.spec,
            tool_names=tuple(tool.name for tool in effective_tools),
        )

        # 建子 AgentCell（single_shot, depth=parent.depth+1）。
        child = AgentCell(
            agent_id=agent_id,
            parent_id=parent_cell.agent_id,
            child_ids=[],
            depth=parent_cell.depth + 1,
            spec=effective_spec,
            skill_names=tuple(request.skill_names),
            session_id=request.child_session_id or f"{parent_cell.session_id}-{agent_id}",
            cwd=request.cwd,
            role_id=request.role_id,
            lifecycle="single_shot",
            state="idle",
            run_enabled_tools=effective_tools,
            run_lifecycle_hooks=request.lifecycle_hooks,
            run_max_tokens=request.max_tokens,
            run_temperature=request.temperature,
            run_timeout_seconds=request.timeout_seconds,
            run_llm_request_metadata=dict(request.llm_request_metadata),
        )

        # 投子 mailbox 首条 input_message（user_message，epoch=0 不过门卫）。
        child.mailbox.put_nowait(_build_initial_mail(request.seed_message, child.agent_id))

        # 先原子登记 TaskRecord(pending) 再启动 agent_loop（不变量：登记先于暴露）。
        # register_pending 在 run_task 启动前就登记一条 pending 记录：cancel_subtree
        # 与 spawn 间的竞态（cancel 发生在 spawn 返回与 run_task 启动之间）也能砍到靶子
        # （关门标志兜底 + no_live_descendants 把 pending 视为存活）。
        record = self._ctx.registry.register_pending(
            agent_id=child.agent_id,
            parent_task_id=request.parent_task_id,
            thread_id=(
                _mapping_text(request.metadata, "parent_session_id") or parent_cell.session_id
            ),
            source=_mapping_text(request.metadata, "source") or "agent",
            workflow_id=_mapping_text(request.metadata, "workflow_id"),
            workflow_task_id=(
                _mapping_text(request.metadata, "workflow_task_id") or request.source_task_id
            ),
            task_run_id=_mapping_text(request.metadata, "task_run_id") or "",
            task_name=_mapping_text(request.metadata, "task_name") or child.spec.name,
            session_id=child.session_id,
        )
        task_id = record.task_id

        # 注册进 AgentCell 注册表 + 父子索引。
        self._cells[child.agent_id] = child
        self._children.setdefault(parent_cell.agent_id, set()).add(child.agent_id)
        parent_cell.child_ids.append(child.agent_id)

        # 启动子 agent_loop（mail_run_bridge 封装 runtime，deliver_sink 投父 mailbox）。
        mail_run_bridge = self._ctx.mail_run_bridge_builder(child)
        deliver_sink = self._ctx.deliver_sink_builder(child, task_id, parent_cell.mailbox)
        loop_task = asyncio.create_task(
            agent_loop(
                child,
                mail_run_bridge=mail_run_bridge,
                registry=self._ctx.registry,
                current_epoch_getter=self._ctx.current_epoch_getter,
                deliver_sink=deliver_sink,
                parent_task_id=request.parent_task_id,
                conversation_id=parent_cell.session_id,
                attach_task_id=task_id,
            ),
            name=f"agent-loop-child-{child.agent_id}",
        )
        self._loop_tasks.add(loop_task)
        loop_task.add_done_callback(self._loop_tasks.discard)
        # 注意：child.run_task 不指向 loop_task（agent_loop 协程）。
        # cell.run_task 字段语义是「当前 run 的 task（cancel 靶子）」，由 agent_loop
        # 内部 _run_one_mail 在创建 mail_run_bridge task 时设置（loop.py 内）。
        # agent_loop 协程本身永不被单独 cancel（不变量：只随树销毁），其生命周期
        # 由本 _loop_tasks 集合管理，不挂到 cell.run_task。
        # 若在此处把 loop_task 赋给 child.run_task，cancel_subtree 会砍掉整个
        # agent_loop 协程 → agent 永久失聪、mailbox 静默堆积（违反不变量）。
        # spawn 后到 agent_loop 首次进 _run_one_mail 之间，child.run_task 保持 None
        # （= idle 态），符合 AgentCell.run_task 的 None 语义。

        logger.info(
            "spawn dispatched child agent_id=%s parent=%s depth=%d task_id=%s",
            child.agent_id,
            parent_cell.agent_id,
            child.depth,
            task_id,
        )
        return SpawnResult(child_id=child.agent_id, status="dispatched", task_id=task_id)

    def _parent_tool_snapshot(self, parent_cell: AgentCell) -> tuple[Tool, ...]:
        """读取父工具，输入为父 cell，输出创建时固定或按 spec 解析的不可变快照。"""
        if parent_cell.run_enabled_tools is not None:
            return tuple(parent_cell.run_enabled_tools)
        if self._ctx.tool_lookup is None:
            if parent_cell.spec.tool_names:
                raise SpawnRejected("parent tool snapshot is unavailable for child clipping")
            return ()
        try:
            return resolve_tool_snapshot(self._ctx.tool_lookup, parent_cell.spec.tool_names)
        except ValueError as exc:
            raise SpawnRejected(str(exc)) from exc

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

    async def cancel_agent_run(self, agent_id: str) -> None:
        """取消指定 agent 子树的在途 run，输入为 agent_id，输出为局部取消完成。

        本方法用于 workflow 单个 child timeout 等局部收口场景，只砍目标子树名下
        run_task / pending approval / mailbox 内部消息；不关闭整个 TaskRegistry，
        后续 sibling workflow task 或父 agent 新 spawn 仍可继续登记。
        """
        descendants = self._descendants_inclusive(agent_id)
        for descendant_id in reversed(descendants):
            try:
                await self._ctx.registry.cancel_subtree(descendant_id)
            except Exception:
                logger.warning(
                    "cancel_agent_run registry.cancel_subtree failed for agent_id=%s",
                    descendant_id,
                    exc_info=True,
                )

        current_epoch = self._ctx.current_epoch_getter()
        for descendant_id in descendants:
            cell = self._cells.get(descendant_id)
            if cell is not None:
                _purge_stale_internal_mails(cell, current_epoch)

        if self._approval_canceller is not None:
            cancelled = 0
            for descendant_id in descendants:
                cancelled += self._approval_canceller(descendant_id)
            if cancelled:
                logger.info(
                    "cancel_agent_run cancelled %d pending approvals for agent_id=%s",
                    cancelled,
                    agent_id,
                )

        logger.info("cancel_agent_run done agent_id=%s", agent_id)

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

    def list_task_records(
        self,
        thread_id: str,
        *,
        include_finished: bool = False,
        limit: int = 50,
    ) -> tuple[TaskProjection, ...]:
        """查询当前树的不可变任务投影，输入为 thread/filter，输出为 TaskRegistry 快照。"""
        return self._ctx.registry.list_thread_tasks(
            thread_id,
            include_finished=include_finished,
            limit=limit,
        )

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


def _mapping_text(values: Mapping[str, Any], key: str) -> str | None:
    """读取非空字符串坐标，输入为 metadata/key，输出为清理后的文本或 None。"""
    value = values.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _build_initial_mail(input_message: Message, recipient_agent_id: str) -> Any:
    """构造子 agent 的首条输入 Mail（user_message，epoch=0 不过门卫）。

    输入为 input_message + 子 agent_id，输出为 Mail（kind=user_message, sender="parent",
    recipient=子 agent_id, task_id="", epoch=0）。子 agent_loop 从队头取此 mail 启动首 run。
    """
    from core.mail import Mail

    return Mail(
        kind="user_message",
        sender="parent",
        recipient_agent_id=recipient_agent_id,
        task_id="",
        epoch=0,
        payload=input_message,
    )


__all__ = [
    "AgentManager",
    "ChildDeliverSink",
    "DispatchedStatus",
    "SpawnContext",
    "SpawnRejected",
    "SpawnResult",
    "SubmitMode",
]
