"""验证 ``schedule`` 工具遵循 safety v0.6 的 thread permissions 审批链。

本文件通过真实 ``SafetyDecisionEngine``、``PermissionsManager`` 和
``ApprovalManager`` 覆盖 default:ask、remember 写回、同 thread 静默命中与跨
thread 隔离。人工选择由测试驱动，安全决策、规则生成和持久化均走生产实现。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from core.contracts import ApprovalDecision, ApprovalOutcome, ApprovalProvider, ApprovalRequest
from infrastructure.config.models import Config, ModelSelectionConfig
from safety.approval.chain import SafetyGatedApproval, build_safety_chain
from safety.approval.events import PendingApprovalView
from safety.approval.manager import ApprovalManager, make_manager_prompt_fn
from safety.approval.permissions_manager import PermissionsManager
from safety.approval.rule_models import PermissionRuleRecord
from safety.approval.types import ApprovalMetadataKeys
from safety.auto_approval.disposition import ApprovalDispositionMode
from safety.guards.danger import DangerGuard
from tools.runtime.approval import InteractiveApproval


@dataclass
class _RecordingApproval:
    """记录进入人工审批终点的请求并返回固定决定。"""

    outcome: ApprovalOutcome = "approved"
    requests: list[ApprovalRequest] = field(default_factory=list)

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """保存请求并返回测试指定的用户决定。"""
        self.requests.append(request)
        return ApprovalDecision(outcome=self.outcome)


@dataclass
class _ApprovalSink:
    """记录 ApprovalManager 发布和移除的 pending 审批。"""

    pending: list[PendingApprovalView] = field(default_factory=list)
    removed: list[tuple[str, str]] = field(default_factory=list)
    ready: asyncio.Event = field(default_factory=asyncio.Event)

    async def emit_approval_required(self, *, pending: PendingApprovalView) -> None:
        """保存新 pending 并唤醒等待中的测试。"""
        self.pending.append(pending)
        self.ready.set()

    async def emit_approval_removed(self, *, request_id: str, reason: str) -> None:
        """记录已完成 pending 的移除原因。"""
        self.removed.append((request_id, reason))


@dataclass(frozen=True)
class _UserModeResolver:
    """让本测试的 default:ask 固定进入用户审批模式。"""

    def mode_for(self, cwd: str) -> ApprovalDispositionMode:
        """返回 user 模式；cwd 仅用于满足生产门户合同。"""
        return ApprovalDispositionMode.USER


def _config() -> Config:
    """构造不访问外部模型的最小配置。"""
    return Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"))


def _request(*, thread_id: str, call_id: str, cwd: Path) -> ApprovalRequest:
    """构造携带顶层 thread 与 cwd 的 schedule 请求。"""
    return ApprovalRequest(
        run_id="run-schedule-v06",
        session_id="child-session",
        turn=1,
        call_id=call_id,
        tool_name="schedule",
        arguments={
            "action": "create",
            "name": "daily-report",
            "schedule": "every 1d",
        },
        metadata={"cwd": cwd.resolve().as_posix(), "thread_id": thread_id},
    )


def _build_chain(
    *,
    kongming_home: Path,
    permissions: PermissionsManager,
    interactive_approval: ApprovalProvider,
) -> SafetyGatedApproval:
    """用真实 v0.6 owner 装配 schedule 测试链。"""
    return build_safety_chain(
        _config(),
        interactive_approval=interactive_approval,
        permissions_manager=permissions,
        danger_guard=DangerGuard(kongming_home=kongming_home),
        disposition_resolver=_UserModeResolver(),
    )


def _remember_decision(pending: PendingApprovalView) -> dict[str, object]:
    """把服务端冻结候选原样回传为允许并记住决定。"""
    rule = pending.remember_rule
    assert rule is not None
    return {
        "allow": True,
        "remember": True,
        "rememberRule": {
            "expression": rule.expression,
            "displayText": rule.display_text,
            "scopeCwd": rule.scope_cwd,
        },
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_schedule_unmatched_request_uses_v06_default_ask(tmp_path: Path) -> None:
    """schedule 未命中本子时冻结当前 thread 的 exact-tool 记忆候选。"""
    permissions = PermissionsManager(tmp_path)
    interactive = _RecordingApproval()
    chain = _build_chain(
        kongming_home=tmp_path,
        permissions=permissions,
        interactive_approval=interactive,
    )

    decision = await chain.decide(
        _request(thread_id="thread-a", call_id="call-schedule-1", cwd=tmp_path)
    )

    assert decision.outcome == "approved"
    assert decision.metadata[ApprovalMetadataKeys.DECISION_CLASS] == "explicit_consent"
    assert decision.metadata[ApprovalMetadataKeys.DECISION_SOURCE] == "user_approval"
    assert decision.metadata[ApprovalMetadataKeys.MATCHED_RULE] == "default:ask"
    assert len(interactive.requests) == 1
    metadata = interactive.requests[0].metadata
    assert metadata[ApprovalMetadataKeys.REMEMBER_ALLOWED] is True
    assert metadata[ApprovalMetadataKeys.REMEMBER_THREAD_ID] == "thread-a"
    assert metadata[ApprovalMetadataKeys.REMEMBER_REVISION] == 0
    assert metadata[ApprovalMetadataKeys.REMEMBER_RULE] == {
        "expression": "schedule",
        "displayText": "记住工具 schedule 的选择",
        "scopeCwd": None,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_schedule_remember_allow_is_scoped_to_current_thread(tmp_path: Path) -> None:
    """真实 Manager 写回后仅当前 thread 静默命中 schedule。"""
    permissions = PermissionsManager(tmp_path)
    sink = _ApprovalSink()
    manager = ApprovalManager(
        permissions_manager=permissions,
        event_sinks=[sink],
        default_timeout_ms=10_000,
    )
    tasks: list[asyncio.Task[ApprovalDecision]] = []

    try:
        prompt_a = make_manager_prompt_fn(
            manager,
            "thread-a",
            default_cwd=tmp_path.resolve().as_posix(),
        )
        chain_a = _build_chain(
            kongming_home=tmp_path,
            permissions=permissions,
            interactive_approval=InteractiveApproval(prompt_a),
        )

        sink.ready.clear()
        first_task = asyncio.create_task(
            chain_a.decide(_request(thread_id="thread-a", call_id="call-schedule-1", cwd=tmp_path))
        )
        tasks.append(first_task)
        await asyncio.wait_for(sink.ready.wait(), timeout=1)
        first_pending = sink.pending[-1]

        assert first_pending.thread_id == "thread-a"
        assert first_pending.tool_name == "schedule"
        assert first_pending.matched_rule == "default:ask"
        assert first_pending.remember_allowed is True
        assert first_pending.remember_rule is not None
        assert first_pending.remember_rule.expression == "schedule"
        assert await manager.resolve(
            "thread-a",
            first_pending.request_id,
            _remember_decision(first_pending),
        )
        first_decision = await first_task

        assert first_decision.outcome == "approved"
        assert (await permissions.snapshot("thread-a")).allow == (
            PermissionRuleRecord(expression="schedule", scope_cwd=None),
        )
        assert (await permissions.snapshot("thread-b")).revision == 0

        second_decision = await chain_a.decide(
            _request(thread_id="thread-a", call_id="call-schedule-2", cwd=tmp_path)
        )

        assert second_decision.outcome == "approved"
        assert second_decision.metadata[ApprovalMetadataKeys.DECISION_SOURCE] == "permissions"
        assert second_decision.metadata[ApprovalMetadataKeys.MATCHED_RULE] == "schedule"
        assert len(sink.pending) == 1

        prompt_b = make_manager_prompt_fn(
            manager,
            "thread-b",
            default_cwd=tmp_path.resolve().as_posix(),
        )
        chain_b = _build_chain(
            kongming_home=tmp_path,
            permissions=permissions,
            interactive_approval=InteractiveApproval(prompt_b),
        )
        sink.ready.clear()
        other_task = asyncio.create_task(
            chain_b.decide(_request(thread_id="thread-b", call_id="call-schedule-b", cwd=tmp_path))
        )
        tasks.append(other_task)
        await asyncio.wait_for(sink.ready.wait(), timeout=1)
        other_pending = sink.pending[-1]

        assert other_pending.thread_id == "thread-b"
        assert other_pending.remember_rule is not None
        assert other_pending.remember_rule.expression == "schedule"
        assert await manager.resolve(
            "thread-b",
            other_pending.request_id,
            {"allow": False, "remember": False},
        )
        assert (await other_task).outcome == "rejected"
        assert (await permissions.snapshot("thread-b")).revision == 0
    finally:
        await manager.aclose()
        await asyncio.gather(*tasks, return_exceptions=True)
