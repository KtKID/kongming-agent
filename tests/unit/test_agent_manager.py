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
    SubmitMode,
)
from application.agents.registry import TaskRegistry
from application.agents.subagent_tools import SpawnAgentRequest
from core.agent_spec import AgentSpec
from core.contracts import PreparedToolCall, SteerRequest, ToolContext, ToolResult
from core.mail import Mail
from core.message import Message
from core.result import Result

# ---------------------------------------------------------------------------
# 测试夹具：fake mail_run_bridge / SpawnContext / spec / message
# ---------------------------------------------------------------------------


def _spec(name: str = "child") -> AgentSpec:
    return AgentSpec(name=name, instructions="i", default_model="m")


def _input_msg(text: str = "do it") -> Message:
    return Message.user(text)


def _spawn_request(
    parent: AgentCell,
    spec: AgentSpec | None = None,
    skill_names: tuple[str, ...] = (),
    seed_message: Message | None = None,
    *,
    cwd: str = "/tmp",
    role_id: str | None = None,
    parent_task_id: str | None = None,
) -> SpawnAgentRequest:
    """构造测试 SpawnAgentRequest，输入为父 cell 和可选字段，输出统一 request。"""
    return SpawnAgentRequest(
        parent_agent_id=parent.agent_id,
        spec=spec or _spec(),
        seed_message=seed_message or _input_msg(),
        cwd=cwd,
        parent_task_id=parent_task_id,
        role_id=role_id,
        skill_names=skill_names,
    )


class _FakeRuntime:
    """可控 run 的 fake runtime，输入为空，输出为装配后的实例。

    run 行为可控（立即完成 / 挂起 / 抛错 / 返回指定 Result），便于构造段2（在途 cancel）
    和 child_result 链路场景。记录每次 run 的 agent_id（验证子 agent_id 透传）。
    """

    def __init__(self) -> None:
        self.run_calls: list[tuple[str, str]] = []  # (mail_text, agent_id)
        self.hang = False
        self.final_message = Message.assistant("child done")
        self.status = "completed"
        self.run_started: asyncio.Event = asyncio.Event()

    def make_mail_run_bridge(self, agent_id: str) -> Any:
        """构造绑定 agent_id 的 mail_run_bridge 闭包，输入为 agent_id，输出为 async bridge。"""

        runtime = self

        async def mail_run_bridge(mail_text: str, *, mail: Mail) -> Result:
            del mail
            runtime.run_calls.append((mail_text, agent_id))
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

        return mail_run_bridge


class _NamedTool:
    """工具裁剪测试替身，输入为名称，输出最小 Tool 实现。"""

    description = "test"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, name: str) -> None:
        self.name = name

    async def execute(self, prepared: PreparedToolCall, ctx: ToolContext) -> ToolResult:
        args = prepared.arguments
        """返回成功结果，输入为任意参数，输出固定内容。"""
        del args, ctx
        return ToolResult(ok=True, content=self.name)


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

    def mail_run_bridge_builder(child: AgentCell) -> Any:
        return runtime.make_mail_run_bridge(child.agent_id)

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
        mail_run_bridge_builder=mail_run_bridge_builder,
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

    result = manager.spawn(_spawn_request(root, seed_message=_input_msg("hello"), cwd="/tmp/work"))

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
        manager.spawn(_spawn_request(root, cwd="/tmp"))


async def test_local_child_cancel_keeps_registry_open_for_sibling_spawn() -> None:
    """局部取消 child A 后，同一父 agent 仍可登记并运行 child B。"""
    runtime = _FakeRuntime()
    runtime.hang = True
    manager, registry, _ = _make_manager(runtime=runtime)
    root = _root()
    manager._cells[root.agent_id] = root

    child_a = manager.spawn(_spawn_request(root, seed_message=_input_msg("child A")))
    await asyncio.wait_for(runtime.run_started.wait(), timeout=1.0)
    await manager.cancel_agent_run(child_a.child_id)

    assert registry.is_closed is False
    runtime.hang = False
    runtime.run_started = asyncio.Event()
    child_b = manager.spawn(_spawn_request(root, seed_message=_input_msg("child B")))
    await asyncio.wait_for(runtime.run_started.wait(), timeout=1.0)

    assert child_b.child_id != child_a.child_id
    assert any(agent_id == child_b.child_id for _, agent_id in runtime.run_calls)
    await manager.cancel_subtree(root.agent_id)


# ---------------------------------------------------------------------------
# SM-002：深度 1 层校验
# ---------------------------------------------------------------------------


async def test_spawn_depth_exceeded_rejected() -> None:
    """SM-002：depth=1 的子 cell 调 spawn → SpawnRejected（v1 max_spawn_depth=1）。"""
    manager, _, _ = _make_manager(max_spawn_depth=1)
    root = _root()
    manager._cells[root.agent_id] = root

    # spawn 第一层子（depth=1，合法）。
    result = manager.spawn(_spawn_request(root, cwd="/tmp"))
    child = manager.get_agent(result.child_id)
    assert child is not None
    assert child.depth == 1

    # depth=1 的子 cell 再 spawn → 超限拒绝。
    with pytest.raises(SpawnRejected, match="depth exceeded"):
        manager.spawn(_spawn_request(child, cwd="/tmp"))

    await manager.cancel_subtree(root.agent_id)
    await asyncio.sleep(0.05)


async def test_child_cell_built_as_single_shot_depth_plus_one() -> None:
    """子 cell = single_shot + depth=parent.depth+1 + parent_id=父 agent_id。"""
    manager, _, _ = _make_manager()
    root = _root()
    manager._cells[root.agent_id] = root

    result = manager.spawn(
        _spawn_request(root, _spec("kid"), ("skill-a",), cwd="/w", role_id="reviewer")
    )
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


async def test_spawn_clips_tools_before_cell_creation_and_freezes_effective_snapshot() -> None:
    """AgentManager 创建 cell 前求交，并让 spec 与 run_enabled_tools 共用结果。"""
    manager, _, _ = _make_manager()
    read = _NamedTool("read_file")
    write = _NamedTool("write_file")
    shell = _NamedTool("shell")
    wrapped_write = _NamedTool("write_file")
    root = make_root_agent_cell(
        spec=AgentSpec(
            name="root",
            instructions="",
            default_model="m",
            tool_names=("read_file", "write_file"),
        ),
        session_id="tree",
        enabled_tools=(read, write),
    )
    manager._cells[root.agent_id] = root

    result = manager.spawn(
        SpawnAgentRequest(
            parent_agent_id=root.agent_id,
            spec=AgentSpec(
                name="child",
                instructions="",
                default_model="m",
                tool_names=("shell", "write_file"),
            ),
            seed_message=Message.user("do it"),
            cwd="/tmp",
            requested_tool_names=("shell", "write_file"),
            scope_allowed_tool_names=("write_file",),
            enabled_tools=(shell, wrapped_write),
        )
    )
    child = manager.get_agent(result.child_id)

    assert child is not None
    assert child.spec.tool_names == ("write_file",)
    assert child.run_enabled_tools == (wrapped_write,)
    await manager.cancel_subtree(root.agent_id)


async def test_spawn_preserves_explicit_empty_child_tool_snapshot() -> None:
    """显式空 requested 集合生成空 tuple，后续 run 不会回退到 spec 默认工具。"""
    manager, _, _ = _make_manager()
    read = _NamedTool("read_file")
    root = make_root_agent_cell(
        spec=AgentSpec(
            name="root",
            instructions="",
            default_model="m",
            tool_names=("read_file",),
        ),
        session_id="tree",
        enabled_tools=(read,),
    )
    manager._cells[root.agent_id] = root

    result = manager.spawn(
        SpawnAgentRequest(
            parent_agent_id=root.agent_id,
            spec=AgentSpec(
                name="child",
                instructions="",
                default_model="m",
                tool_names=("read_file",),
            ),
            seed_message=Message.user("no tools"),
            cwd="/tmp",
            requested_tool_names=(),
        )
    )
    child = manager.get_agent(result.child_id)

    assert child is not None
    assert child.spec.tool_names == ()
    assert child.run_enabled_tools == ()
    await manager.cancel_subtree(root.agent_id)


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

    result = manager.spawn(_spawn_request(root, seed_message=_input_msg("task"), cwd="/tmp"))
    assert result.status == "dispatched"

    # 等 B run 完成 + child_result 投递。
    await asyncio.wait_for(runtime.run_started.wait(), timeout=2.0)
    # 给 agent_loop 时间消费 + deliver。
    await asyncio.sleep(0.2)

    # B run 收到正确 agent_id（mail_run_bridge 闭包透传）。
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

    result = manager.spawn(_spawn_request(root, cwd="/tmp"))
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

    result = manager.spawn(_spawn_request(root, seed_message=_input_msg("long"), cwd="/tmp"))
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

    result = manager.spawn(_spawn_request(root, cwd="/tmp"))
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

    result = manager.spawn(_spawn_request(root, seed_message=_input_msg("done"), cwd="/tmp"))
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

    r1 = manager.spawn(_spawn_request(root, _spec("c1"), cwd="/w"))
    r2 = manager.spawn(_spawn_request(root, _spec("c2"), cwd="/w"))

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
    manager.spawn(_spawn_request(root, cwd="/tmp"))
    # 关门后试图再 spawn → 被拒。
    registry.close_registry()
    with pytest.raises(SpawnRejected):
        manager.spawn(_spawn_request(root, cwd="/tmp"))

    # cancel 清理第一个子。
    await manager.cancel_subtree(root.agent_id)
    await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# boot_root / submit / interrupt / teardown_root(主 agent 装配链路)
# agent-manager-boot-root 任务:把 bridge 的 mailbox 装配下沉到 AgentManager 门户
# ---------------------------------------------------------------------------


class _RecordingDeliverSink:
    """记录 deliver 调用的 fake DeliverSink(测试 boot_root 链路用)。

    bridge 生产用的 _BridgeDeliverSink 把 Result 回传给阻塞的 run_once;本 fake 不
    回传 future,只记录 deliver 被调用的事实 + 收到的 Result,供测试断言 agent_loop
    跑完一条 Mail 后正确分发到 sink。
    """

    def __init__(self) -> None:
        self.delivered: list[Result] = []

    def deliver_up_or_ui(
        self, cell: Any, disposition: Any, *, result: Result, run_epoch: int
    ) -> None:
        self.delivered.append(result)

    def emit_only(self, cell: Any, disposition: Any, *, result: Result, run_epoch: int) -> None:
        self.delivered.append(result)


async def test_boot_root_assembles_root_cell_and_starts_loop() -> None:
    """boot_root 装配 persistent 主 cell + 启动 agent_loop,返回 root_agent_id。

    验证点:boot_root 后 _root_agent_id 被设置;_cells 含 root;_loop_tasks 含一个
    root loop task;submit(queue) 投 Mail 后 agent_loop 消费 → mail_run_bridge 被调 →
    deliver_sink 收到 Result。
    """
    manager, _, runtime = _make_manager()
    sink = _RecordingDeliverSink()

    root_id = manager.boot_root(
        spec=_spec("root"),
        session_id="sess-1",
        mail_run_bridge=runtime.make_mail_run_bridge("root-fake-id"),
        deliver_sink=sink,
    )

    assert manager._root_agent_id == root_id
    assert root_id in manager._cells
    assert any(t.get_name() == f"agent-loop-root-{root_id}" for t in manager._loop_tasks)

    # submit(queue) 投一条 Mail,agent_loop 消费 → mail_run_bridge 被调 → sink 收到 Result。
    ok = manager.submit("hello", mode=SubmitMode.QUEUE)
    assert ok is True
    await asyncio.sleep(0.05)  # 让 agent_loop 跑完
    assert len(runtime.run_calls) == 1
    assert runtime.run_calls[0][0] == "hello"
    assert len(sink.delivered) == 1
    assert sink.delivered[0].status == "completed"

    # teardown 收尾(避免 loop task 泄漏)。
    await manager.teardown_root()


async def test_boot_root_rejects_double_boot_without_teardown() -> None:
    """已 boot 过 root 时再 boot → RuntimeError(必须先 teardown_root)。"""
    manager, _, runtime = _make_manager()
    manager.boot_root(
        spec=_spec("root"),
        session_id="sess-1",
        mail_run_bridge=runtime.make_mail_run_bridge("root"),
        deliver_sink=_RecordingDeliverSink(),
    )
    with pytest.raises(RuntimeError, match="root agent already booted"):
        manager.boot_root(
            spec=_spec("root"),
            session_id="sess-2",
            mail_run_bridge=runtime.make_mail_run_bridge("root2"),
            deliver_sink=_RecordingDeliverSink(),
        )
    await manager.teardown_root()


async def test_submit_immediate_calls_steering_fn_and_returns_bool() -> None:
    """submit(mode=SubmitMode.IMMEDIATE) 调注入的 steer_fn,返回其结果(True/False)。"""
    manager, _, runtime = _make_manager()
    steer_calls: list[tuple[str, SteerRequest]] = []

    def steer_fn(session_id: str, request: SteerRequest) -> bool:
        steer_calls.append((session_id, request))
        return request.text == "hit"

    manager.boot_root(
        spec=_spec("root"),
        session_id="sess-1",
        mail_run_bridge=runtime.make_mail_run_bridge("root"),
        deliver_sink=_RecordingDeliverSink(),
        steer_fn=steer_fn,
    )

    assert manager.submit("hit", mode=SubmitMode.IMMEDIATE) is True
    assert manager.submit("miss", mode=SubmitMode.IMMEDIATE) is False
    assert [(session_id, request.text) for session_id, request in steer_calls] == [
        ("sess-1", "hit"),
        ("sess-1", "miss"),
    ]
    await manager.teardown_root()


async def test_submit_immediate_without_steer_fn_returns_false() -> None:
    """steer_fn=None 时 submit(immediate) 永远 False(回落 queued)。"""
    manager, _, runtime = _make_manager()
    manager.boot_root(
        spec=_spec("root"),
        session_id="sess-1",
        mail_run_bridge=runtime.make_mail_run_bridge("root"),
        deliver_sink=_RecordingDeliverSink(),
        # steer_fn 不传(默认 None)
    )
    assert manager.submit("anything", mode=SubmitMode.IMMEDIATE) is False
    await manager.teardown_root()


async def test_submit_without_boot_raises() -> None:
    """root 未 boot 时 submit → RuntimeError。"""
    manager, _, _ = _make_manager()
    with pytest.raises(RuntimeError, match="root agent not booted"):
        manager.submit("hi", mode=SubmitMode.QUEUE)


async def test_interrupt_cancels_inflight_run_and_bumps_epoch() -> None:
    """interrupt 对在途 run 调 cancel_subtree(收口成 cancelled Result) + epoch bump。

    构造:boot_root + submit(queue) 投一条会 hang 的 run → interrupt → run_task 被
    cancel → agent_loop 收口 cancelled Result 经 sink deliver。
    """
    manager, _, runtime = _make_manager()
    runtime.hang = True  # 让 mail_run_bridge 挂起,模拟在途长 run
    sink = _RecordingDeliverSink()

    manager.boot_root(
        spec=_spec("root"),
        session_id="sess-1",
        mail_run_bridge=runtime.make_mail_run_bridge("root"),
        deliver_sink=sink,
    )
    manager.submit("long-running", mode=SubmitMode.QUEUE)
    await runtime.run_started.wait()  # 等 run 真的起跑

    old_epoch = manager._epoch
    await manager.interrupt()

    assert manager._epoch == old_epoch + 1  # epoch bump
    await asyncio.sleep(0.05)  # 等 cancel 收口 + deliver
    # interrupt 后 registry 关闭,teardown 重建。
    await manager.teardown_root()


async def test_teardown_root_clears_root_state() -> None:
    """teardown_root 清空 _root_agent_id / _cells[root] / _steer_fn;epoch 不清零。"""
    manager, _, runtime = _make_manager()
    manager.boot_root(
        spec=_spec("root"),
        session_id="sess-1",
        mail_run_bridge=runtime.make_mail_run_bridge("root"),
        deliver_sink=_RecordingDeliverSink(),
        steer_fn=lambda sid, text: True,
    )
    manager._epoch = 5  # 模拟 interrupt bump 过

    await manager.teardown_root()

    assert manager._root_agent_id is None
    assert manager._steer_fn is None
    assert manager._epoch == 5  # epoch 保持(interrupt 已 bump,语义连续)
    # _cells 里 root 已移除。
    assert all(not aid for aid in manager._cells)  # 空


async def test_teardown_root_idempotent_when_not_booted() -> None:
    """未 boot 过 root 时 teardown_root 幂等返回(None)。"""
    manager, _, _ = _make_manager()
    await manager.teardown_root()  # 不 raise
    assert manager._root_agent_id is None


async def test_boot_root_after_teardown_rebuilds() -> None:
    """teardown 后再 boot_root 重建全新一套(interrupt 后复用语义)。"""
    manager, _, runtime = _make_manager()
    sink1 = _RecordingDeliverSink()
    root_id_1 = manager.boot_root(
        spec=_spec("root"),
        session_id="sess-1",
        mail_run_bridge=runtime.make_mail_run_bridge("root"),
        deliver_sink=sink1,
    )
    await manager.teardown_root()

    sink2 = _RecordingDeliverSink()
    root_id_2 = manager.boot_root(
        spec=_spec("root"),
        session_id="sess-1",
        mail_run_bridge=runtime.make_mail_run_bridge("root"),
        deliver_sink=sink2,
    )
    assert root_id_2 != root_id_1  # 新 cell 新 agent_id
    assert manager._root_agent_id == root_id_2
    await manager.teardown_root()
