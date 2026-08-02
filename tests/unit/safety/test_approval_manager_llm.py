"""ApprovalManager 的 LLM 复核与用户中断窗口合同。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from core.contracts import ApprovalDecision
from safety.approval.events import PendingApprovalView
from safety.approval.llm_reviewer import LlmReviewDecision, LlmReviewResult
from safety.approval.manager import ApprovalManager
from safety.approval.permissions_manager import PermissionsManager
from safety.auto_approval.disposition import ApprovalDispositionMode
from safety.auto_approval.manager import AutoApprovalManager


class _Reviewer:
    """返回固定复核结论的异步替身。"""

    def __init__(self, decision: LlmReviewDecision = LlmReviewDecision.ALLOW) -> None:
        self._decision = decision
        self.calls: list[dict[str, object]] = []
        self.closed = False

    async def review(self, **kwargs: object) -> LlmReviewResult:
        """记录调用参数并返回预设结论。"""
        self.calls.append(dict(kwargs))
        return LlmReviewResult(
            decision=self._decision,
            reason="reviewed",
            model="reviewer-small",
        )

    async def aclose(self) -> None:
        """记录 Manager 是否释放 reviewer。"""
        self.closed = True


@dataclass
class _Sink:
    """收集 pending 更新并通过事件暴露倒计时状态。"""

    views: list[PendingApprovalView] = field(default_factory=list)
    countdown_started: asyncio.Event = field(default_factory=asyncio.Event)
    removed: list[str] = field(default_factory=list)

    async def emit_approval_required(self, *, pending: PendingApprovalView) -> None:
        """记录 pending；LLM allow 更新包含倒计时截止时间。"""
        self.views.append(pending)
        if pending.auto_approve_at_ms is not None:
            self.countdown_started.set()

    async def emit_approval_removed(self, *, request_id: str, reason: str) -> None:
        """记录移除原因。"""
        del request_id
        self.removed.append(reason)


@dataclass
class _Audit:
    """记录 LLM 自动放行审计。"""

    records: list[dict[str, object]] = field(default_factory=list)

    def log_llm_auto_allow(self, **kwargs: object) -> None:
        """保存审计结构。"""
        self.records.append(dict(kwargs))


async def _request(
    manager: ApprovalManager, *, matched_rule: str, danger: bool = False
) -> ApprovalDecision:
    """提交固定 thread 的审批请求。"""
    return await manager.request(
        channel="generic_chat",
        thread_id="thread-approval",
        cwd="/workspace",
        tool_name="write_file",
        tool_input={"path": "README.md", "content": "ok"},
        metadata={"matched_rule": matched_rule, "danger": danger},
    )


@pytest.mark.unit
async def test_llm_allow_updates_same_pending_then_user_can_interrupt(tmp_path: Path) -> None:
    """LLM allow 进入同 request 的倒计时，用户拒绝能取消自动放行。"""
    auto = AutoApprovalManager.build(tmp_path)
    auto.set_mode("/workspace", ApprovalDispositionMode.LLM)
    reviewer = _Reviewer()
    sink = _Sink()
    audit = _Audit()
    manager = ApprovalManager(
        permissions_manager=PermissionsManager(tmp_path),
        auto_approval_policy=auto.policy,
        llm_reviewer=reviewer,  # type: ignore[arg-type]
        audit_sink=audit,
    )
    manager.register_event_sink(sink)

    request_task = asyncio.create_task(_request(manager, matched_rule="default:ask"))
    await asyncio.wait_for(sink.countdown_started.wait(), timeout=1)
    pending = sink.views[-1]
    assert pending.auto_approve_at_ms is not None
    assert len(reviewer.calls) == 1
    assert audit.records[0]["model"] == "reviewer-small"

    accepted = await manager.resolve(
        "thread-approval",
        pending.request_id,
        {"allow": False, "remember": False},
    )
    decision = await request_task

    assert accepted is True
    assert decision.outcome == "rejected"
    assert sink.removed == ["user_decided"]
    assert manager.auto_approve_task_count == 0
    await manager.aclose()
    assert reviewer.closed is True


@pytest.mark.unit
async def test_llm_only_reviews_default_ask_and_never_rejects_directly(tmp_path: Path) -> None:
    """danger 与非 default:ask 保留人审；模型 review_required 也保留人审。"""
    auto = AutoApprovalManager.build(tmp_path)
    auto.set_mode("/workspace", ApprovalDispositionMode.LLM)
    reviewer = _Reviewer(LlmReviewDecision.REVIEW_REQUIRED)
    sink = _Sink()
    manager = ApprovalManager(
        permissions_manager=PermissionsManager(tmp_path),
        auto_approval_policy=auto.policy,
        llm_reviewer=reviewer,  # type: ignore[arg-type]
    )
    manager.register_event_sink(sink)

    request_task = asyncio.create_task(
        _request(manager, matched_rule="danger:rm-recursive", danger=True)
    )
    while not sink.views:
        await asyncio.sleep(0)
    accepted = await manager.resolve(
        "thread-approval",
        sink.views[-1].request_id,
        {"allow": True, "remember": False},
    )
    decision = await request_task

    assert accepted is True
    assert decision.outcome == "approved"
    assert reviewer.calls == []
    await manager.aclose()
