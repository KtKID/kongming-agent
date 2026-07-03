"""ThreadManager agent-tree opt-in 接入集成测试（task-4 验证闭环）。

验证 manager 的 opt-in 副路径（init_root_agent / enqueue_user_mail /
interrupt_agent_tree）与 agent_loop + mailbox 闭环可跑通，且**不破坏**现有
pending_inputs / current_run_task 生产路径（并存）。

覆盖（对照 README DoD / E2E）：
1. E2E-001：用户消息 → root_agent mailbox 入队 → agent_loop run → idle。
2. E2E-002：旧 epoch mail 注入被丢弃。
3. E2E-003：interrupt_agent_tree → cancel_subtree → A 回 idle 不复活。
4. opt-in 并存：不调 init_root_agent 的 cell 仍走旧 pending_inputs 路径无回归。

验证命令：``uv run pytest tests/unit/web/test_agent_tree_manager_wiring.py -v``
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from core.agent_spec import AgentSpec
from core.message import Message
from core.result import Result
from hosts.web.app_support.host_adapter import WebHostAdapter
from hosts.web.threads.manager import ThreadManager
from infrastructure.config.models import Config

# ---------------------------------------------------------------------------
# fake runtime：带 agent_spec + 可控行为的 run
# ---------------------------------------------------------------------------


class _FakeRuntime:
    """带 agent_spec + 可控 run 的 fake runtime，输入为空，输出为装配后的实例。

    agent_spec 暴露给 manager.init_root_agent；run 行为可控（立即完成 / 挂起 / 抛错），
    便于构造段2（在途 cancel）等场景。
    """

    def __init__(self) -> None:
        self.aclose = AsyncMock(return_value=None)
        self._spec = AgentSpec(name="root", instructions="i", default_model="fake")
        self.run_started: asyncio.Event = asyncio.Event()
        self.run_calls: list[str] = []
        self.hang = False  # True=run 挂起（用于 cancel 测试）

    @property
    def agent_spec(self) -> AgentSpec:
        return self._spec

    async def run(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        reasoning_effort: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
        agent_id: str = "",
    ) -> Result:
        self.run_calls.append(user_input)
        self.run_started.set()
        if self.hang:
            await asyncio.sleep(10.0)  # 模拟在途 LLM await
        return Result(
            run_id=f"run-{len(self.run_calls)}",
            session_id=session_id or "?",
            status="completed",
            final_message=Message.assistant(f"reply:{user_input}"),
            turn_count=1,
        )


def _make_cfg() -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {
                "enabled": True,
                "idle_timeout_seconds": 60,
                "idle_check_interval_seconds": 10,
            },
        }
    )


async def _make_manager(
    tmp_path: Path,
    *,
    runtime: _FakeRuntime,
    spawn_handle: Any | None = None,
    approval_manager: Any | None = None,
) -> tuple[ThreadManager, _FakeRuntime, str]:
    """装配 manager + fake runtime + 一个 thread，返回 (manager, runtime, thread_id)。

    spawn_handle / approval_manager 可选注入（P0-1 装配测试用）。
    """

    async def factory(
        thread_id: str,
        preset_id: str,
        adapter: WebHostAdapter,
        sinks: list[Any],
    ) -> tuple[_FakeRuntime, Any]:
        del thread_id, preset_id, adapter, sinks
        # bridge 不被 agent_loop 副路径用到（run_fn 走 runtime.run）；给个最小桩。
        bridge = _NullBridge()
        return runtime, bridge

    manager = ThreadManager(
        _make_cfg(),
        kongming_home=tmp_path,
        runtime_factory=factory,
        approval_manager=approval_manager,
    )
    if spawn_handle is not None:
        manager.set_spawn_handle(spawn_handle)
    meta = await manager.create_thread("demo", "preset-a")
    return manager, runtime, meta.id


class _NullBridge:
    """最小 bridge 桩：暴露 run_once 给旧 pending_inputs 路径（agent_loop 副路径走 runtime.run）。"""

    session_id = "demo"

    async def run_once(
        self,
        user_input: str,
        *,
        reasoning_effort: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
    ) -> Result:
        return Result(
            run_id="r",
            session_id="demo",
            status="completed",
            final_message=Message.assistant("legacy"),
            turn_count=1,
        )


# ---------------------------------------------------------------------------
# E2E-001：用户消息 → mailbox → run → idle
# ---------------------------------------------------------------------------


async def test_e2e_user_message_runs_and_returns_to_idle(tmp_path: Path) -> None:
    """E2E-001：enqueue_user_mail → root_agent.mailbox → agent_loop run → idle。"""
    runtime = _FakeRuntime()
    manager, runtime, thread_id = await _make_manager(tmp_path, runtime=runtime)

    cell = await manager.boot_or_attach(thread_id)
    root = await manager.init_root_agent(thread_id)

    await manager.enqueue_user_mail(thread_id, "hello")
    # 等 agent_loop 消费 + run 完成。
    await asyncio.wait_for(runtime.run_started.wait(), timeout=2.0)
    await asyncio.sleep(0.1)

    assert runtime.run_calls == ["hello"]
    assert root.state == "idle"
    assert root.run_task is None
    assert cell.epoch == 0

    await manager.aclose_all()


# ---------------------------------------------------------------------------
# E2E-002：旧 epoch mail 注入被丢弃
# ---------------------------------------------------------------------------


async def test_e2e_old_epoch_mail_dropped(tmp_path: Path) -> None:
    """E2E-002：bump_epoch 后注入旧 epoch 内部 mail → 被门卫拦截，不复活。"""
    runtime = _FakeRuntime()
    manager, runtime, thread_id = await _make_manager(tmp_path, runtime=runtime)
    # 先 boot 再 init（init_root_agent 要求 cell 已在 _cells）。
    cell = await manager.boot_or_attach(thread_id)
    root = await manager.init_root_agent(thread_id)
    assert root is not None

    # bump 到 2，注入 epoch=1 的旧内部 mail。
    cell.bump_epoch()
    cell.bump_epoch()
    assert cell.epoch == 2
    from core.mail import Mail

    await root.mailbox.put(
        Mail(
            kind="child_result",
            sender="child01",
            recipient_agent_id=root.agent_id,
            task_id="t1",
            epoch=1,
            payload=Message.assistant("ghost"),
        )
    )
    await asyncio.sleep(0.15)
    # 旧 mail 被门卫拦截：run 未启动（不复活说话）。
    assert runtime.run_calls == []
    assert root.state == "idle"

    await manager.aclose_all()


# ---------------------------------------------------------------------------
# E2E-003：interrupt → cancel_subtree → A 回 idle 不复活
# ---------------------------------------------------------------------------


async def test_e2e_interrupt_cancels_run_and_returns_idle(tmp_path: Path) -> None:
    """E2E-003：interrupt_agent_tree → cancel run_task → 段2收口 → A 回 idle。"""
    runtime = _FakeRuntime()
    runtime.hang = True  # run 挂起，模拟在途 LLM await
    manager, runtime, thread_id = await _make_manager(tmp_path, runtime=runtime)
    root = await manager.init_root_agent(thread_id)

    await manager.enqueue_user_mail(thread_id, "long")
    await asyncio.wait_for(runtime.run_started.wait(), timeout=2.0)
    assert root.state == "running"
    assert root.run_task is not None

    # interrupt（cancel_subtree + bump_epoch + purge）。
    cell = await manager.boot_or_attach(thread_id)
    interrupted = await manager.interrupt_agent_tree(thread_id, reason="user_interrupt")
    assert interrupted is True
    # bump 了 epoch。
    assert cell.epoch >= 1
    await asyncio.sleep(0.1)

    # 段2 收口：run_task 被 cancel → 约束16 收口 cancelled Result → Cancelled Outcome。
    assert root.run_task is None
    assert root.state == "idle"

    await manager.aclose_all()


async def test_interrupt_no_active_run_returns_false(tmp_path: Path) -> None:
    """无在途 run 时 interrupt 返回 False（无需 cancel，仍 bump+purge）。"""
    runtime = _FakeRuntime()
    manager, runtime, thread_id = await _make_manager(tmp_path, runtime=runtime)
    await manager.init_root_agent(thread_id)

    interrupted = await manager.interrupt_agent_tree(thread_id)
    assert interrupted is False
    cell = await manager.boot_or_attach(thread_id)
    assert cell.epoch >= 1

    await manager.aclose_all()


async def test_interrupt_without_root_agent_returns_false(tmp_path: Path) -> None:
    """boot 默认 init root agent（P0-2）：interrupt 在无在途 run 时返回 False。

    旧 opt-in 语义下 root_agent 默认 None，本测试验证该 None 分支。P0-2 改为
    boot 默认通电后 root_agent 恒非 None（agent_loop 通电）；本测试调整为验证
    「无在途 run 时 interrupt 返回 False 且 bump epoch」（防御性 None 分支保留在
    interrupt_agent_tree 内，仅 boot 异常路径触发）。
    """
    runtime = _FakeRuntime()
    manager, runtime, thread_id = await _make_manager(tmp_path, runtime=runtime)
    # boot 默认 init root agent（P0-2）。
    cell = await manager.boot_or_attach(thread_id)
    assert cell.root_agent is not None
    interrupted = await manager.interrupt_agent_tree(thread_id)
    assert interrupted is False

    await manager.aclose_all()


# ---------------------------------------------------------------------------
# opt-in 并存：旧 pending_inputs 路径不回归
# ---------------------------------------------------------------------------


async def test_opt_in_does_not_break_pending_inputs_path(tmp_path: Path) -> None:
    """boot 默认 init root agent 后仍可用 submit_user_input（生产路径不回归）。

    对抗式审查 P0-2：boot 时默认装配 root_agent + agent_loop（通电），但旧
    pending_inputs / current_run_task 机制必须仍然可用（并存的硬约束）。
    """
    runtime = _FakeRuntime()
    manager, runtime, thread_id = await _make_manager(tmp_path, runtime=runtime)
    cell = await manager.boot_or_attach(thread_id)

    # P0-2：boot 默认初始化 root_agent（agent_loop + interrupt 通电）。
    assert cell.root_agent is not None
    assert cell.registry is not None
    assert cell.epoch == 0

    # submit_user_input 仍走旧 pending_inputs 路径（不抛错 = 无回归）。
    result = await manager.submit_user_input(thread_id, "via legacy path")
    assert result.accepted is True

    await manager.aclose_all()


async def test_init_root_agent_idempotent(tmp_path: Path) -> None:
    """init_root_agent 幂等：重复调用返回同一 root AgentCell。"""
    runtime = _FakeRuntime()
    manager, runtime, thread_id = await _make_manager(tmp_path, runtime=runtime)

    root1 = await manager.init_root_agent(thread_id)
    root2 = await manager.init_root_agent(thread_id)
    assert root1 is root2
    assert root1.agent_id  # 8-hex

    await manager.aclose_all()


async def test_aclose_all_cancels_agent_loop(tmp_path: Path) -> None:
    """aclose_all（server shutdown）cancel agent_loop 协程（树销毁场景）。"""
    runtime = _FakeRuntime()
    runtime.hang = True
    manager, runtime, thread_id = await _make_manager(tmp_path, runtime=runtime)
    await manager.init_root_agent(thread_id)
    await manager.enqueue_user_mail(thread_id, "x")
    await asyncio.wait_for(runtime.run_started.wait(), timeout=2.0)

    assert len(manager._agent_loop_tasks) == 1
    loop_task = next(iter(manager._agent_loop_tasks))
    assert not loop_task.done()

    await manager.aclose_all()
    # agent_loop 被 cancel（树销毁）。
    assert loop_task.done()
    assert len(manager._agent_loop_tasks) == 0


# ---------------------------------------------------------------------------
# 对抗式审查 P0-1：AgentManager 装配 + spawn_subagent 工具注册
# ---------------------------------------------------------------------------


async def test_init_root_agent_wires_agent_manager_and_binds_handle(
    tmp_path: Path,
) -> None:
    """init_root_agent 装配 per-cell AgentManager 并按 session 绑定到 spawn_handle（P0-1）。

    验证：spawn_handle 注入后，init_root_agent 不仅启动 agent_loop，还构造
    AgentManager（spawn 主路径门户）并把 root AgentCell 登记进 manager 注册表，
    最后按 session_id 绑定到 handle（工具运行期按 ctx.session_id 解析）。
    """
    from tools.agent_workflow_tool import AgentTreeSpawnHandle

    runtime = _FakeRuntime()
    handle = AgentTreeSpawnHandle()
    manager, runtime, thread_id = await _make_manager(
        tmp_path, runtime=runtime, spawn_handle=handle
    )

    root = await manager.init_root_agent(thread_id)

    # handle 按 session 绑定了 AgentManager。
    bound = handle._managers_by_session_id.get(thread_id)
    assert bound is not None
    # root AgentCell 登记进 manager 注册表（spawn_subagent 用 agent_id 查父）。
    assert bound.get_agent(root.agent_id) is root
    await manager.aclose_all()


async def test_init_root_agent_without_spawn_handle_skips_agent_manager(
    tmp_path: Path,
) -> None:
    """无 spawn_handle（CLI / 测试路径）时 init_root_agent 不装配 AgentManager（P0-1）。

    root agent + agent_loop 仍通电；仅 spawn 副路径不通电（不抛错）。
    """
    runtime = _FakeRuntime()
    manager, runtime, thread_id = await _make_manager(tmp_path, runtime=runtime)
    assert manager._spawn_handle is None  # 未注入

    root = await manager.init_root_agent(thread_id)
    assert root is not None  # agent_loop 仍通电
    await manager.aclose_all()


async def test_agent_manager_wires_approval_canceller(tmp_path: Path) -> None:
    """init_root_agent 装配的 AgentManager approval_canceller 取自 ApprovalManager（P0-1）。"""
    from safety.approval.manager import ApprovalManager
    from safety.approval.rules import ApprovalRules

    runtime = _FakeRuntime()
    approval_manager = ApprovalManager(rules=ApprovalRules(), event_sinks=[])

    async def factory(
        thread_id: str,
        preset_id: str,
        adapter: WebHostAdapter,
        sinks: list[Any],
    ) -> tuple[_FakeRuntime, Any]:
        del thread_id, preset_id, adapter, sinks
        return runtime, _NullBridge()

    from tools.agent_workflow_tool import AgentTreeSpawnHandle

    spawn_handle = AgentTreeSpawnHandle()
    mgr = ThreadManager(
        _make_cfg(),
        kongming_home=tmp_path,
        runtime_factory=factory,
        approval_manager=approval_manager,
    )
    mgr.set_spawn_handle(spawn_handle)
    meta = await mgr.create_thread("demo", "preset-a")

    await mgr.init_root_agent(meta.id)

    bound = spawn_handle._managers_by_session_id.get(meta.id)
    assert bound is not None
    # approval_canceller 是 approval_manager.cancel_by_agent 绑定方法。
    assert bound._approval_canceller is not None
    # 调用幂等（未知 agent_id 返回 0，不抛错）。
    assert bound._approval_canceller("nobody") == 0
    await mgr.aclose_all()


async def test_spawn_subagent_tool_registered_in_default_registry() -> None:
    """register_spawn_subagent_tool 把 spawn_subagent 注册进 ToolRegistry（P0-1）。

    验证工具名出现在 registry.names()，证明 LLM tool 列表里能看到 spawn_subagent。
    """
    from tools import register_spawn_subagent_tool
    from tools.agent_workflow_tool import AgentTreeSpawnHandle
    from tools.runtime.registry import ToolRegistry

    registry = ToolRegistry()
    handle = AgentTreeSpawnHandle()
    register_spawn_subagent_tool(registry, handle)
    assert "spawn_subagent" in registry.names()


async def test_spawn_handle_per_session_resolution(tmp_path: Path) -> None:
    """AgentTreeSpawnHandle 按 session 分桶：两个 thread 各自的 AgentManager 独立（P0-1）。"""
    from core.contracts import ToolContext
    from tools.agent_workflow_tool import AgentTreeSpawnHandle

    runtime = _FakeRuntime()
    handle = AgentTreeSpawnHandle()

    async def factory(
        thread_id: str,
        preset_id: str,
        adapter: WebHostAdapter,
        sinks: list[Any],
    ) -> tuple[_FakeRuntime, Any]:
        del preset_id, adapter, sinks
        return runtime, _NullBridge()

    mgr = ThreadManager(_make_cfg(), kongming_home=tmp_path, runtime_factory=factory)
    mgr.set_spawn_handle(handle)
    meta_a = await mgr.create_thread("a", "preset-a")
    meta_b = await mgr.create_thread("b", "preset-a")

    root_a = await mgr.init_root_agent(meta_a.id)
    root_b = await mgr.init_root_agent(meta_b.id)

    # 按 session 解析出各自的 manager。
    ctx_a = ToolContext(run_id="r", session_id=meta_a.id, turn=1, call_id="c", metadata={})
    ctx_b = ToolContext(run_id="r", session_id=meta_b.id, turn=1, call_id="c", metadata={})
    mgr_a = handle.get(ctx_a)
    mgr_b = handle.get(ctx_b)
    assert mgr_a is not None and mgr_b is not None
    assert mgr_a is not mgr_b  # 独立 AgentManager
    # 各自的 root 登记正确。
    assert mgr_a.get_agent(root_a.agent_id) is root_a
    assert mgr_b.get_agent(root_b.agent_id) is root_b
    await mgr.aclose_all()


# ---------------------------------------------------------------------------
# 对抗式审查 P0-2：boot 默认 init_root_agent（agent_loop + interrupt 通电）
# ---------------------------------------------------------------------------


async def test_boot_default_inits_root_agent(tmp_path: Path) -> None:
    """boot_or_attach 默认装配 root_agent + 启动 agent_loop（P0-2 通电）。

    无需显式调 init_root_agent：boot 完成即有 root_agent + 常驻 agent_loop task，
    让 interrupt_agent_tree（tree-aware 副路径）能触发。
    """
    runtime = _FakeRuntime()
    manager, runtime, thread_id = await _make_manager(tmp_path, runtime=runtime)

    cell = await manager.boot_or_attach(thread_id)
    # boot 默认 init：root_agent 已装配。
    assert cell.root_agent is not None
    # agent_loop task 已启动（常驻消费协程）。
    assert len(manager._agent_loop_tasks) == 1

    await manager.aclose_all()


async def test_boot_default_init_lets_interrupt_fire(tmp_path: Path) -> None:
    """boot 默认通电后 interrupt_agent_tree 能触发 cancel（P0-2 interrupt 通电）。"""
    runtime = _FakeRuntime()
    runtime.hang = True  # run 挂起，模拟在途 LLM await
    manager, runtime, thread_id = await _make_manager(tmp_path, runtime=runtime)

    # boot 已默认 init root agent；直接 enqueue + interrupt。
    root = (await manager.boot_or_attach(thread_id)).root_agent
    assert root is not None
    await manager.enqueue_user_mail(thread_id, "long")
    await asyncio.wait_for(runtime.run_started.wait(), timeout=2.0)
    assert root.state == "running"

    interrupted = await manager.interrupt_agent_tree(thread_id, reason="user_interrupt")
    assert interrupted is True
    await asyncio.sleep(0.1)
    assert root.run_task is None
    assert root.state == "idle"

    await manager.aclose_all()


async def test_boot_default_init_does_not_break_legacy_submit(tmp_path: Path) -> None:
    """boot 默认 init 后 submit_user_input 仍走 pending_inputs（P0-2 渐进切换）。

    WS 输入切换到 mailbox 延后；当前 submit_user_input 仍用旧路径，不抛错。
    """
    runtime = _FakeRuntime()
    manager, runtime, thread_id = await _make_manager(tmp_path, runtime=runtime)
    cell = await manager.boot_or_attach(thread_id)
    assert cell.root_agent is not None  # 默认通电

    result = await manager.submit_user_input(thread_id, "legacy still works")
    assert result.accepted is True

    await manager.aclose_all()
