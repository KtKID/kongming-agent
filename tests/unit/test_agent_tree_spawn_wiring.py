"""审批 agent_id 维度 + spawn 工具入口 + 用量分桶 单元测试（agent-tree-v0.1 task-5）。

覆盖：
1. **#6 审批 agent_id**：_PendingApproval.agent_id 字段；PendingApprovalView.agent_id
   投影；cancel_by_agent 按 agent_id 批量取消子树 pending future。
2. **#5 spawn 工具入口**：SpawnSubAgentTool 调 dispatcher.spawn（duck-typed router），
   返回 {child_id, status:dispatched}；深度超限返回 rejected tool_result（不打断父 run）。
3. **#8 用量分桶**：UsageTokenManager.record_agent_usage / get_agent_usage / list_agent_usage
   按 agent_id 聚合 Event usage。

验证命令：``uv run pytest tests/unit/test_agent_tree_spawn_wiring.py -v``
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from application.agents.cell import make_root_agent_cell
from application.agents.manager import SpawnRejected
from application.agents.subagent_tools import SpawnAgentRequest
from core.agent_spec import AgentSpec
from core.contracts import ToolContext
from hosts.web.usage.usage_token_v2.manager import AgentUsageBucket
from safety.approval.events import PendingApprovalView
from safety.approval.manager import ApprovalManager
from safety.approval.permissions_manager import PermissionsManager
from tools.agent_workflow_tool import (
    AgentTreeRuntimeRouter,
    build_spawn_subagent_tool,
)

# ---------------------------------------------------------------------------
# #6 审批 agent_id 维度
# ---------------------------------------------------------------------------


def _make_approval_manager(tmp_path: Path) -> ApprovalManager:
    """构造绑定临时 thread permissions 本子的审批门户。"""
    return ApprovalManager(
        permissions_manager=PermissionsManager(tmp_path),
        event_sinks=[],
    )


async def test_pending_approval_carries_agent_id(tmp_path: Path) -> None:
    """_PendingApproval.agent_id 字段存在；request() 透传 agent_id 到 pending。"""
    manager = _make_approval_manager(tmp_path)
    # 发起一个带 agent_id 的审批（不 resolve，靠 cancel_by_agent 收口）。
    agent_id = "child01"
    request_task = asyncio.create_task(
        manager.request(
            channel="generic_chat",
            thread_id="t1",
            cwd="/w",
            tool_name="Bash",
            tool_input={"command": "ls"},
            agent_id=agent_id,
            timeout_ms=60_000,
        )
    )
    await asyncio.sleep(0.05)  # 让 request 进入 await future
    # 断言 pending 记录带 agent_id。
    pendings = [p for p in manager._pending.values()]
    assert len(pendings) == 1
    assert pendings[0].agent_id == agent_id
    assert pendings[0].thread_id == "t1"

    # cancel_by_agent 按 agent_id 取消。
    cancelled = manager.cancel_by_agent(agent_id)
    assert cancelled == 1
    # request 被唤醒（rejected）。
    decision = await asyncio.wait_for(request_task, timeout=1.0)
    assert decision.outcome == "rejected"


async def test_cancel_by_agent_only_matches_target_agent(tmp_path: Path) -> None:
    """cancel_by_agent 只取消目标 agent_id 名下 pending，不动其他 agent。"""
    manager = _make_approval_manager(tmp_path)
    t1 = asyncio.create_task(
        manager.request(
            channel="generic_chat",
            thread_id="t",
            cwd="/w",
            tool_name="Bash",
            tool_input={},
            agent_id="agentA",
        )
    )
    t2 = asyncio.create_task(
        manager.request(
            channel="generic_chat",
            thread_id="t",
            cwd="/w",
            tool_name="Bash",
            tool_input={},
            agent_id="agentB",
        )
    )
    await asyncio.sleep(0.05)

    cancelled = manager.cancel_by_agent("agentA")
    assert cancelled == 1
    # agentA 的 future 已结束（rejected）；agentB 的 future 仍 pending。
    pendings = {p.agent_id: p for p in manager._pending.values()}
    assert pendings["agentA"].future.done()
    assert not pendings["agentB"].future.done()

    # 清理 agentB。
    manager.cancel_by_agent("agentB")
    await asyncio.gather(t1, t2, return_exceptions=True)


def test_pending_approval_view_has_agent_id_field() -> None:
    """PendingApprovalView 含 agent_id 字段（默认 ""，兼容现有构造）。"""
    view = PendingApprovalView(
        request_id="r1",
        channel="generic_chat",
        thread_id="t1",
        agent_id="child02",
        cwd="/w",
        tool_name="Bash",
        tool_input={"command": "ls"},
        metadata={},
        severity="standard",
        matched_rule=None,
        arrived_at_ms=1000,
        timeout_ms=60_000,
    )
    assert view.agent_id == "child02"

    # 默认值兼容（不传 agent_id）。
    view2 = PendingApprovalView(
        request_id="r2",
        channel="generic_chat",
        thread_id="t1",
    )
    assert view2.agent_id == ""


def test_request_signature_accepts_agent_id_kwarg() -> None:
    """request() 签名含 agent_id 关键字参数（默认 ""）。"""
    import inspect

    sig = inspect.signature(ApprovalManager.request)
    assert "agent_id" in sig.parameters
    assert sig.parameters["agent_id"].default == ""


# ---------------------------------------------------------------------------
# #5 spawn 工具入口
# ---------------------------------------------------------------------------


class _RecordingAgentManager:
    """duck-typed AgentManager 桩：记录 spawn 调用，输入为空，输出为桩实例。

    避免 SpawnSubAgentTool 直接 import application（分层冲突）；用 duck-type 验证
    工具调 manager.spawn / manager.get_agent 的契约。
    """

    def __init__(self) -> None:
        self.spawn_calls: list[SpawnAgentRequest] = []
        self.root = make_root_agent_cell(
            spec=AgentSpec(name="root", instructions="i", default_model="m"),
            session_id="t1",
        )

    def get_agent(self, agent_id: str) -> Any:
        if agent_id == "" or agent_id == self.root.agent_id:
            return self.root
        return None

    def spawn(self, request: SpawnAgentRequest) -> Any:
        from application.agents.manager import SpawnResult

        self.spawn_calls.append(request)
        return SpawnResult(child_id="newchild", status="dispatched", task_id="task-xyz")


async def test_spawn_subagent_tool_dispatches_via_router() -> None:
    """SpawnSubAgentTool 调 dispatcher.spawn，返回 {child_id, status:dispatched}。"""
    fake_manager = _RecordingAgentManager()
    router = AgentTreeRuntimeRouter()
    router.bind_dispatcher(fake_manager)
    tool = build_spawn_subagent_tool(router, parent_model="m")

    ctx = ToolContext(
        run_id="r1",
        session_id="t1",
        turn=1,
        call_id="c1",
        metadata={},
        agent_id=fake_manager.root.agent_id,
    )
    content, data = await tool._run(
        {
            "prompt": "summarize the report",
            "name": "summarizer",
            "cwd": "/tmp/work",
        },
        ctx,
    )
    assert data is not None
    assert data["child_id"] == "newchild"
    assert data["status"] == "dispatched"
    assert data["task_id"] == "task-xyz"
    assert "dispatched" in content
    assert len(fake_manager.spawn_calls) == 1
    request = fake_manager.spawn_calls[0]
    assert isinstance(request, SpawnAgentRequest)
    assert request.seed_message.content == "summarize the report"
    assert request.cwd == "/tmp/work"
    assert request.parent_agent_id == fake_manager.root.agent_id
    assert request.spec.name == "summarizer"


async def test_spawn_subagent_tool_returns_rejected_on_spawn_error() -> None:
    """spawn 抛 SpawnRejected → 工具返回 rejected tool_result（不打断父 run）。"""

    class _RejectingManager(_RecordingAgentManager):
        def spawn(self, *args: Any, **kw: Any) -> Any:
            raise SpawnRejected("depth exceeded")

    router = AgentTreeRuntimeRouter()
    router.bind_dispatcher(_RejectingManager())
    tool = build_spawn_subagent_tool(router, parent_model="m")

    ctx = ToolContext(
        run_id="r1",
        session_id="t1",
        turn=1,
        call_id="c1",
        metadata={},
        agent_id="",
    )
    _content, data = await tool._run({"prompt": "x", "name": "c", "cwd": "/w"}, ctx)
    assert data is not None
    assert data["status"] == "rejected"
    assert data["child_id"] is None


async def test_spawn_subagent_tool_raises_when_router_unbound() -> None:
    """未绑定 agent tree runtime → RuntimeError（spawn_subagent 需要 agent-tree）。"""
    router = AgentTreeRuntimeRouter()
    tool = build_spawn_subagent_tool(router)
    ctx = ToolContext(run_id="r", session_id="s", turn=1, call_id="c", metadata={})
    with pytest.raises(RuntimeError, match="not bound"):
        await tool._run({"prompt": "x", "name": "c", "cwd": "/w"}, ctx)


# ---------------------------------------------------------------------------
# #8 用量分桶
# ---------------------------------------------------------------------------


def _make_usage_bucket() -> AgentUsageBucket:
    """构造独立的 AgentUsageBucket（per-agent 用量累加器，不挂在无状态的 UsageTokenManager 上）。"""
    return AgentUsageBucket()


def test_record_and_get_agent_usage_buckets_by_agent_id() -> None:
    """record 按 agent_id 累加；get 查单 agent 桶。"""
    bucket = _make_usage_bucket()
    bucket_a = bucket.record("agentA", prompt_tokens=10, completion_tokens=5, total_tokens=15)
    assert bucket_a == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    bucket.record("agentA", prompt_tokens=20, completion_tokens=0, total_tokens=20)
    assert bucket.get("agentA") == {
        "prompt_tokens": 30,
        "completion_tokens": 5,
        "total_tokens": 35,
    }
    # agentB 独立分桶。
    bucket.record("agentB", prompt_tokens=100, completion_tokens=50, total_tokens=150)
    assert bucket.get("agentB")["total_tokens"] == 150
    # agentA 不受影响。
    assert bucket.get("agentA")["total_tokens"] == 35


def test_get_agent_usage_unknown_agent_returns_zeros() -> None:
    """未知 agent_id 返回全 0 桶。"""
    bucket = _make_usage_bucket()
    assert bucket.get("nobody") == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def test_list_agent_usage_returns_snapshot() -> None:
    """list_all 返回所有 agent 桶快照（会话级上卷）。"""
    bucket = _make_usage_bucket()
    bucket.record("a1", prompt_tokens=1, total_tokens=1)
    bucket.record("a2", prompt_tokens=2, total_tokens=2)
    snapshot = bucket.list_all()
    assert set(snapshot.keys()) == {"a1", "a2"}
    assert snapshot["a1"]["total_tokens"] == 1
    # 快照是副本（改不影响内部）。
    snapshot["a1"]["total_tokens"] = 999
    assert bucket.get("a1")["total_tokens"] == 1
