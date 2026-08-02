"""agent_loop — 常驻消费协程（模块 C · Mailbox + agent_loop 的核心）。

功能：定义每个 :class:`AgentCell` 的常驻消费协程 ``agent_loop``，把
``cell.mailbox`` 的串行输入流驱动成「epoch 门卫 → run → classify_result →
Disposition 按 action 二选一分发 → idle」的统一内核循环。
作用：单 agent 退化形态下把主 agent 从「同步阻塞 run_once」演进为「mailbox
串行驱动 + agent_loop 常驻消费 + epoch 门卫」；epoch 门卫只在消费侧抛弃
旧世代内部投递（user_message 永不过期），保证「打断只阻止未来不回滚过去」。
关键执行流程（agent_loop 主循环）：
1. ``await cell.mailbox.get()`` 取一条 Mail。
2. **epoch 门卫**：``mail.kind != "user_message" and mail.epoch < current_epoch``
   → 丢弃（``continue``）。``user_message`` 永不过期（用户输入不携带世代语义）。
3. **run_epoch 盖章**：``run_epoch = mail.epoch``（run 启动瞬间捕获；user_message
   用 0，不过门卫）。该 run 的上投 Mail 全继承此 epoch。
4. ``cell.state = "running"`` → ``create_task(mail_run_bridge(mail_text, mail=mail))``
   → ``registry.register_run``
   → ``await run_task``（约束16：cancel 收口成 Result 不 raise；Exception 兜底
   ``internal_failure_result`` 防 actor 失聪）。
5. ``classify_result(result, current_epoch) -> Disposition`` → **按 action 二选一**：
   - ``action == "emit_only"``（树级取消：tree_wide=True 的 cancel 类 reason，如
     user_interrupt/parent_cascade/watchdog）→ emit_only（父也被砍，不上投只 emit）
   - ``action == "deliver_up"``（其余所有情况：completed / max_turns 等预算耗尽 /
     llm.* / tool.execution 等失败 / 局部 cancel）→ deliver_up_or_ui（上投 / 推 UI，
     payload 里带 reason 让父 agent 区分来源）
6. single_shot 终态退出检查（persistent 不退出）→ ``cell.state = "idle"``。
   外层 ``try/finally``：``finally registry.close_cell(cell.agent_id)``。
关键不变量：
- **agent_loop 永不被单独 cancel**（只随树销毁），否则 agent 永久失聪、mailbox
  静默堆积——cancel_subtree 靶子列表只含 run_task，不含 agent_loop 协程。
- **打断只阻止未来**：已提交 session 一律保留；抛弃只在 mailbox 消费侧 epoch 门卫。

事实源：``docs/spec/agent-tree-v0.1/04-data-and-state.md``（agent_loop 伪代码 +
打断链三段时间线）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

from application.agents.cell import AgentCell
from application.agents.registry import TaskRegistry
from core.errors import AgentError
from core.mail import Mail
from core.message import Message
from core.outcome import Disposition, classify_result
from core.result import Result

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# mail_run_bridge / deliver 协议（注入式依赖，让 agent_loop 可单测、不硬耦合 HostDispatcher）
# ---------------------------------------------------------------------------


class MailRunBridge(Protocol):
    """执行一条 Mail 触发的 run，封装宿主侧 runtime/bridge。

    ``mail_text`` 是从 ``Mail.payload.content`` 提取出的文本；``mail`` 保留完整
    mailbox 信封，供宿主侧读取 metadata / kind / task_id。返回类型用
    ``Coroutine`` 精确表达，让 ``asyncio.create_task`` 能直接调度。
    """

    def __call__(self, mail_text: str, *, mail: Mail) -> Coroutine[Any, Any, Result]:
        """执行当前 mailbox 消息对应的 run，返回 runner 收口后的 Result。"""
        ...


class DeliverSink(Protocol):
    """deliver 上投 / UI 推送的宿主侧 sink 协议（agent_loop 注入式依赖）。

    agent_loop 的「上投 Mail / 推 UI」动作委托给宿主侧（task-4 主 agent 退化形态下
    通常推 WS Event；子 agent 上投父 mailbox 留 task-5）。本 Protocol 让 agent_loop
    可单测：注入 fake sink 断言分发正确，无需真实 WS。
    """

    def deliver_up_or_ui(
        self,
        cell: AgentCell,
        disposition: Disposition,
        *,
        result: Result,
        run_epoch: int,
    ) -> None:
        """action=deliver_up 的上投/UI 推送（completed / 预算耗尽 / 失败 / 局部 cancel）。

        主 agent（parent_id=None）→ 推 UI（Event 已由 bridge 通过 event_sinks 推过，
        本处通常只记录日志 / 收尾）；子 agent（task-5）→ 投父 mailbox。``disposition.reason``
        让消费侧区分来源（completed vs failed vs 局部 cancel）。
        """
        ...

    def emit_only(
        self,
        cell: AgentCell,
        disposition: Disposition,
        *,
        result: Result,
        run_epoch: int,
    ) -> None:
        """action=emit_only（树级取消）：父也被砍，不上投，只 emit（Event 已发）。"""
        ...


# ---------------------------------------------------------------------------
# 内部 helper：从 Mail 构造 mailbox 文本 / 兜底 Result
# ---------------------------------------------------------------------------


def _get_mailbox_text(mail_payload: Message) -> str:
    """从 Mail.payload 取 run 的 user_input 文本，输入为 Message，输出为 str。

    user_message / child_result / system_notice 的 payload 都是 Message；
    mail_run_bridge 的 user_input 取 ``payload.content``（None 时退化为空串，避免
    runner 收到 None content）。复杂结构（attachments / references）由
    mail_run_bridge 闭包封装，不在此处展开（保持 agent_loop 与 wire 协议解耦）。
    """
    content = mail_payload.content
    return content if content is not None else ""


def _internal_failure_result(exc: BaseException, *, session_id: str) -> Result:
    """把意外异常兜底成 Result(status=failed)，输入为异常，输出为 Result。

    约束16 保证 runner.run 内部已把 CancelledError / AgentError 收口成 Result；
    本函数只在「mail_run_bridge 自身在包外抛了异常（理论不应发生）」时兜底，
    防 actor 失聪——agent_loop 永远走 action 二选一分发，绝不因 run 抛异常而崩溃退出。
    """
    wrapped = AgentError(
        f"agent_loop internal failure: {exc!r}",
        details={"exception_type": type(exc).__name__},
    )
    return Result(
        run_id="",
        session_id=session_id,
        status="failed",
        final_message=None,
        turn_count=0,
        error=wrapped,
        metadata={"internal_failure": True, "exception_type": type(exc).__name__},
    )


def _purge_stale_internal_mails(cell: AgentCell, current_epoch: int) -> int:
    """非破坏性地从 mailbox 清出旧世代内部投递（兜底），返回清出条数。

    asyncio.Queue 没有 peek/drain 原语；本函数在 cancel 编排时被调用（管理器侧），
    遍历当前队列内容，只把 ``kind != user_message and epoch < current_epoch`` 的
    Mail 取走丢弃，user_message 与新世代内部投递保留。是 epoch 门卫的「段1 purge
    兜底」——消费侧门卫已是正确性保证，purge 只为减少静默堆积。
    """
    # Queue 内部用 _queue deque；这里通过非阻塞 get_nowait 取空再选择性 put 回，
    # 避免直接触私有属性。并发边界：本函数应在 cancel 编排（cell 已停 run）时调用，
    # 此时无新 run 消费，且 mailbox.put 由外部串行触发。
    drained: list[Mail] = []
    purged = 0
    while True:
        try:
            drained.append(cell.mailbox.get_nowait())
        except asyncio.QueueEmpty:
            break
    for mail in drained:
        # drained 元素必为 Mail（mailbox 是 asyncio.Queue[Mail]）。
        if mail.kind != "user_message" and mail.epoch < current_epoch:
            purged += 1
            continue
        cell.mailbox.put_nowait(mail)
    return purged


# ---------------------------------------------------------------------------
# agent_loop 主协程（核心）
# ---------------------------------------------------------------------------


async def agent_loop(
    cell: AgentCell,
    *,
    mail_run_bridge: MailRunBridge,
    registry: TaskRegistry,
    current_epoch_getter: Callable[[], int],
    deliver_sink: DeliverSink,
    parent_task_id: str | None = None,
    conversation_id: str = "",
    attach_task_id: str | None = None,
) -> None:
    """常驻消费协程：mailbox.get → epoch 门卫 → run → classify → match → deliver → idle。

    单 agent 退化形态（task-4）：``cell.parent_id=None`` / ``lifecycle="persistent"``，
    永不退出（不进 closed）。退出逻辑写通用式供 task-5 single_shot 子 agent。

    Args:
        cell: 消费的 agent 实例（含 mailbox / state / run_task）。
        mail_run_bridge: 执行一条 Mail 触发 run 的协程，签名
            ``async (mail_text, *, mail) -> Result``。宿主侧封装 runtime.run /
            bridge.run_once（注入 event_sinks / session / agent_id）。
        registry: 该 cell 所属树的 :class:`TaskRegistry`（登记 run_task / close_cell）。
        current_epoch_getter: 实时读取当前世代计数器的 callable（从 ThreadCell.epoch
            读）。用 getter 而非启动时冻结，让 cancel_subtree ``bump_epoch()`` 后本
            循环能立刻看到新值（门卫取最新 epoch）。
        deliver_sink: 上投 / UI 推送的宿主侧 sink（注入式，主 agent 推 WS / 记日志）。
        parent_task_id: 该 agent run 登记时的 parent_task_id（根 = None）。
        conversation_id: 会话树 id（= thread_id），用于日志 / Event 坐标。
        attach_task_id: task-5 子 agent spawn 时 AgentManager.register_pending 登记的
            task_id。首 run 用 ``registry.attach_run_task`` 绑定真实 run_task 到该 pending
            记录（而非 ``register_run`` 新建）。后续 run（single_shot 通常只 1 run）走
            ``register_run``。None = 主 agent / 单 agent 退化形态，走 ``register_run``。

    不变量：
    - 永不被单独 cancel（只随树销毁）；cancel_subtree 靶子只含 run_task 不含本协程。
    - ``await run_task`` 永不 raise（约束16 + internal_failure 兜底）。
    """
    try:
        while True:
            await _run_one_mail(
                cell,
                mail_run_bridge=mail_run_bridge,
                registry=registry,
                current_epoch_getter=current_epoch_getter,
                deliver_sink=deliver_sink,
                parent_task_id=parent_task_id,
                conversation_id=conversation_id,
                attach_task_id=attach_task_id,
            )

            # single_shot 退出检查（通用式，v1 深度1 时子 agent 无子孙恒真；
            # persistent 主 agent 永不退出）。task-5 spawn single_shot 子 agent 时
            # 此分支生效；task-4 主 agent lifecycle=persistent 永远走 idle 回路。
            if cell.lifecycle == "single_shot" and registry.no_live_descendants(cell.agent_id):
                break
            cell.state = "idle"
    finally:
        # 树销毁（cancel_subtree 关门 / cell evict）时 agent_loop 退出，注销 agent。
        # close_cell 幂等，重复调用安全。注意：cancel_subtree 不应 cancel agent_loop
        # 协程本身（否则失聪）；本 finally 仅在协程正常退出 / 任务被取消时兜底注销。
        registry.close_cell(cell.agent_id)


async def _run_one_mail(
    cell: AgentCell,
    *,
    mail_run_bridge: MailRunBridge,
    registry: TaskRegistry,
    current_epoch_getter: Callable[[], int],
    deliver_sink: DeliverSink,
    parent_task_id: str | None,
    conversation_id: str,
    attach_task_id: str | None = None,
) -> None:
    """处理一条 Mail：门卫 → 盖章 → run → classify_result → 按 action 二选一分发（agent_loop 单轮）。

    拆出独立函数便于单测（注入一条 Mail，断言分发结果），并让 agent_loop 主循环
    逻辑清晰。输入为 cell + 注入依赖；输出为 None（副作用：state 变化 / 上投）。
    """

    mail: Mail = await cell.mailbox.get()
    current_epoch = current_epoch_getter()

    # epoch 门卫：旧世代内部投递丢弃（user_message 永不过期）。
    # 事实源：04-data-and-state.md「ThreadCell.epoch: 单调递增」+ 需求要点 2。
    if mail.kind != "user_message" and mail.epoch < current_epoch:
        logger.info(
            "agent_loop dropped stale mail agent_id=%s kind=%s mail_epoch=%d "
            "current_epoch=%d task_id=%s",
            cell.agent_id,
            mail.kind,
            mail.epoch,
            current_epoch,
            mail.task_id,
        )
        return

    # run_epoch 盖章：run 启动瞬间捕获（内部投递 = mail.epoch；user_message 用 0）。
    # 该 run 的上投 Mail 全继承此 epoch；cancel_subtree bump 后旧 run
    # 上投 Mail 会带旧 epoch 被父门卫拦（段3 既成事实不回滚，只拦上投）。
    run_epoch = mail.epoch

    cell.state = "running"
    mail_text = _get_mailbox_text(mail.payload)

    # 创建 run task 并登记账本（登记先于 await，满足「登记先于暴露」不变量）。
    # registry 关门后（cancel_subtree 后）register_run 会 raise RuntimeError——
    # 但此时本 Mail 已过门卫（epoch 匹配），关门通常发生在 cancel 路径而非消费路径；
    # 若竞态使 register 失败，兜底成 internal_failure Result 走 Failed 分支。
    try:
        run_task: asyncio.Task[Result] = asyncio.create_task(
            mail_run_bridge(mail_text, mail=mail),
            name=f"agent-run-{cell.agent_id}-{mail.kind}",
        )
    except Exception as exc:
        logger.exception(
            "agent_loop create_task failed agent_id=%s kind=%s",
            cell.agent_id,
            mail.kind,
        )
        result = _internal_failure_result(exc, session_id=cell.session_id)
        _dispatch_outcome(
            cell,
            classify_result(result, current_epoch),
            result=result,
            run_epoch=run_epoch,
            deliver_sink=deliver_sink,
        )
        return

    cell.run_task = run_task
    # 登记账本：task-5 子 agent spawn 时 attach_task_id 指向 AgentManager.register_pending
    # 登记的 pending 记录——首 run 绑定真实 run_task 到该记录（status pending→running）。
    # attach 失败（task_id 未知 / 非 pending）fallback 到 register_run 新建记录。
    # 主 agent / 单 agent 退化形态 attach_task_id=None，走 register_run。
    try:
        attached = (
            registry.attach_run_task(attach_task_id, run_task)
            if attach_task_id is not None
            else False
        )
        if not attached:
            registry.register_run(run_task, cell.agent_id, parent_task_id)
    except RuntimeError:
        # registry 已关门（cancel 在 create_task 与 register 之间发生）：cancel 本
        # task（让约束16 收口成 cancelled Result）+ 兜底分发。语义等价于「该 run 被
        # cancel_subtree 砍」，Disposition 按 cancel 类 reason 归类。
        run_task.cancel()
    except Exception:
        logger.exception("agent_loop register_run failed agent_id=%s", cell.agent_id)

    # await run_task：约束16 保证不 raise（runner 收口 CancelledError/AgentError 成
    # Result）；理论外的异常（mail_run_bridge 包外抛、task 被 cancel 后未收口）兜底成
    # internal_failure Result，actor 不失聪。
    try:
        result = await run_task
    except asyncio.CancelledError:
        # run_task 被 cancel 但未在包内收口成 Result（理论不应发生，约束16 保证收口）。
        # 兜底成 cancelled Result，classify_result 归类为 cancel 类 reason。
        result = Result(
            run_id="",
            session_id=cell.session_id,
            status="cancelled",
            final_message=None,
            turn_count=0,
            metadata={"cancel_reason": "user_interrupt", "agent_loop_fallback": True},
        )
    except Exception as exc:
        logger.exception("agent_loop run_task raised agent_id=%s", cell.agent_id)
        result = _internal_failure_result(exc, session_id=cell.session_id)
    finally:
        cell.run_task = None

    _dispatch_outcome(
        cell,
        classify_result(result, current_epoch),
        result=result,
        run_epoch=run_epoch,
        deliver_sink=deliver_sink,
    )


def _dispatch_outcome(
    cell: AgentCell,
    disposition: Disposition,
    *,
    result: Result,
    run_epoch: int,
    deliver_sink: DeliverSink,
) -> None:
    """classify_result → Disposition → 按 action 二选一（agent_loop 唯一分发点）。

    事实源：04-data-and-state.md「agent_loop 主循环」分发逻辑 + README 技术设计。

    - emit_only：树级取消（user_interrupt/parent_cascade/watchdog 且 tree_wide=True），
        父也被砍，不上投，只 emit。
    - deliver_up：其余所有情况（completed/exhausted/failed/局部 cancel），
        上投 / 推 UI，payload 里带 reason 让父 agent 区分来源。
    """
    if disposition.action == "emit_only":
        deliver_sink.emit_only(cell, disposition, result=result, run_epoch=run_epoch)
    else:
        deliver_sink.deliver_up_or_ui(cell, disposition, result=result, run_epoch=run_epoch)


# ---------------------------------------------------------------------------
# 默认 DeliverSink：主 agent 退化形态（推 UI = 记日志，Event 已由 bridge 推过）
# ---------------------------------------------------------------------------


class RootAgentDeliverSink:
    """主 agent（parent_id=None）的默认 DeliverSink。

    主 agent 没有「父 mailbox」可上投：bridge.run_once 已通过 event_sinks（WSEventSink
    等）把 run 的 Event（run.start / assistant.* / run.end / run.cancelled / error）
    实时推给 UI。故本 sink 对所有 deliver 只记 debug 日志（便于排障），不做额外推送。

    task-5 spawn 子 agent 时，子 agent 用投父 mailbox 的 sink（另写）；主 agent
    始终用本 sink。
    """

    def deliver_up_or_ui(
        self,
        cell: AgentCell,
        disposition: Disposition,
        *,
        result: Result,
        run_epoch: int,
    ) -> None:
        """主 agent deliver：Event 已推 UI，此处只记日志。"""
        logger.debug(
            "root agent deliver agent_id=%s reason=%s status=%s run_epoch=%d",
            cell.agent_id,
            disposition.reason,
            result.status,
            run_epoch,
        )

    def emit_only(
        self,
        cell: AgentCell,
        disposition: Disposition,
        *,
        result: Result,
        run_epoch: int,
    ) -> None:
        """主 agent action=emit_only（树级取消）：Event 已推 UI，此处只记日志。"""
        logger.debug(
            "root agent emit_only (cancelled tree_wide) agent_id=%s reason=%s run_epoch=%d",
            cell.agent_id,
            disposition.reason,
            run_epoch,
        )


__all__ = [
    "DeliverSink",
    "MailRunBridge",
    "RootAgentDeliverSink",
    "agent_loop",
]
