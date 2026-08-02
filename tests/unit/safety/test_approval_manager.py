"""验证 v0.6 ApprovalManager 的 pending、记忆写回与生命周期。

测试使用真实 PermissionsManager 和 JSON Store，覆盖 allow/deny 记住、thread 身份、
danger 限制、revision 冲突、超时取消、事件 fan-out 以及 prompt bridge。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from core.contracts import ApprovalAction, ApprovalDecision, ApprovalRequest
from safety.approval.events import PendingApprovalView
from safety.approval.manager import (
    ApprovalManager,
    _decision_to_action,
    get_approval_manager,
    make_manager_prompt_fn,
    reset_for_testing,
)
from safety.approval.permissions_manager import PermissionsManager
from safety.approval.rule_models import PermissionRuleRecord
from safety.approval.types import ApprovalMetadataKeys


@dataclass
class _Sink:
    """记录 required/removed 事件并提供等待信号。"""

    pending: list[PendingApprovalView] = field(default_factory=list)
    removed: list[tuple[str, str]] = field(default_factory=list)
    ready: asyncio.Event = field(default_factory=asyncio.Event)

    async def emit_approval_required(self, *, pending: PendingApprovalView) -> None:
        """保存新 pending 并唤醒测试。"""
        self.pending.append(pending)
        self.ready.set()

    async def emit_approval_removed(self, *, request_id: str, reason: str) -> None:
        """保存移除事件。"""
        self.removed.append((request_id, reason))


@pytest.fixture(autouse=True)
def _reset_singleton() -> Iterator[None]:
    """隔离进程级 ApprovalManager 单例。"""
    reset_for_testing()
    yield
    reset_for_testing()


def _manager(tmp_path: Path, sink: _Sink, *, timeout_ms: int = 10_000) -> ApprovalManager:
    """构造绑定真实本子门户的 Manager。"""
    return ApprovalManager(
        permissions_manager=PermissionsManager(tmp_path),
        event_sinks=[sink],
        default_timeout_ms=timeout_ms,
    )


def _remember_metadata(
    *,
    thread_id: str,
    revision: int = 0,
    danger: bool = False,
) -> dict[str, object]:
    """构造引擎冻结后的 remember 元数据。"""
    return {
        ApprovalMetadataKeys.DANGER: danger,
        ApprovalMetadataKeys.REMEMBER_ALLOWED: not danger,
        ApprovalMetadataKeys.REMEMBER_THREAD_ID: thread_id,
        ApprovalMetadataKeys.REMEMBER_REVISION: revision,
        ApprovalMetadataKeys.REMEMBER_RULE: {
            "expression": "read_file",
            "displayText": "记住 read_file 的选择",
            "scopeCwd": None,
        },
        ApprovalMetadataKeys.MATCHED_RULE: ("danger:test" if danger else "permissions:unmatched"),
    }


def _remember_decision(allow: bool) -> dict[str, object]:
    """构造与服务端冻结候选完全一致的 remember 决策。"""
    return {
        "allow": allow,
        "remember": True,
        "rememberRule": {
            "expression": "read_file",
            "displayText": "记住 read_file 的选择",
            "scopeCwd": None,
        },
    }


async def _start_request(
    manager: ApprovalManager,
    sink: _Sink,
    *,
    thread_id: str = "thread-a",
    metadata: dict[str, object] | None = None,
    agent_id: str = "",
) -> tuple[asyncio.Task[ApprovalDecision], PendingApprovalView]:
    """启动一次请求并等待 pending 已发布。"""
    task = asyncio.create_task(
        manager.request(
            channel="generic_chat",
            thread_id=thread_id,
            cwd="/workspace",
            tool_name="read_file",
            tool_input={"path": "/workspace/a.md"},
            metadata=dict(metadata or {}),
            agent_id=agent_id,
        )
    )
    await asyncio.wait_for(sink.ready.wait(), timeout=1)
    return task, sink.pending[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize(("allow", "bucket"), [(True, "allow"), (False, "deny")])
async def test_remember_writes_allow_or_deny_to_frozen_thread(
    tmp_path: Path,
    allow: bool,
    bucket: str,
) -> None:
    """remember=true 将用户二态决定写入 pending 所属 thread。"""
    sink = _Sink()
    manager = _manager(tmp_path, sink)
    task, pending = await _start_request(
        manager,
        sink,
        metadata=_remember_metadata(thread_id="thread-a"),
    )

    accepted = await manager.resolve(
        "thread-a",
        pending.request_id,
        _remember_decision(allow),
    )
    decision = await task
    snapshot = await manager.permissions_manager.snapshot("thread-a")
    other = await manager.permissions_manager.snapshot("thread-b")

    assert accepted is True
    assert decision.outcome == ("approved" if allow else "rejected")
    assert getattr(snapshot, bucket) == (
        PermissionRuleRecord(expression="read_file", scope_cwd=None),
    )
    assert other.revision == 0
    assert sink.removed == [(pending.request_id, "user_decided")]


@pytest.mark.asyncio
async def test_once_decision_does_not_materialize_permissions_file(tmp_path: Path) -> None:
    """remember=false 只完成当前决定，snapshot 保持 revision 0。"""
    sink = _Sink()
    manager = _manager(tmp_path, sink)
    task, pending = await _start_request(
        manager,
        sink,
        metadata=_remember_metadata(thread_id="thread-a"),
    )

    assert await manager.resolve(
        "thread-a",
        pending.request_id,
        {"allow": True, "remember": False},
    )
    decision = await task

    assert decision.outcome == "approved"
    assert (await manager.permissions_manager.snapshot("thread-a")).revision == 0


@pytest.mark.asyncio
async def test_danger_pending_rejects_remember_and_allows_explicit_once(
    tmp_path: Path,
) -> None:
    """danger 卡关闭 remember，同时保留显式一次性 allow/deny。"""
    sink = _Sink()
    manager = _manager(tmp_path, sink)
    task, pending = await _start_request(
        manager,
        sink,
        metadata=_remember_metadata(thread_id="thread-a", danger=True),
    )

    assert pending.danger is True
    assert pending.remember_allowed is False
    assert (
        await manager.resolve(
            "thread-a",
            pending.request_id,
            _remember_decision(True),
        )
        is False
    )
    assert (
        await manager.resolve(
            "thread-a",
            pending.request_id,
            {"allow": False, "remember": False},
        )
        is True
    )
    assert (await task).outcome == "rejected"


@pytest.mark.asyncio
async def test_resolve_rejects_wrong_thread_and_duplicate(tmp_path: Path) -> None:
    """路径 thread 与 pending 身份必须一致，已完成请求不可重复 resolve。"""
    sink = _Sink()
    manager = _manager(tmp_path, sink)
    task, pending = await _start_request(manager, sink)

    assert (
        await manager.resolve(
            "thread-b",
            pending.request_id,
            {"allow": True, "remember": False},
        )
        is False
    )
    assert (
        await manager.resolve(
            "thread-a",
            pending.request_id,
            {"allow": True, "remember": False},
        )
        is True
    )
    await task
    assert (
        await manager.resolve(
            "thread-a",
            pending.request_id,
            {"allow": True, "remember": False},
        )
        is False
    )


@pytest.mark.asyncio
async def test_revision_conflict_keeps_pending_retryable(tmp_path: Path) -> None:
    """stale revision 保存失败返回 false，卡片保持 pending 并可再次决策。"""
    sink = _Sink()
    manager = _manager(tmp_path, sink)
    task, pending = await _start_request(
        manager,
        sink,
        metadata=_remember_metadata(thread_id="thread-a", revision=0),
    )
    await manager.permissions_manager.replace(
        "thread-a",
        allow=[PermissionRuleRecord(expression="list_dir", scope_cwd=None)],
        deny=[],
        expected_revision=0,
    )

    assert (
        await manager.resolve(
            "thread-a",
            pending.request_id,
            _remember_decision(True),
        )
        is False
    )
    assert manager.pending_count == 1
    assert task.done() is False
    assert (
        await manager.resolve(
            "thread-a",
            pending.request_id,
            {"allow": False, "remember": False},
        )
        is True
    )
    assert (await task).outcome == "rejected"


@pytest.mark.asyncio
async def test_remember_rejects_client_scope_tampering(tmp_path: Path) -> None:
    """客户端回传另一 cwd 时保持 snapshot 不变并让 pending 可重试。"""
    sink = _Sink()
    manager = _manager(tmp_path, sink)
    metadata = {
        ApprovalMetadataKeys.DANGER: False,
        ApprovalMetadataKeys.REMEMBER_ALLOWED: True,
        ApprovalMetadataKeys.REMEMBER_THREAD_ID: "thread-a",
        ApprovalMetadataKeys.REMEMBER_REVISION: 0,
        ApprovalMetadataKeys.REMEMBER_RULE: {
            "expression": "run_shell(git status:*)",
            "displayText": "记住 /repo/a 中的 git status",
            "scopeCwd": "/repo/a",
        },
    }
    task, pending = await _start_request(manager, sink, metadata=metadata)

    accepted = await manager.resolve(
        "thread-a",
        pending.request_id,
        {
            "allow": True,
            "remember": True,
            "rememberRule": {
                "expression": "run_shell(git status:*)",
                "displayText": "记住 /repo/a 中的 git status",
                "scopeCwd": "/repo/b",
            },
        },
    )

    assert accepted is False
    assert (await manager.permissions_manager.snapshot("thread-a")).revision == 0
    assert manager.pending_count == 1
    assert await manager.resolve(
        "thread-a",
        pending.request_id,
        {"allow": False, "remember": False},
    )
    assert (await task).outcome == "rejected"


@pytest.mark.asyncio
async def test_timeout_fails_closed_and_cleans_task(tmp_path: Path) -> None:
    """无人处理时超时拒绝，并统一清理 pending 与 timeout task。"""
    sink = _Sink()
    manager = _manager(tmp_path, sink, timeout_ms=10)
    task, pending = await _start_request(manager, sink)

    decision = await task

    assert decision.outcome == "rejected"
    assert decision.metadata["source"] == "manager_timeout"
    assert manager.pending_count == 0
    assert manager.timeout_task_count == 0
    assert sink.removed == [(pending.request_id, "timeout")]


@pytest.mark.asyncio
async def test_cancel_by_agent_releases_matching_pending(tmp_path: Path) -> None:
    """子树取消按 agent_id 释放对应 pending。"""
    sink = _Sink()
    manager = _manager(tmp_path, sink)
    task, _pending = await _start_request(manager, sink, agent_id="child-1")

    assert manager.cancel_by_agent("child-1", reason="subtree_cancelled") == 1
    decision = await task

    assert decision.outcome == "rejected"
    assert decision.metadata["reason"] == "subtree_cancelled"


@pytest.mark.asyncio
async def test_make_manager_prompt_fn_preserves_root_thread_and_agent(
    tmp_path: Path,
) -> None:
    """prompt bridge 使用绑定的 root thread，并保留 child agent 审计身份。"""
    sink = _Sink()
    manager = _manager(tmp_path, sink)
    prompt = make_manager_prompt_fn(
        manager,
        "root-thread",
        default_cwd="/workspace",
    )
    request = ApprovalRequest(
        run_id="run-1",
        session_id="child-session",
        turn=1,
        call_id="call-1",
        tool_name="list_dir",
        arguments={"path": "/workspace"},
        metadata={"agent_id": "child-agent"},
    )
    task = asyncio.create_task(prompt(request))
    await asyncio.wait_for(sink.ready.wait(), timeout=1)
    pending = sink.pending[0]

    assert pending.thread_id == "root-thread"
    assert pending.agent_id == "child-agent"
    assert pending.cwd == "/workspace"
    assert await manager.resolve(
        "root-thread",
        pending.request_id,
        {"allow": True, "remember": False},
    )
    assert await task is ApprovalAction.ACCEPT_ONCE
    assert getattr(prompt, "__action_aware__") is True


def test_singleton_uses_explicit_permissions_manager(tmp_path: Path) -> None:
    """单例首次装配复用调用方提供的 PermissionsManager。"""
    permissions = PermissionsManager(tmp_path)

    manager = get_approval_manager(permissions_manager=permissions)

    assert manager.permissions_manager is permissions
    assert manager.auto_approve_task_count == 0


def test_decision_to_action_is_once_only() -> None:
    """记忆已在 Manager 内完成，运行时 action 只表达当前调用。"""
    assert _decision_to_action(ApprovalDecision(outcome="approved")) is ApprovalAction.ACCEPT_ONCE
    assert _decision_to_action(ApprovalDecision(outcome="rejected")) is ApprovalAction.REJECT
