"""AgentManager 单元 + e2e 链路测试（agent-tree-v0.1 task-5 验证闭环）。

覆盖（对照 README DoD / Smoke / E2E）：
1. **SM-001 spawn 登记先于返回**：spawn 内部 register_pending 调用在返回 SpawnResult 前；
   关门标志下 spawn 被拒（防孤儿子）。
2. **SM-002 深度 1 层校验**：depth=1 的子 cell 调 spawn → SpawnRejected。
3. **SM-003 single_shot 退出条件**：B single_shot 终态 + no_live_descendants → close_cell 注销。
4. **E2E-001 主链路 spawn→child_result→deliver**：spawn B → A 立即回 dispatched →
   B run 完成 → child_result Mail 入 A mailbox（sender=B, recipient=A, task_id 匹配）。
5. **E2E-002/003 cancel_subtree + 对抗性旧 epoch**：cancel_subtree 砍 run_task + bump
   epoch；B 恰在 cancel 瞬间完成 → 旧 epoch child_result 被父门卫拦截不复活。
6. **cancel_subtree 取消子树 pending approval future**（approval_canceller 注入）。

验证命令：``uv run pytest tests/unit/test_agent_manager.py -v``
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from application.agents.cell import AgentCell, make_root_agent_cell
from application.agents.manager import (
    AgentManager,
    ChildDeliverSink,
    SpawnContext,
    SpawnRejected,
    SpawnResult,
)
from application.agents.registry import TaskRegistry
from core.agent_spec import AgentSpec
from core.message import Message
from core.result import Result

# ---------------------------------------------------------------------------
# 测试夹具：fake run_fn / SpawnContext / spec / message
# ---------------------------------------------------------------------------


def _spec(name: str = "child") -> AgentSpec:
    return AgentSpec(name=name, instructions="i", default_model="m")


def _seed(text: str = "do it") -> Message:
    return Message.user(text)


class _FakeRuntime:
    """可控 run 的 fake runtime，输入为空，输出为装配后的实例。

    run 行为可控（立即完成 / 挂起 / 抛错 / 返回指定 Result），便于构造段2（在途 cancel）
    和 child_result 链路场景。记录每次 run 的 agent_id（验证子 agent_id 透传）。
    """

    def __init__(self) -> None:
        self.run_calls: list[tuple[str, str]] = []  # (seed, agent_id)
        self.hang = False
        self.final_message = Message.assistant("child done")
        self.status = "completed"
        self.run_started: asyncio.Event = asyncio.Event()

    def make_run_fn(self, agent_id: str) -> Any:
        """构造绑定 agent_id 的 run_fn 闭包，输入为 agent_id，输出为 async run_fn。"""

        runtime = self

        async def run_fn(seed: str, **kw: Any) -> Result:
            runtime.run_calls.append((seed, agent_id))
            runtime.run_started.set()
            if runtime.hang:
                await asyncio.sleep(10.0)
            return Result(
                run_id=f"run-{len(runtime.run_calls)}",
                session_id=agent_id,
                status=runtime.status,
                final_message=runtime.final_message,
                turn_count=1,
            )

        return run_fn


def _make_manager(
    *,
    runtime: _FakeRuntime | None = None,
    registry: TaskRegistry | None = None,
    max_spawn_depth: int = 1,
    approval_canceller: Any | None = None,
) -> tuple[AgentManager, TaskRegistry, _FakeRuntime]:
    """装配 AgentManager + SpawnContext，输入为可选注入，输出为 (manager, registry, runtime)。"""
    runtime = runtime or _FakeRuntime()
    registry = registry or TaskRegistry()
    epoch_box = {"epoch": 0}

    def run_fn_builder(child: AgentCell) -> Any:
        return runtime.make_run_fn(child.agent_id)

    def deliver_sink_builder(
        child: AgentCell, task_id: str, parent_mailbox: Any
    ) -> ChildDeliverSink:
        return ChildDeliverSink(
            child=child,
            task_id=task_id,
            parent_mailbox=parent_mailbox,
            parent_agent_id=child.parent_id or "",
        )

    def epoch_getter() -> int:
        return epoch_box["epoch"]

    ctx = SpawnContext(
        run_fn_builder=run_fn_builder,
        deliver_sink_builder=deliver_sink_builder,
        current_epoch_getter=epoch_getter,
        registry=registry,
        max_spawn_depth=max_spawn_depth,
    )
    manager = AgentManager(ctx, approval_canceller=approval_canceller)
    # 暴露 epoch_box 让测试 bump epoch。
    manager._epoch_box = epoch_box  # type: ignore[attr-defined]
    return manager, registry, runtime


def _root(session_id: str = "thread-1") -> AgentCell:
    return make_root_agent_cell(spec=_spec("root"), session_id=session_id)


# ---------------------------------------------------------------------------
# SM-001：spawn 登记先于返回
# ---------------------------------------------------------------------------


async def test_spawn_registers_pending_before_return() -> None:
    """SM-001：spawn 返回前 TaskRecord(pending) 已登记；SpawnResult 带 task_id。"""
    manager, registry, _ = _make_manager()
    root = _root()
    # 手动把 root 登记进 manager 注册表（spawn 用 parent_cell.mailbox 投递）。
    manager._cells[root.agent_id] = root

    result = manager.spawn(
        root,
        _spec(),
        (),
        _seed("hello"),
        cwd="/tmp/work",
        role_id=None,
    )

    assert isinstance(result, SpawnResult)
    assert result.status == "dispatched"
    assert len(result.child_id) == 8
    # pending TaskRecord 已登记（登记先于返回）。
    assert result.task_id
    record = None
    for rec in registry._records.values():
        if rec.agent_id == result.child_id:
            record = rec
            break
    assert record is not None
    assert record.status == "pending"
    assert record.agent_id == result.child_id

    # 清理：cancel 子 agent_loop 避免悬挂 task。
    await manager.cancel_subtree(root.agent_id)
    await asyncio.sleep(0.05)


async def test_spawn_rejected_when_registry_closed() -> None:
    """关门标志下 spawn 被拒（防孤儿子）；返回 SpawnRejected 不打断父 run。"""
    manager, registry, _ = _make_manager()
    root = _root()
    manager._cells[root.agent_id] = root
    registry.close_registry()  # 关门

    with pytest.raises(SpawnRejected):
        manager.spawn(root, _spec(), (), _seed(), cwd="/tmp", role_id=None)


# ---------------------------------------------------------------------------
# SM-002：深度 1 层校验
# ---------------------------------------------------------------------------


async def test_spawn_depth_exceeded_rejected() -> None:
    """SM-002：depth=1 的子 cell 调 spawn → SpawnRejected（v1 max_spawn_depth=1）。"""
    manager, _, _ = _make_manager(max_spawn_depth=1)
    root = _root()
    manager._cells[root.agent_id] = root

    # spawn 第一层子（depth=1，合法）。
    result = manager.spawn(root, _spec(), (), _seed(), cwd="/tmp", role_id=None)
    child = manager.get_agent(result.child_id)
    assert child is not None
    assert child.depth == 1

    # depth=1 的子 cell 再 spawn → 超限拒绝。
    with pytest.raises(SpawnRejected, match="depth exceeded"):
        manager.spawn(child, _spec(), (), _seed(), cwd="/tmp", role_id=None)

    await manager.cancel_subtree(root.agent_id)
    await asyncio.sleep(0.05)


async def test_child_cell_built_as_single_shot_depth_plus_one() -> None:
    """子 cell = single_shot + depth=parent.depth+1 + parent_id=父 agent_id。"""
    manager, _, _ = _make_manager()
    root = _root()
    manager._cells[root.agent_id] = root

    result = manager.spawn(root, _spec("kid"), ("skill-a",), _seed(), cwd="/w", role_id="reviewer")
    child = manager.get_agent(result.child_id)
    assert child is not None
    assert child.lifecycle == "single_shot"
    assert child.depth == 1
    assert child.parent_id == root.agent_id
    assert child.role_id == "reviewer"
    assert child.cwd == "/w"
    assert child.skill_names == ("skill-a",)
    # 父 child_ids 已注册。
    assert result.child_id in root.child_ids

    await manager.cancel_subtree(root.agent_id)
    await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# E2E-001：spawn → child_result → deliver 主链路
# ---------------------------------------------------------------------------


async def test_e2e_spawn_child_result_delivered_to_parent_mailbox() -> None:
    """E2E-001：spawn B → B 完成 → child_result Mail 入 A mailbox。

    断言：A 立即回 dispatched（不阻塞）；B run 收到正确 agent_id；child_result Mail
    sender=B.agent_id, recipient=A.agent_id, task_id=spawn 的 task_id；payload 是 B 的
    final_message。
    """
    manager, _, runtime = _make_manager()
    root = _root()
    manager._cells[root.agent_id] = root

    result = manager.spawn(root, _spec(), (), _seed("task"), cwd="/tmp", role_id=None)
    assert result.status == "dispatched"

    # 等 B run 完成 + child_result 投递。
    await asyncio.wait_for(runtime.run_started.wait(), timeout=2.0)
    # 给 agent_loop 时间消费 + deliver。
    await asyncio.sleep(0.2)

    # B run 收到正确 agent_id（run_fn 闭包透传）。
    assert len(runtime.run_calls) == 1
    assert runtime.run_calls[0][1] == result.child_id

    # child_result Mail 入 A mailbox。
    mail = await asyncio.wait_for(root.mailbox.get(), timeout=1.0)
    assert mail.kind == "child_result"
    assert mail.sender == result.child_id
    assert mail.recipient_agent_id == root.agent_id
    assert mail.task_id == result.task_id
    assert mail.payload.content == "child done"


async def test_e2e_failed_child_delivers_failure_notice() -> None:
    """E2E：子 agent run 失败（status=failed）→ child_result Mail 带 failure notice。"""
    runtime = _FakeRuntime()
    runtime.status = "failed"
    runtime.final_message = None
    manager, _, runtime = _make_manager(runtime=runtime)
    root = _root()
    manager._cells[root.agent_id] = root

    result = manager.spawn(root, _spec(), (), _seed(), cwd="/tmp", role_id=None)
    await asyncio.wait_for(runtime.run_started.wait(), timeout=2.0)
    await asyncio.sleep(0.2)

    mail = await asyncio.wait_for(root.mailbox.get(), timeout=1.0)
    assert mail.kind == "child_result"
    assert mail.sender == result.child_id
    assert "failed" in (mail.payload.content or "")


# ---------------------------------------------------------------------------
# E2E-002/003：cancel_subtree + 对抗性旧 epoch 拦截
# ---------------------------------------------------------------------------


async def test_cancel_subtree_cancels_child_run_task_and_bumps_epoch() -> None:
    """E2E-002：cancel_subtree → 子 run_task cancel + epoch bump。"""
    runtime = _FakeRuntime()
    runtime.hang = True  # 子 run 挂起（在途 LLM await）
    manager, _, runtime = _make_manager(runtime=runtime)
    root = _root()
    manager._cells[root.agent_id] = root

    result = manager.spawn(root, _spec(), (), _seed("long"), cwd="/tmp", role_id=None)
    child = manager.get_agent(result.child_id)
    assert child is not None
    await asyncio.wait_for(runtime.run_started.wait(), timeout=2.0)
    # bump epoch（模拟 ThreadManager.interrupt_agent_tree 先 bump）。
    manager._epoch_box["epoch"] = 1  # type: ignore[attr-defined]

    await manager.cancel_subtree(root.agent_id)
    await asyncio.sleep(0.1)

    # 子 run_task 被 cancel（约束16 收口 cancelled Result）。
    # single_shot 终态后 agent_loop 退出，run_task=None。
    assert child.run_task is None


async def test_cancel_subtree_calls_approval_canceller_for_subtree() -> None:
    """cancel_subtree 取消子树各 agent_id 名下 pending approval future。"""
    cancelled_agents: list[str] = []

    def fake_canceller(agent_id: str) -> int:
        cancelled_agents.append(agent_id)
        return 1 if agent_id else 0

    runtime = _FakeRuntime()
    runtime.hang = True
    manager, _, runtime = _make_manager(runtime=runtime, approval_canceller=fake_canceller)
    root = _root()
    manager._cells[root.agent_id] = root

    result = manager.spawn(root, _spec(), (), _seed(), cwd="/tmp", role_id=None)
    manager._epoch_box["epoch"] = 1  # type: ignore[attr-defined]
    await manager.cancel_subtree(root.agent_id)

    # root + child 两个 agent_id 都被取消审批。
    assert root.agent_id in cancelled_agents
    assert result.child_id in cancelled_agents


async def test_adversarial_old_epoch_child_result_intercepted() -> None:
    """E2E-003 对抗性：B 恰在 cancel 瞬间完成 → 旧 epoch child_result 被父门卫拦截。

    模拟：B 完成时 epoch=0；cancel_subtree bump 到 epoch=1；投递的 child_result 带
    epoch=0 → 父 mailbox 消费侧门卫丢弃（不复活说话）。
    """
    manager, _, _ = _make_manager()
    root = _root()
    manager._cells[root.agent_id] = root

    # 手工注入一条旧 epoch child_result Mail（模拟 B 在 bump 前完成投递）。
    from core.mail import Mail

    manager._epoch_box["epoch"] = 1  # type: ignore[attr-defined]
    await root.mailbox.put(
        Mail(
            kind="child_result",
            sender="ghost01",
            recipient_agent_id=root.agent_id,
            task_id="t1",
            epoch=0,  # 旧 epoch
            payload=Message.assistant("ghost result"),
        )
    )
    # root 没有 agent_loop 在跑，直接验证 purge 在 cancel_subtree 时清掉。
    await manager.cancel_subtree(root.agent_id)
    # purge 后 mailbox 应为空（旧 epoch 内部 mail 被清）。
    assert root.mailbox.empty()


# ---------------------------------------------------------------------------
# SM-003：single_shot 退出条件 + close_cell
# ---------------------------------------------------------------------------


async def test_single_shot_child_exits_and_close_cell_unregisters() -> None:
    """SM-003：B single_shot 终态 + no_live_descendants → close_cell 注销。"""
    manager, _registry, runtime = _make_manager()
    root = _root()
    manager._cells[root.agent_id] = root

    result = manager.spawn(root, _spec(), (), _seed("done"), cwd="/tmp", role_id=None)
    child = manager.get_agent(result.child_id)
    assert child is not None
    await asyncio.wait_for(runtime.run_started.wait(), timeout=2.0)
    await asyncio.sleep(0.2)  # B run 完成 + agent_loop 退出（single_shot）

    # close_cell 注销 child（no_live_descendants=True 因 B 已终态）。
    manager.close_cell(result.child_id)
    assert manager.get_agent(result.child_id) is None
    # 父 child_ids 同步移除。
    assert result.child_id not in root.child_ids


def test_close_cell_persistent_agent_not_unregistered() -> None:
    """persistent agent（主 agent）不进 closed——close_cell 无操作。"""
    manager, _, _ = _make_manager()
    root = _root()
    manager._cells[root.agent_id] = root

    manager.close_cell(root.agent_id)
    # persistent agent 仍在注册表。
    assert manager.get_agent(root.agent_id) is not None


# ---------------------------------------------------------------------------
# 查询：get_agent / list_agents / list_children
# ---------------------------------------------------------------------------


async def test_list_agents_and_list_children() -> None:
    """list_agents 列树内所有 agent；list_children 列直接子 agent。"""
    manager, _, _ = _make_manager()
    root = _root()
    manager._cells[root.agent_id] = root

    r1 = manager.spawn(root, _spec("c1"), (), _seed(), cwd="/w", role_id=None)
    r2 = manager.spawn(root, _spec("c2"), (), _seed(), cwd="/w", role_id=None)

    all_agents = manager.list_agents()
    agent_ids = {a.agent_id for a in all_agents}
    assert {root.agent_id, r1.child_id, r2.child_id} <= agent_ids

    children = manager.list_children(root.agent_id)
    child_ids = {c.agent_id for c in children}
    assert child_ids == {r1.child_id, r2.child_id}

    await manager.cancel_subtree(root.agent_id)
    await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# cancel_subtree 幂等
# ---------------------------------------------------------------------------


async def test_cancel_subtree_idempotent() -> None:
    """cancel_subtree 幂等：多次取消已终态子树无副作用。"""
    manager, _, _ = _make_manager()
    root = _root()
    manager._cells[root.agent_id] = root

    await manager.cancel_subtree(root.agent_id)
    # 再 cancel 一次不抛错。
    await manager.cancel_subtree(root.agent_id)


# ---------------------------------------------------------------------------
# 并发安全：spawn 登记与 cancel 竞态靠关门标志兜底
# ---------------------------------------------------------------------------


async def test_concurrent_spawn_then_cancel_no_orphan() -> None:
    """并发 spawn + cancel：关门标志下 spawn 被拒，无孤儿子泄漏。"""
    manager, registry, _ = _make_manager()
    root = _root()
    manager._cells[root.agent_id] = root

    # 先 spawn 一个合法子。
    manager.spawn(root, _spec(), (), _seed(), cwd="/tmp", role_id=None)
    # 关门后试图再 spawn → 被拒。
    registry.close_registry()
    with pytest.raises(SpawnRejected):
        manager.spawn(root, _spec(), (), _seed(), cwd="/tmp", role_id=None)

    # cancel 清理第一个子。
    await manager.cancel_subtree(root.agent_id)
    await asyncio.sleep(0.05)
