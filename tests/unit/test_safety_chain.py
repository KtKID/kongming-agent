"""验证 v0.6 三模式审批链的 12 格决策矩阵。

本文件覆盖 DangerGuard、全局 approval mode、thread permissions 和人工审批终点
的固定优先级，并校验 auto 回落事件、root thread_id 归属及 remember 冻结上下文。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from core.contracts import ApprovalDecision, ApprovalRequest, Event, ToolExecutionScope
from infrastructure.config.models import Config, ModelSelectionConfig
from runtime_assembly.session_engine import SessionEngine
from safety.approval.chain import build_safety_chain
from safety.approval.permissions_manager import PermissionsManager
from safety.approval.rule_models import PermissionRuleRecord
from safety.approval.types import ApprovalMetadataKeys
from safety.auto_approval.disposition import ApprovalDispositionMode


@dataclass
class _RecordingApproval:
    """记录进入人工审批终点的请求，并返回可配置决定。"""

    outcome: str = "approved"
    requests: list[ApprovalRequest] = field(default_factory=list)

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """保存审批请求并返回固定用户决定。"""
        self.requests.append(request)
        if self.outcome == "approved":
            return ApprovalDecision(outcome="approved", metadata={"source": "user"})
        return ApprovalDecision(outcome="rejected", metadata={"source": "user"})


@dataclass
class _RecordingEventSink:
    """记录安全链发出的结构化审计事件。"""

    events: list[Event] = field(default_factory=list)

    async def emit(self, event: Event) -> None:
        """保存单条事件。"""
        self.events.append(event)


@dataclass(frozen=True)
class _ModeResolver:
    """测试用 cwd 处置模式门户。"""

    mode: ApprovalDispositionMode

    def mode_for(self, cwd: str) -> ApprovalDispositionMode:
        """为任意 cwd 返回固定模式。"""
        return self.mode


def _config() -> Config:
    """构造使用本地模型地址的最小配置。"""
    return Config(
        model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"),
    )


def _request(kind: str, *, cwd: Path) -> ApprovalRequest:
    """按矩阵类别构造 danger、deny、allow 或未命中请求。"""
    canonical_cwd = cwd.resolve().as_posix()
    if kind == "hard_block":
        tool_name = "run_shell"
        arguments: dict[str, object] = {
            "command": "rm -rf /",
            "cwd": canonical_cwd,
        }
    elif kind in {"deny", "allow"}:
        tool_name = "read_file"
        arguments = {"path": str(cwd / "notes.md")}
    else:
        tool_name = "list_dir"
        arguments = {"path": str(cwd / "unmatched")}
    return ApprovalRequest(
        run_id="run-matrix",
        session_id="child-session",
        turn=1,
        call_id=f"call-{kind}",
        tool_name=tool_name,
        arguments=arguments,
        execution_scope=ToolExecutionScope(cwd=canonical_cwd if tool_name == "run_shell" else None),
        metadata={"cwd": canonical_cwd, "thread_id": "root-thread"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(ApprovalDispositionMode))
@pytest.mark.parametrize("kind", ["hard_block", "deny", "allow", "unmatched"])
async def test_three_modes_decision_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: ApprovalDispositionMode,
    kind: str,
) -> None:
    """三种模式乘四类请求均遵循 Danger → mode → permissions 顺序。"""
    monkeypatch.setenv("KONGMING_HOME", str(tmp_path / ".kongming"))
    permissions = PermissionsManager(tmp_path / ".kongming")
    if kind == "deny":
        await permissions.replace(
            "root-thread",
            allow=[],
            deny=[PermissionRuleRecord(expression="read_file", scope_cwd=None)],
            expected_revision=0,
        )
    elif kind == "allow":
        await permissions.replace(
            "root-thread",
            allow=[PermissionRuleRecord(expression="read_file", scope_cwd=None)],
            deny=[],
            expected_revision=0,
        )

    interactive = _RecordingApproval()
    events: list[tuple[str, ApprovalDecision]] = []

    def _trace(
        event_kind: str,
        decision: ApprovalDecision,
        request: ApprovalRequest,
    ) -> None:
        """收集决策事件并验证事件仍指向原请求。"""
        assert request.call_id == f"call-{kind}"
        events.append((event_kind, decision))

    chain = build_safety_chain(
        _config(),
        interactive_approval=interactive,
        permissions_manager=permissions,
        trace_emitter=_trace,
        disposition_resolver=_ModeResolver(mode),
    )
    decision = await chain.decide(_request(kind, cwd=tmp_path))

    source = decision.metadata[ApprovalMetadataKeys.DECISION_SOURCE]
    if kind == "hard_block":
        assert decision.outcome == "rejected"
        assert source == "intrinsic"
        assert interactive.requests == []
    elif mode is ApprovalDispositionMode.FULL_TRUST:
        assert decision.outcome == "approved"
        assert source == "full_trust"
        assert interactive.requests == []
    elif kind == "deny":
        assert decision.outcome == "rejected"
        assert source == "permissions"
        assert interactive.requests == []
    elif kind == "allow":
        assert decision.outcome == "approved"
        assert source == "permissions"
        assert interactive.requests == []
    else:
        assert decision.outcome == "approved"
        assert source == "user_approval"
        assert len(interactive.requests) == 1
        metadata = interactive.requests[0].metadata
        assert metadata[ApprovalMetadataKeys.REMEMBER_ALLOWED] is True
        assert metadata[ApprovalMetadataKeys.REMEMBER_THREAD_ID] == "root-thread"
        assert metadata[ApprovalMetadataKeys.REMEMBER_REVISION] == 0


@pytest.mark.asyncio
async def test_deny_wins_allow_in_user_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同表达式位于两边时 deny 保持最高 permissions 优先级。"""
    monkeypatch.setenv("KONGMING_HOME", str(tmp_path / ".kongming"))
    permissions = PermissionsManager(tmp_path / ".kongming")
    await permissions.replace(
        "root-thread",
        allow=[PermissionRuleRecord(expression="read_file", scope_cwd=None)],
        deny=[PermissionRuleRecord(expression="read_file", scope_cwd=None)],
        expected_revision=0,
    )
    interactive = _RecordingApproval()
    chain = build_safety_chain(
        _config(),
        interactive_approval=interactive,
        permissions_manager=permissions,
        disposition_resolver=_ModeResolver(ApprovalDispositionMode.USER),
    )

    decision = await chain.decide(_request("allow", cwd=tmp_path))

    assert decision.outcome == "rejected"
    assert decision.metadata[ApprovalMetadataKeys.DECISION_SOURCE] == "permissions"
    assert interactive.requests == []


@pytest.mark.asyncio
async def test_hard_block_precedes_scoped_shell_allow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exact cwd Shell allow 仍无法绕过 DangerGuard hard block。"""
    monkeypatch.setenv("KONGMING_HOME", str(tmp_path / ".kongming"))
    canonical_cwd = tmp_path.resolve().as_posix()
    permissions = PermissionsManager(tmp_path / ".kongming")
    await permissions.replace(
        "root-thread",
        allow=[
            PermissionRuleRecord(
                expression="run_shell(rm:*)",
                scope_cwd=canonical_cwd,
            )
        ],
        deny=[],
        expected_revision=0,
    )
    interactive = _RecordingApproval()
    chain = build_safety_chain(
        _config(),
        interactive_approval=interactive,
        permissions_manager=permissions,
        disposition_resolver=_ModeResolver(ApprovalDispositionMode.USER),
    )
    request = ApprovalRequest(
        run_id="run-danger-scope",
        session_id="child-session",
        turn=1,
        call_id="call-danger-scope",
        tool_name="run_shell",
        arguments={"command": "rm -rf /"},
        execution_scope=ToolExecutionScope(cwd=canonical_cwd),
        metadata={"cwd": canonical_cwd, "thread_id": "root-thread"},
    )

    decision = await chain.decide(request)

    assert decision.outcome == "rejected"
    assert decision.metadata[ApprovalMetadataKeys.DECISION_SOURCE] == "intrinsic"
    assert interactive.requests == []


@pytest.mark.asyncio
async def test_scoped_shell_allow_audit_contains_execution_and_rule_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """permissions 命中事件同时记录 prepared cwd 与规则 cwd。"""
    monkeypatch.setenv("KONGMING_HOME", str(tmp_path / ".kongming"))
    canonical_cwd = tmp_path.resolve().as_posix()
    permissions = PermissionsManager(tmp_path / ".kongming")
    await permissions.replace(
        "root-thread",
        allow=[
            PermissionRuleRecord(
                expression="run_shell(git status:*)",
                scope_cwd=canonical_cwd,
            )
        ],
        deny=[],
        expected_revision=0,
    )
    sink = _RecordingEventSink()
    chain = build_safety_chain(
        _config(),
        interactive_approval=_RecordingApproval(),
        permissions_manager=permissions,
        event_sinks=[sink],
        disposition_resolver=_ModeResolver(ApprovalDispositionMode.USER),
    )
    request = ApprovalRequest(
        run_id="run-audit-scope",
        session_id="child-session",
        turn=1,
        call_id="call-audit-scope",
        tool_name="run_shell",
        arguments={"command": "git status --short"},
        execution_scope=ToolExecutionScope(cwd=canonical_cwd),
        metadata={"cwd": canonical_cwd, "thread_id": "root-thread"},
    )

    decision = await chain.decide(request)
    await asyncio.sleep(0)

    assert decision.outcome == "approved"
    event = next(item for item in sink.events if item.kind == "tool.silently_allowed")
    assert event.payload["execution_scope_cwd"] == canonical_cwd
    assert event.payload["matched_rule_scope_cwd"] == canonical_cwd


@pytest.mark.asyncio
async def test_cli_falls_back_to_stable_session_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺少 Web thread_id 时以稳定 session id 冻结 remember 归属。"""
    monkeypatch.setenv("KONGMING_HOME", str(tmp_path / ".kongming"))
    interactive = _RecordingApproval(outcome="rejected")
    chain = build_safety_chain(
        _config(),
        interactive_approval=interactive,
        permissions_manager=PermissionsManager(tmp_path / ".kongming"),
        disposition_resolver=_ModeResolver(ApprovalDispositionMode.USER),
    )
    request = _request("unmatched", cwd=tmp_path)
    request = ApprovalRequest(
        run_id=request.run_id,
        session_id="stable-cli-session",
        turn=request.turn,
        call_id=request.call_id,
        tool_name=request.tool_name,
        arguments=request.arguments,
        metadata={"cwd": str(tmp_path)},
    )

    decision = await chain.decide(request)

    assert decision.outcome == "rejected"
    assert (
        interactive.requests[0].metadata[ApprovalMetadataKeys.REMEMBER_THREAD_ID]
        == "stable-cli-session"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_rule"),
    [
        ("hard_block", "host-root-delete"),
        ("unmatched", "default:ask"),
    ],
)
async def test_session_engine_without_interactive_host_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    expected_rule: str,
) -> None:
    """缺少人工审批宿主时，danger 与普通未命中都不能被占位 Provider 放行。"""
    monkeypatch.setenv("KONGMING_HOME", str(tmp_path / ".kongming"))
    runtime = SessionEngine.build(
        _config(),
        permissions_manager=PermissionsManager(tmp_path / ".kongming"),
        disposition_resolver=_ModeResolver(ApprovalDispositionMode.USER),
    )
    try:
        decision = await runtime.approval.decide(_request(kind, cwd=tmp_path))
    finally:
        await runtime.aclose()

    assert decision.outcome == "rejected"
    assert decision.metadata[ApprovalMetadataKeys.MATCHED_RULE] == expected_rule
