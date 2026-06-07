"""``CLIApprovalEventSink`` 的单元测试。"""

from __future__ import annotations

import asyncio

from cli.approval_manager_sink import CLIApprovalEventSink
from core.contracts import ApprovalAction, ApprovalRequest
from safety.approval_manager import ApprovalManager, _PendingApproval
from safety.approval_rules import ApprovalRules


def _pending(
    loop: asyncio.AbstractEventLoop,
    *,
    severity: str = "standard",
    matched_rule: str | None = None,
    auto_approve_at_ms: int | None = None,
    auto_reject_at_ms: int | None = None,
    timeout_ms: int | None = 60_000,
) -> _PendingApproval:
    future = loop.create_future()
    return _PendingApproval(
        request_id="req-1",
        channel="cli",
        thread_id="cli-session",
        cwd="/proj",
        tool_name="run_shell",
        tool_input={"command": "ls"},
        metadata={
            "run_id": "run-1",
            "session_id": "cli-session",
            "turn": 1,
            "call_id": "call-1",
            "reason": "needs approval",
        },
        severity=severity,
        matched_rule=matched_rule,
        auto_approve_at_ms=auto_approve_at_ms,
        auto_reject_at_ms=auto_reject_at_ms,
        future=future,
        timeout_ms=timeout_ms,
    )


# 验证终端返回本次会话同意时，CLI 接收器只按单次允许回写审批管理器。
async def test_cli_sink_accept_for_session_resolves_as_once_payload() -> None:
    manager = ApprovalManager(rules=ApprovalRules())
    captured: list[ApprovalRequest] = []

    async def prompt(request: ApprovalRequest) -> ApprovalAction:
        captured.append(request)
        return ApprovalAction.ACCEPT_FOR_SESSION

    pending = _pending(asyncio.get_running_loop())
    manager._pending[pending.request_id] = pending
    sink = CLIApprovalEventSink(manager, prompt)

    await sink.emit_approval_required(pending=pending)

    assert pending.future.done()
    decision = pending.future.result()
    assert decision.outcome == "approved"
    assert "remember_for_session" not in decision.metadata
    assert "remember_persistent" not in decision.metadata
    assert captured[0].run_id == "run-1"
    assert captured[0].session_id == "cli-session"
    assert captured[0].call_id == "call-1"
    assert captured[0].metadata["cwd"] == "/proj"
    assert captured[0].metadata["approval_channel"] == "cli"
    assert captured[0].metadata["timeout_ms"] == 60_000


# 验证 CLI 接收器会把安全路径自动同意 deadline 透传给终端 prompt。
async def test_cli_sink_projects_auto_approve_metadata() -> None:
    manager = ApprovalManager(rules=ApprovalRules())
    captured: list[ApprovalRequest] = []

    async def prompt(request: ApprovalRequest) -> ApprovalAction:
        captured.append(request)
        return ApprovalAction.ACCEPT_ONCE

    pending = _pending(
        asyncio.get_running_loop(),
        severity="standard",
        auto_approve_at_ms=54_321,
        timeout_ms=10_000,
    )
    manager._pending[pending.request_id] = pending
    sink = CLIApprovalEventSink(manager, prompt)

    await sink.emit_approval_required(pending=pending)

    assert pending.future.done()
    assert pending.future.result().outcome == "approved"
    assert captured[0].metadata["severity"] == "standard"
    assert captured[0].metadata["auto_approve_at_ms"] == 54_321
    assert captured[0].metadata["auto_reject_at_ms"] is None
    assert captured[0].metadata["timeout_ms"] == 10_000


# 验证 CLI 接收器会把危险规则、自动拒绝 deadline 和超时配置透传给终端 prompt。
async def test_cli_sink_projects_auto_reject_metadata() -> None:
    manager = ApprovalManager(rules=ApprovalRules())
    captured: list[ApprovalRequest] = []

    async def prompt(request: ApprovalRequest) -> ApprovalAction:
        captured.append(request)
        return ApprovalAction.REJECT

    pending = _pending(
        asyncio.get_running_loop(),
        severity="elevated",
        matched_rule="bash_rm_any",
        auto_reject_at_ms=12_345,
        timeout_ms=10_000,
    )
    manager._pending[pending.request_id] = pending
    sink = CLIApprovalEventSink(manager, prompt)

    await sink.emit_approval_required(pending=pending)

    assert pending.future.done()
    assert pending.future.result().outcome == "rejected"
    assert captured[0].metadata["severity"] == "elevated"
    assert captured[0].metadata["matched_rule"] == "bash_rm_any"
    assert captured[0].metadata["blocked_by_rule"] == "bash_rm_any"
    assert captured[0].metadata["auto_reject_at_ms"] == 12_345
    assert captured[0].metadata["timeout_ms"] == 10_000


# 验证终端审批提示抛异常时，接收器会按失败关闭自动拒绝。
async def test_cli_sink_prompt_exception_rejects() -> None:
    manager = ApprovalManager(rules=ApprovalRules())

    async def prompt(_request: ApprovalRequest) -> ApprovalAction:
        raise RuntimeError("标准输入失败")

    pending = _pending(asyncio.get_running_loop())
    manager._pending[pending.request_id] = pending
    sink = CLIApprovalEventSink(manager, prompt)

    await sink.emit_approval_required(pending=pending)

    assert pending.future.done()
    assert pending.future.result().outcome == "rejected"


# 验证待处理请求无法投影成 ApprovalRequest 时，接收器会自动拒绝。
async def test_cli_sink_projection_exception_rejects() -> None:
    manager = ApprovalManager(rules=ApprovalRules())
    called = False

    async def prompt(_request: ApprovalRequest) -> ApprovalAction:
        nonlocal called
        called = True
        return ApprovalAction.ACCEPT_ONCE

    pending = _pending(asyncio.get_running_loop())
    pending.tool_input = None  # type: ignore[assignment]
    manager._pending[pending.request_id] = pending
    sink = CLIApprovalEventSink(manager, prompt)

    await sink.emit_approval_required(pending=pending)

    assert called is False
    assert pending.future.done()
    assert pending.future.result().outcome == "rejected"


# 验证 CLI 接收器只处理 cli 通道，其他通道保持原待处理状态。
async def test_cli_sink_ignores_other_channels() -> None:
    manager = ApprovalManager(rules=ApprovalRules())
    called = False

    async def prompt(_request: ApprovalRequest) -> ApprovalAction:
        nonlocal called
        called = True
        return ApprovalAction.ACCEPT_ONCE

    pending = _pending(asyncio.get_running_loop())
    pending.channel = "generic_chat"
    manager._pending[pending.request_id] = pending
    sink = CLIApprovalEventSink(manager, prompt)

    await sink.emit_approval_required(pending=pending)

    assert called is False
    assert pending.future.done() is False
