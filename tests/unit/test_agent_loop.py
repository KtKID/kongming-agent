"""agent_loop + mailbox 单元测试（agent-tree-v0.1 task-4 验证闭环）。

覆盖（对照 README DoD / Smoke / E2E）：
1. **PoC epoch 门卫**（SM-002/SM-003）：单 agent 退化跑通 mailbox.get → 门卫 →
   run → classify → deliver 闭环；旧世代内部投递丢弃、user_message 永不过期、
   run_epoch 盖章时机正确。
2. **Outcome match 分发**（DoD）：Completed / Cancelled(tree_wide=True→emit_only) /
   Cancelled(tree_wide=False→deliver_up_or_ui) / Failed / Exhausted 五分支。
3. **三段时间线对抗性检验**（E2E-002/003/004）：
   - 段1：旧 epoch mail 在队列 → 门卫丢弃（+purge 兜底）。
   - 段2：run 在途 cancel → CancelledError 炸开 → 约束16 收口成 Result(cancelled)。
   - 段3：session 已提交（run 完成）→ 上投 mail 带旧 epoch 被拦（不复活）。
4. **agent_loop 不失聪**：cancel_subtree 靶子只含 run_task 不含 agent_loop；run 抛
   异常走 internal_failure 兜底（actor 不崩溃退出）。
5. **Mail / AgentCell / ThreadCell.bump_epoch** 契约 sanity。

验证命令：``uv run pytest tests/unit/test_agent_loop.py -v``
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from application.agents.cell import AgentCell, make_root_agent_cell
from application.agents.loop import (
    DeliverSink,
    agent_loop,
)
from application.agents.registry import TaskRegistry
from core.agent_spec import AgentSpec
from core.errors import AgentError, MaxTurnsExceededError, ProviderError
from core.mail import Mail
from core.message import Message
from core.outcome import (
    Cancelled,
    Outcome,
)
from core.result import Result

# ---------------------------------------------------------------------------
# 测试夹具：fake run_fn / 记录 deliver sink / 状态盒子
# ---------------------------------------------------------------------------


def _spec() -> AgentSpec:
    return AgentSpec(name="root", instructions="i", default_model="m")


def _user_mail(text: str, *, recipient: str = "root") -> Mail:
    return Mail(
        kind="user_message",
        sender="user",
        recipient_agent_id=recipient,
        task_id="",
        epoch=0,
        payload=Message.user(text),
    )


def _internal_mail(
    *,
    kind: str = "child_result",
    sender: str = "child01",
    recipient: str = "root",
    task_id: str = "t1",
    epoch: int,
    content: str = "done",
) -> Mail:
    return Mail(
        kind=kind,  # type: ignore[arg-type]
        sender=sender,
        recipient_agent_id=recipient,
        task_id=task_id,
        epoch=epoch,
        payload=Message.assistant(content),
    )


def _result(
    *,
    status: str = "completed",
    error: AgentError | None = None,
    metadata: dict[str, object] | None = None,
    final_message: Message | None = None,
) -> Result:
    return Result(
        run_id="r1",
        session_id="s1",
        status=status,  # type: ignore[arg-type]
        final_message=final_message if final_message is not None else Message.assistant("ok"),
        turn_count=1,
        error=error,
        metadata=metadata or {},
    )


@dataclass
class _Delivery:
    """单条 deliver 记录（method / outcome 类型 / run_epoch）。"""

    method: str  # "deliver_up_or_ui" | "emit_only"
    outcome_type: str
    run_epoch: int
    status: str


@dataclass
class RecordingSink(DeliverSink):
    """记录所有 deliver/emit 调用的 DeliverSink，输入为空，输出为记录列表。"""

    calls: list[_Delivery] = field(default_factory=list)

    def deliver_up_or_ui(
        self,
        cell: AgentCell,
        outcome: Outcome,
        *,
        result: Result,
        run_epoch: int,
    ) -> None:
        self.calls.append(
            _Delivery(
                method="deliver_up_or_ui",
                outcome_type=type(outcome).__name__,
                run_epoch=run_epoch,
                status=result.status,
            )
        )

    def emit_only(
        self,
        cell: AgentCell,
        outcome: Cancelled,
        *,
        result: Result,
        run_epoch: int,
    ) -> None:
        self.calls.append(
            _Delivery(
                method="emit_only",
                outcome_type=type(outcome).__name__,
                run_epoch=run_epoch,
                status=result.status,
            )
        )


def _make_loop_deps(
    cell: AgentCell,
    *,
    epoch_holder: list[int],
    run_results: list[Result] | None = None,
    run_event: asyncio.Event | None = None,
    raise_on_run: BaseException | None = None,
) -> tuple[Any, TaskRegistry, RecordingSink, list[str]]:
    """构造 agent_loop 注入依赖，返回 (run_fn, registry, sink, seeds)。"""
    registry = TaskRegistry()
    sink = RecordingSink()
    seeds: list[str] = []

    async def run_fn(seed: str, **kw: Any) -> Result:
        seeds.append(seed)
        # 让 cancel 测试能在 run 挂起时打断
        if run_event is not None:
            run_event.set()
            # 挂起让 cancel 有窗口
            await asyncio.sleep(10.0)
        if raise_on_run is not None:
            raise raise_on_run
        if run_results:
            return run_results.pop(0)
        return _result()

    def getter() -> int:
        return epoch_holder[0]

    return (run_fn, registry, sink, seeds), getter  # type: ignore[return-value]


def _spawn_agent_loop(
    cell: AgentCell,
    deps: tuple[Any, TaskRegistry, RecordingSink, list[str]],
    getter: Any,
) -> asyncio.Task[None]:
    """启动 agent_loop 后台协程，返回 task handle。"""
    run_fn, registry, sink, _seeds = deps
    return asyncio.create_task(
        agent_loop(
            cell,
            run_fn=run_fn,
            registry=registry,
            current_epoch_getter=getter,
            deliver_sink=sink,
        )
    )


# ---------------------------------------------------------------------------
# #5 契约 sanity：Mail / AgentCell / bump_epoch
# ---------------------------------------------------------------------------


def test_mail_is_frozen_and_carries_fields() -> None:
    """Mail 是 frozen；epoch/enqueued_at_ms 字段齐全。"""
    m = _user_mail("hi")
    assert m.kind == "user_message"
    assert m.sender == "user"
    assert m.epoch == 0
    assert m.enqueued_at_ms > 0
    with pytest.raises(Exception):
        m.epoch = 1  # type: ignore[misc]


def test_agent_cell_single_agent_degenerate_defaults() -> None:
    """make_root_agent_cell 退化形态：parent_id=None / depth=0 / persistent / idle。"""
    c = make_root_agent_cell(spec=_spec(), session_id="s1", agent_id="root")
    assert c.parent_id is None
    assert c.depth == 0
    assert c.lifecycle == "persistent"
    assert c.state == "idle"
    assert c.child_ids == []
    assert c.role_id is None
    assert c.run_task is None
    assert isinstance(c.mailbox, asyncio.Queue)


def test_thread_cell_bump_epoch_monotonic() -> None:
    """ThreadCell.bump_epoch 单调递增。用最小桩避免装配完整 cell。"""
    from hosts.web.threads.cell import ThreadCell

    # bump_epoch 只读写 self.epoch，不碰其它字段，可零依赖验证。
    class _Stub(ThreadCell):  # type: ignore[misc]
        def __init__(self) -> None:
            self.epoch = 0

    stub = _Stub()
    assert stub.bump_epoch() == 1
    assert stub.bump_epoch() == 2
    assert stub.epoch == 2


# ---------------------------------------------------------------------------
# #1 PoC epoch 门卫（SM-002/SM-003）
# ---------------------------------------------------------------------------


async def test_poc_epoch_gate_drops_stale_internal_mail() -> None:
    """SM-002：旧世代内部投递（child_result）被 epoch 门卫丢弃，不触发 run。"""
    cell = make_root_agent_cell(spec=_spec(), session_id="s1", agent_id="root")
    # current_epoch 已经 bump 到 2；注入 epoch=1 的旧内部 mail。
    (deps, getter) = _make_loop_deps(cell, epoch_holder=[2])
    _, _, sink, seeds = deps

    await cell.mailbox.put(_internal_mail(epoch=1, content="stale"))
    loop_task = _spawn_agent_loop(cell, deps, getter)

    # 给循环时间消费一条 mail。
    await asyncio.sleep(0.05)
    # 旧 mail 被丢弃：run 未启动、无 deliver、state 回 idle。
    assert seeds == []
    assert sink.calls == []
    assert cell.state == "idle"

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task


async def test_poc_epoch_gate_user_message_never_expires() -> None:
    """SM-002：user_message 永不过期（即使 current_epoch 已 bump）。"""
    cell = make_root_agent_cell(spec=_spec(), session_id="s1", agent_id="root")
    # current_epoch=bumped，但 user_message 不过门卫。
    (deps, getter) = _make_loop_deps(cell, epoch_holder=[5], run_results=[_result()])
    _, _, sink, seeds = deps

    await cell.mailbox.put(_user_mail("hi", recipient="root"))
    loop_task = _spawn_agent_loop(cell, deps, getter)

    # 等消费完成：run 启动 + deliver Completed。
    await asyncio.sleep(0.1)
    assert seeds == ["hi"]
    assert len(sink.calls) == 1
    assert sink.calls[0].method == "deliver_up_or_ui"
    assert sink.calls[0].outcome_type == "Completed"
    assert cell.state == "idle"

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task


async def test_poc_run_epoch_stamp_equals_mail_epoch() -> None:
    """run_epoch 盖章时机：内部投递 run_epoch = mail.epoch（当前世代匹配）。"""
    cell = make_root_agent_cell(spec=_spec(), session_id="s1", agent_id="root")
    (deps, getter) = _make_loop_deps(cell, epoch_holder=[3], run_results=[_result()])
    _, _, sink, seeds = deps

    # 当前 epoch=3，注入 epoch=3 的内部 mail（不过门卫）→ run_epoch 应=3。
    await cell.mailbox.put(_internal_mail(epoch=3, content="fresh"))
    loop_task = _spawn_agent_loop(cell, deps, getter)

    await asyncio.sleep(0.1)
    assert seeds == ["fresh"]
    assert len(sink.calls) == 1
    assert sink.calls[0].run_epoch == 3

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task


async def test_poc_loop_consumes_mailbox_serially() -> None:
    """mailbox 串行：两条 user_message 顺序消费，run 严格串行（结构必然单 run）。"""
    cell = make_root_agent_cell(spec=_spec(), session_id="s1", agent_id="root")
    # run_fn 控制顺序：先慢后快也能保证串行（seeds 顺序=入队顺序）。
    order: list[str] = []

    async def run_fn(seed: str, **kw: Any) -> Result:
        order.append(seed)
        await asyncio.sleep(0.01)
        return _result(final_message=Message.assistant(seed))

    registry = TaskRegistry()
    sink = RecordingSink()

    def getter() -> int:
        return 0

    await cell.mailbox.put(_user_mail("first"))
    await cell.mailbox.put(_user_mail("second"))
    loop_task = asyncio.create_task(
        agent_loop(
            cell,
            run_fn=run_fn,
            registry=registry,
            current_epoch_getter=getter,
            deliver_sink=sink,
        )
    )
    await asyncio.sleep(0.2)
    assert order == ["first", "second"]
    assert len(sink.calls) == 2

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task


# ---------------------------------------------------------------------------
# #2 Outcome match 分发（DoD 五分支）
# ---------------------------------------------------------------------------


async def _run_one_dispatch(result: Result, epoch_holder: list[int]) -> RecordingSink:
    """跑一条 user_message mail 经 agent_loop，返回 sink（断言分发分支）。"""
    cell = make_root_agent_cell(spec=_spec(), session_id="s1", agent_id="root")
    (deps, getter) = _make_loop_deps(cell, epoch_holder=epoch_holder, run_results=[result])
    _, _, sink, _ = deps
    await cell.mailbox.put(_user_mail("x"))
    loop_task = _spawn_agent_loop(cell, deps, getter)
    await asyncio.sleep(0.1)
    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task
    return sink


async def test_outcome_match_completed() -> None:
    """Completed → deliver_up_or_ui。"""
    sink = await _run_one_dispatch(_result(status="completed"), [0])
    assert sink.calls[-1].method == "deliver_up_or_ui"
    assert sink.calls[-1].outcome_type == "Completed"


async def test_outcome_match_cancelled_tree_wide_emit_only() -> None:
    """Cancelled(tree_wide=True) → emit_only。"""
    sink = await _run_one_dispatch(
        _result(status="cancelled", metadata={"cancel_reason": "user_interrupt"}),
        [0],
    )
    assert sink.calls[-1].method == "emit_only"
    assert sink.calls[-1].outcome_type == "Cancelled"


async def test_outcome_match_cancelled_local_deliver_up() -> None:
    """Cancelled(tree_wide=False, hook_blocked) → deliver_up_or_ui。"""
    sink = await _run_one_dispatch(
        _result(status="cancelled", metadata={"cancel_reason": "hook_blocked"}),
        [0],
    )
    assert sink.calls[-1].method == "deliver_up_or_ui"
    assert sink.calls[-1].outcome_type == "Cancelled"


async def test_outcome_match_failed() -> None:
    """Failed(llm.capability) → deliver_up_or_ui。"""
    err = ProviderError("boom", details={"status_code": 500})
    sink = await _run_one_dispatch(_result(status="failed", error=err), [0])
    assert sink.calls[-1].method == "deliver_up_or_ui"
    assert sink.calls[-1].outcome_type == "Failed"


async def test_outcome_match_exhausted() -> None:
    """Exhausted(max_turns) → deliver_up_or_ui。"""
    err = MaxTurnsExceededError("max")
    sink = await _run_one_dispatch(_result(status="failed", error=err), [0])
    assert sink.calls[-1].method == "deliver_up_or_ui"
    assert sink.calls[-1].outcome_type == "Exhausted"


# ---------------------------------------------------------------------------
# #3 三段时间线对抗性检验（E2E-002/003/004）
# ---------------------------------------------------------------------------


async def test_timeline_seg1_stale_mail_in_queue_dropped() -> None:
    """段1：旧 epoch mail 已在队列 → bump_epoch 后被消费侧门卫丢弃（不复活）。

    场景：B 恰在 cancel 瞬间完成，旧 epoch child_result 已进父 mailbox；
    cancel_subtree bump_epoch 后父消费该 mail 时被门卫拦截。
    """
    cell = make_root_agent_cell(spec=_spec(), session_id="s1", agent_id="root")
    epoch_holder = [1]
    (deps, getter) = _make_loop_deps(cell, epoch_holder=epoch_holder, run_results=[_result()])
    _, _, sink, seeds = deps

    # 段1 mail：epoch=1（旧世代）。先入队再 bump。
    await cell.mailbox.put(_internal_mail(epoch=1, content="stale_seg1"))
    # 模拟 cancel：bump 到 2（>mail.epoch=1）。
    epoch_holder[0] = 2

    loop_task = _spawn_agent_loop(cell, deps, getter)
    await asyncio.sleep(0.1)
    # 旧 mail 被门卫丢弃：run 未启动。
    assert seeds == []
    assert sink.calls == []

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task


async def test_timeline_seg2_run_in_flight_cancel_collected() -> None:
    """段2：run 在途 await 挂起 → cancel run_task → CancelledError 炸开 → 约束16 收口。

    场景：用户在 run 进行中点 Stop → cancel_subtree cancel run_task →
    runner 顶层吞 CancelledError 收口成 Result(cancelled)（约束16）→ Outcome=Cancelled。
    """
    cell = make_root_agent_cell(spec=_spec(), session_id="s1", agent_id="root")
    epoch_holder = [0]
    run_event = asyncio.Event()
    # run_fn 挂起模拟在途 run；被 cancel 时抛 CancelledError（runner 顶层会收口，
    # 这里直接模拟收口后的 cancelled Result——但为测「真炸开」路径，让 run_fn 抛 CE，
    # agent_loop 走 CancelledError 兜底分支成 cancelled Result）。

    async def run_fn(seed: str, **kw: Any) -> Result:
        run_event.set()
        # 模拟在途 LLM await 被 cancel：挂起，等外部 cancel 炸开。
        await asyncio.sleep(10.0)
        return _result()

    registry = TaskRegistry()
    sink = RecordingSink()

    def getter() -> int:
        return epoch_holder[0]

    await cell.mailbox.put(_user_mail("hi"))
    loop_task = asyncio.create_task(
        agent_loop(
            cell,
            run_fn=run_fn,
            registry=registry,
            current_epoch_getter=getter,
            deliver_sink=sink,
        )
    )

    # 等 run 真正启动（cell.run_task 被 set）。
    await run_event.wait()
    assert cell.state == "running"
    assert cell.run_task is not None
    assert not cell.run_task.done()

    # 段2：cancel run_task（模拟 cancel_subtree 的砍靶）。
    cell.run_task.cancel()
    await asyncio.sleep(0.1)

    # 约束16 收口：run_task 不 raise，被 agent_loop 收成 cancelled Result，
    # Outcome=Cancelled（user_interrupt → tree_wide=True → emit_only）。
    assert len(sink.calls) == 1
    assert sink.calls[0].outcome_type == "Cancelled"
    assert sink.calls[0].method == "emit_only"
    assert sink.calls[0].status == "cancelled"
    # 段2 后 state 回 idle（persistent）。
    assert cell.state == "idle"
    assert cell.run_task is None

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task


async def test_timeline_seg3_session_committed_upward_mail_intercepted() -> None:
    """段3：run 已完成（session 已提交）→ 上投 mail 带旧 epoch 被父门卫拦截。

    场景：B 完成时 append 进 session（既成事实，不回滚）；B 完成回灌的 child_result
    mail 带 B 的 run_epoch（旧世代）。父 cancel_subtree bump 后消费该 mail 被门卫拦。
    session 保留（不抛弃），只拦上投 mail。
    """
    cell = make_root_agent_cell(spec=_spec(), session_id="s1", agent_id="root")
    epoch_holder = [1]  # 父当前世代=1（B 的 run_epoch 也是 1）
    (deps, getter) = _make_loop_deps(cell, epoch_holder=epoch_holder, run_results=[_result()])
    _, _, sink, seeds = deps

    # 段3：B 完成回灌 child_result，epoch=1（B 的 run 起始世代）。
    seg3_mail = _internal_mail(epoch=1, content="child_done")
    await cell.mailbox.put(seg3_mail)
    # 父 cancel_subtree bump 到 2。
    epoch_holder[0] = 2

    loop_task = _spawn_agent_loop(cell, deps, getter)
    await asyncio.sleep(0.1)
    # 段3 既成事实：session 不回滚（这里只验 mail 被拦——run 不启动 = 不消费旧结果）。
    assert seeds == []
    assert sink.calls == []
    assert cell.state == "idle"

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task


# ---------------------------------------------------------------------------
# #4 不失聪验证（DoD）
# ---------------------------------------------------------------------------


async def test_run_fn_raising_non_cancel_does_not_kill_loop() -> None:
    """run_fn 抛非 cancel 异常 → internal_failure 兜底，actor 不失聪（不崩溃退出）。

    场景：run_fn 在包外抛 RuntimeError（理论不应发生，约束16 保证收口）；agent_loop
    兜底成 Failed(internal.bug) Result，走 Failed 分支，循环继续不退出。
    """
    cell = make_root_agent_cell(spec=_spec(), session_id="s1", agent_id="root")
    epoch_holder = [0]
    (deps, getter) = _make_loop_deps(
        cell, epoch_holder=epoch_holder, raise_on_run=RuntimeError("boom")
    )
    _, _, sink, _ = deps

    await cell.mailbox.put(_user_mail("first"))
    loop_task = _spawn_agent_loop(cell, deps, getter)
    await asyncio.sleep(0.1)

    # 兜底成 Failed(internal.bug)，循环不退出（task 仍 alive）。
    assert not loop_task.done()
    assert len(sink.calls) == 1
    assert sink.calls[0].outcome_type == "Failed"
    assert cell.state == "idle"

    # 再投一条，循环继续消费（不失聪）。
    # 重置 raise（_make_loop_deps 的 raise_on_run 是固定值，第二条会再 raise；
    # 这里只验循环未死——第二条也走兜底）。
    await cell.mailbox.put(_user_mail("second"))
    await asyncio.sleep(0.1)
    assert len(sink.calls) == 2
    assert not loop_task.done()

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task


async def test_cancel_subtree_does_not_target_agent_loop() -> None:
    """agent_loop 不在 cancel_subtree 靶子列表（cancel 只砍 run_task）。

    场景：cancel_subtree 后 agent_loop 协程仍存活（只有 run_task 被 cancel）；
    验证 cancel run_task 不波及 agent_loop 自身（防失聪不变量）。
    """
    cell = make_root_agent_cell(spec=_spec(), session_id="s1", agent_id="root")
    epoch_holder = [0]
    run_event = asyncio.Event()
    # 用真 registry 验证 cancel_subtree 只 cancel run_task。

    async def run_fn(seed: str, **kw: Any) -> Result:
        run_event.set()
        await asyncio.sleep(10.0)
        return _result()

    registry = TaskRegistry()
    sink = RecordingSink()

    def getter() -> int:
        return epoch_holder[0]

    await cell.mailbox.put(_user_mail("hi"))
    loop_task = asyncio.create_task(
        agent_loop(
            cell,
            run_fn=run_fn,
            registry=registry,
            current_epoch_getter=getter,
            deliver_sink=sink,
        )
    )
    await run_event.wait()
    # run_task 已 register 进 registry。
    assert cell.run_task is not None

    # cancel_subtree 砍靶（单 agent 退化 = cancel root run_task）。
    await registry.cancel_subtree(cell.agent_id)

    # agent_loop 协程本身没被 cancel（只 run_task 被砍）。
    assert not loop_task.done()
    # run_task 收口成 cancelled Result，Outcome=Cancelled。
    await asyncio.sleep(0.05)
    assert cell.run_task is None
    assert len(sink.calls) == 1
    assert sink.calls[0].outcome_type == "Cancelled"

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task


# ---------------------------------------------------------------------------
# purge 兜底（段1 purge 辅助）
# ---------------------------------------------------------------------------


async def test_purge_stale_internal_mails_keeps_user_messages() -> None:
    """_purge_stale_internal_mails：旧世代内部投递清出，user_message 与新世代保留。"""
    from application.agents.loop import _purge_stale_internal_mails

    cell = make_root_agent_cell(spec=_spec(), session_id="s1", agent_id="root")
    await cell.mailbox.put(_internal_mail(epoch=1, content="stale"))
    await cell.mailbox.put(_user_mail("keep_user"))
    await cell.mailbox.put(_internal_mail(epoch=3, content="fresh"))

    # current_epoch=3：epoch=1 的内部 mail 清出；user + epoch=3 保留。
    purged = _purge_stale_internal_mails(cell, current_epoch=3)
    assert purged == 1
    remaining: list[Mail] = []
    while not cell.mailbox.empty():
        remaining.append(cell.mailbox.get_nowait())
    assert len(remaining) == 2
    kinds = [m.kind for m in remaining]
    assert "user_message" in kinds
    # epoch=3 的内部 mail 保留。
    assert any(m.kind == "child_result" and m.epoch == 3 for m in remaining)


# ---------------------------------------------------------------------------
# 对抗性：bump 后立即注入旧 epoch mail（B 恰在 cancel 瞬间完成）
# ---------------------------------------------------------------------------


async def test_adversarial_old_epoch_injection_intercepted() -> None:
    """对抗性：bump_epoch 后立即注入旧 epoch 内部 mail → 被门卫拦截（PoC 通过标准）。

    复刻「B 恰在 cancel 瞬间完成」：先 bump，再注入带旧 epoch 的 child_result，
    断言 mail 被丢弃、run 不复活。
    """
    cell = make_root_agent_cell(spec=_spec(), session_id="s1", agent_id="root")
    epoch_holder = [1]
    (deps, getter) = _make_loop_deps(cell, epoch_holder=epoch_holder, run_results=[_result()])
    _, _, sink, seeds = deps

    loop_task = _spawn_agent_loop(cell, deps, getter)
    await asyncio.sleep(0.02)  # 让 loop 进入 mailbox.get 等待

    # 对抗：bump 后注入旧 epoch mail。
    epoch_holder[0] = 2
    await cell.mailbox.put(_internal_mail(epoch=1, content="ghost_result"))
    await asyncio.sleep(0.1)

    # 旧 mail 被门卫拦截：run 未启动、无 deliver（不复活说话）。
    assert seeds == []
    assert sink.calls == []
    assert cell.state == "idle"

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task
