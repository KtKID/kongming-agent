"""Web agent-tree wiring tests for HostDispatcher-owned root trees.

本脚本验证 host-dispatch-consolidation 后的真实 owner：
HostDispatcher 持有 AgentManager / root mailbox / agent loop，并在首次启动时把
自身按 session_id 绑定到 AgentTreeRuntimeRouter。ThreadManager 只持有 cell fleet
和 pending input 队列，不保存 runtime router。

关键函数：
- ``_FakeRuntime``：提供 agent_spec 和可观察 run 调用。
- ``_make_manager``：装配 ThreadManager，runtime factory 返回 HostDispatcher。
- ``test_submit_binds_agent_tree_runtime_router``：首次 submit 后按 session 绑定 dispatcher。
- ``test_runtime_router_resolves_distinct_dispatchers_per_thread``：多 thread 独立绑定。
- ``test_thread_manager_has_no_runtime_router_middleman``：固定删除 ThreadManager 中转入口。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from application.agents.manager import SubmitMode
from core.agent_spec import AgentSpec
from core.contracts import ToolContext
from core.message import Message
from core.result import Result
from hosts.shared.host_dispatcher import HostDispatcher
from hosts.web.app_support.host_adapter import WebHostAdapter
from hosts.web.threads.manager import ThreadManager
from infrastructure.config.models import Config
from tools.agent_workflow_tool import AgentTreeRuntimeRouter


class _FakeRuntime:
    """测试用 runtime，输入为空，输出为可被 HostDispatcher 调用的实例。"""

    def __init__(self) -> None:
        self._spec = AgentSpec(name="root", instructions="i", default_model="fake")
        self.run_started: asyncio.Event = asyncio.Event()
        self.run_calls: list[tuple[str, str, str]] = []

    @property
    def agent_spec(self) -> AgentSpec:
        """返回 root agent spec，输入为空，输出为稳定测试 spec。"""
        return self._spec

    async def run(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        event_context: dict[str, Any] | None = None,
        agent_id: str = "",
    ) -> Result:
        """记录一次 run 调用，输入为文本和上下文，输出 completed Result。"""
        del event_context
        self.run_calls.append((user_input, session_id or "", agent_id))
        self.run_started.set()
        return Result(
            run_id=f"run-{len(self.run_calls)}",
            session_id=session_id or "",
            status="completed",
            final_message=Message.assistant(f"reply:{user_input}"),
            turn_count=1,
        )

    async def aclose(self) -> None:
        """关闭 fake runtime，输入为空，输出为空。"""


def _make_cfg() -> Config:
    """构造最小 Web config，输入为空，输出 Config。"""
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
    agent_tree_runtime_router: AgentTreeRuntimeRouter | None = None,
) -> tuple[ThreadManager, str]:
    """装配 ThreadManager 和一个 thread，输入为 runtime/router，输出 manager/thread_id。"""

    async def factory(
        thread_id: str,
        preset_id: str,
        adapter: WebHostAdapter,
        sinks: list[Any],
    ) -> tuple[_FakeRuntime, HostDispatcher]:
        """runtime factory，输入为 thread 装配参数，输出 runtime + HostDispatcher。"""
        del preset_id, adapter, sinks
        dispatcher = HostDispatcher(
            runtime=runtime,  # type: ignore[arg-type]
            session_id=thread_id,
            agent_tree_runtime_router=agent_tree_runtime_router,
        )
        return runtime, dispatcher

    manager = ThreadManager(_make_cfg(), kongming_home=tmp_path, runtime_factory=factory)
    meta = await manager.create_thread("demo", "preset-a")
    return manager, meta.id


def _tool_context(session_id: str) -> ToolContext:
    """构造 ToolContext，输入为 session_id，输出可用于 router.resolve 的上下文。"""
    return ToolContext(run_id="run", session_id=session_id, turn=1, call_id="call", metadata={})


async def test_submit_binds_agent_tree_runtime_router(tmp_path: Path) -> None:
    """首次 submit 后，AgentTreeRuntimeRouter 按 session 解析到同一个 HostDispatcher。"""
    runtime = _FakeRuntime()
    router = AgentTreeRuntimeRouter()
    manager, thread_id = await _make_manager(
        tmp_path,
        runtime=runtime,
        agent_tree_runtime_router=router,
    )
    cell = await manager.boot_or_attach(thread_id)

    assert router.resolve(_tool_context(thread_id)) is None
    await cell.host_dispatcher.submit("hello", mode=SubmitMode.QUEUE)

    dispatcher = router.resolve(_tool_context(thread_id))
    assert dispatcher is cell.host_dispatcher
    assert cell.host_dispatcher.agent_manager is not None
    root_agent_id = cell.host_dispatcher.agent_manager._root_agent_id
    assert root_agent_id is not None
    root = cell.host_dispatcher.agent_manager.get_agent(root_agent_id)
    assert root is not None
    assert runtime.run_calls == [("hello", thread_id, root.agent_id)]
    await manager.aclose_all()


async def test_runtime_router_resolves_distinct_dispatchers_per_thread(tmp_path: Path) -> None:
    """两个 Web thread 首次 submit 后，各自绑定独立 HostDispatcher。"""
    runtime = _FakeRuntime()
    router = AgentTreeRuntimeRouter()
    manager, first_thread_id = await _make_manager(
        tmp_path,
        runtime=runtime,
        agent_tree_runtime_router=router,
    )
    second_meta = await manager.create_thread("second", "preset-a")

    first_cell = await manager.boot_or_attach(first_thread_id)
    second_cell = await manager.boot_or_attach(second_meta.id)

    await first_cell.host_dispatcher.submit("one", mode=SubmitMode.QUEUE)
    await second_cell.host_dispatcher.submit("two", mode=SubmitMode.QUEUE)

    first_dispatcher = router.resolve(_tool_context(first_thread_id))
    second_dispatcher = router.resolve(_tool_context(second_meta.id))
    assert first_dispatcher is first_cell.host_dispatcher
    assert second_dispatcher is second_cell.host_dispatcher
    assert first_dispatcher is not second_dispatcher
    await manager.aclose_all()


async def test_thread_manager_has_no_runtime_router_middleman(tmp_path: Path) -> None:
    """ThreadManager 只管理 cell fleet，runtime router 绑定归 HostDispatcher。"""
    runtime = _FakeRuntime()
    manager, _thread_id = await _make_manager(tmp_path, runtime=runtime)

    assert not hasattr(manager, "set_spawn_handle")
    assert not hasattr(manager, "_spawn_handle")
    await manager.aclose_all()
