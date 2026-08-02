"""Safety 决策顺序与 effective cwd 单一真源红线测试。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from core.contracts import ApprovalDecision, ApprovalRequest, ToolExecutionScope
from infrastructure.config.models import Config, ModelSelectionConfig
from safety.approval.chain import build_safety_chain
from safety.approval.permissions_manager import PermissionsManager
from safety.approval.types import ApprovalMetadataKeys
from safety.auto_approval.disposition import ApprovalDispositionMode
from safety.guards.danger import DangerAction, DangerGuard


@dataclass
class _RecordingApproval:
    """记录进入人工审批的请求，并统一返回拒绝。"""

    requests: list[ApprovalRequest] = field(default_factory=list)

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """保存真实请求，以拒绝结果区分人工审批与静默放行。"""
        self.requests.append(request)
        return ApprovalDecision(outcome="rejected", metadata={"decision_source": "user"})


@dataclass
class _AllowApproval:
    """记录重绑后的人工请求并统一允许。"""

    requests: list[ApprovalRequest] = field(default_factory=list)

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(outcome="approved", metadata={"decision_source": "user"})


@dataclass
class _RecordingModeResolver:
    """按冲突哨兵 cwd 返回不同模式，并记录真实查询坐标。"""

    modes: dict[str, ApprovalDispositionMode]
    seen_cwds: list[str] = field(default_factory=list)

    def mode_for(self, cwd: str) -> ApprovalDispositionMode:
        """记录 mode 查询 cwd，并返回该 cwd 的明确配置。"""
        self.seen_cwds.append(cwd)
        return self.modes[cwd]


def _config() -> Config:
    """构造不依赖外部模型服务的最小配置。"""
    return Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"))


def _shell_request(
    *,
    command: str,
    metadata_cwd: Path,
    effective_cwd: Path,
) -> ApprovalRequest:
    """构造 metadata 与 execution scope 明确冲突的 Shell 审批请求。"""
    return ApprovalRequest(
        run_id="run-safety-redline",
        session_id="session-safety-redline",
        turn=1,
        call_id="call-safety-redline",
        tool_name="run_shell",
        arguments={"command": command, "cwd": effective_cwd.resolve().as_posix()},
        execution_scope=ToolExecutionScope(cwd=effective_cwd.resolve().as_posix()),
        metadata={
            "cwd": metadata_cwd.resolve().as_posix(),
            "thread_id": "thread-safety-redline",
        },
    )


@pytest.mark.unit
async def test_shell_mode_lookup_uses_prepared_effective_cwd(tmp_path: Path) -> None:
    """Shell mode 查询必须消费 execution_scope.cwd 的 B 哨兵。"""
    metadata_cwd = tmp_path / "runtime-a"
    effective_cwd = tmp_path / "shell-b"
    metadata_cwd.mkdir()
    effective_cwd.mkdir()
    metadata_value = metadata_cwd.resolve().as_posix()
    effective_value = effective_cwd.resolve().as_posix()
    resolver = _RecordingModeResolver(
        modes={
            metadata_value: ApprovalDispositionMode.FULL_TRUST,
            effective_value: ApprovalDispositionMode.USER,
        }
    )
    interactive = _RecordingApproval()
    home = tmp_path / ".kongming"
    chain = build_safety_chain(
        _config(),
        interactive_approval=interactive,
        permissions_manager=PermissionsManager(home),
        danger_guard=DangerGuard(kongming_home=home),
        disposition_resolver=resolver,
    )
    request = _shell_request(
        command="git status --short",
        metadata_cwd=metadata_cwd,
        effective_cwd=effective_cwd,
    )

    decision = await chain.decide(request)

    assert resolver.seen_cwds == [effective_value]
    assert decision.outcome == "rejected"
    assert len(interactive.requests) == 1
    assert interactive.requests[0].arguments == request.arguments
    assert interactive.requests[0].execution_scope == request.execution_scope


@pytest.mark.unit
async def test_rebinding_interactive_approval_preserves_runtime_hard_block(
    tmp_path: Path,
) -> None:
    """人工终点可按 thread 重绑，DangerGuard 仍由原 Safety 门户先决策。"""
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    cwd_value = cwd.resolve().as_posix()
    resolver = _RecordingModeResolver(modes={cwd_value: ApprovalDispositionMode.USER})
    original = _RecordingApproval()
    rebound = _AllowApproval()
    home = tmp_path / ".kongming"
    chain = build_safety_chain(
        _config(),
        interactive_approval=original,
        permissions_manager=PermissionsManager(home),
        danger_guard=DangerGuard(kongming_home=home),
        disposition_resolver=resolver,
    )
    rebound_chain = chain.with_interactive_approval(rebound)
    safe_request = _shell_request(
        command="git status --short",
        metadata_cwd=cwd,
        effective_cwd=cwd,
    )
    blocked_request = _shell_request(
        command="rm -rf /",
        metadata_cwd=cwd,
        effective_cwd=cwd,
    )

    safe_decision = await rebound_chain.decide(safe_request)
    blocked_decision = await rebound_chain.decide(blocked_request)

    assert safe_decision.outcome == "approved"
    assert len(rebound.requests) == 1
    assert original.requests == []
    assert blocked_decision.outcome == "rejected"
    assert blocked_decision.metadata[ApprovalMetadataKeys.DECISION_CLASS] == "hard_block"


@pytest.mark.unit
async def test_full_trust_cannot_auto_allow_force_ask(tmp_path: Path) -> None:
    """DangerAction.FORCE_ASK 必须在 full_trust 下进入人工审批。"""
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    cwd_value = cwd.resolve().as_posix()
    resolver = _RecordingModeResolver(modes={cwd_value: ApprovalDispositionMode.FULL_TRUST})
    interactive = _RecordingApproval()
    event_kinds: list[str] = []

    def _record_trace(
        event_kind: str,
        decision: ApprovalDecision,
        request: ApprovalRequest,
    ) -> None:
        """记录决策事件，并保持 callback 对真实参数可观测。"""
        assert decision.outcome in {"approved", "rejected", "pending"}
        assert request.call_id == "call-safety-redline"
        event_kinds.append(event_kind)

    home = tmp_path / ".kongming"
    chain = build_safety_chain(
        _config(),
        interactive_approval=interactive,
        permissions_manager=PermissionsManager(home),
        danger_guard=DangerGuard(kongming_home=home),
        trace_emitter=_record_trace,
        disposition_resolver=resolver,
    )
    request = _shell_request(
        command="rm -rf generated",
        metadata_cwd=cwd,
        effective_cwd=cwd,
    )
    matched_rule = DangerGuard(kongming_home=home).match(request)

    decision = await chain.decide(request)

    assert matched_rule is not None
    assert matched_rule.action is DangerAction.FORCE_ASK
    assert decision.outcome == "rejected"
    assert len(interactive.requests) == 1
    consent_request = interactive.requests[0]
    assert consent_request.arguments == request.arguments
    assert consent_request.execution_scope == request.execution_scope
    assert consent_request.metadata[ApprovalMetadataKeys.DANGER] is True
    assert consent_request.metadata[ApprovalMetadataKeys.REMEMBER_ALLOWED] is False
    assert "tool.approval_required" in event_kinds
    assert "approval.full_trust.auto_allow" not in event_kinds


@pytest.mark.unit
def test_danger_guard_resolves_relative_target_from_effective_cwd(tmp_path: Path) -> None:
    """DangerGuard 必须以 B 哨兵解析 Shell 中的相对写入目标。"""
    metadata_cwd = tmp_path / "runtime-a"
    effective_cwd = tmp_path / "shell-b"
    metadata_cwd.mkdir()
    effective_cwd.mkdir()
    git_dir = effective_cwd / ".git"
    git_dir.mkdir()
    (effective_cwd / "repo-control").symlink_to(git_dir, target_is_directory=True)
    home = tmp_path / ".kongming"
    request = _shell_request(
        command="python -c \"open('repo-control/config','w').write('x')\"",
        metadata_cwd=metadata_cwd,
        effective_cwd=effective_cwd,
    )

    rule = DangerGuard(kongming_home=home).match(request)

    assert rule is not None
    assert rule.name == "git-internal"
    assert rule.action is DangerAction.BLOCK


@pytest.mark.unit
@pytest.mark.parametrize("scope_cwd", [None, "packages/app"])
def test_shell_invalid_execution_scope_fails_closed(
    tmp_path: Path,
    scope_cwd: str | None,
) -> None:
    """缺失或相对 Shell scope 必须在 DangerGuard 入口硬关闭。"""
    request = ApprovalRequest(
        run_id="run-invalid-shell-scope",
        session_id="session-invalid-shell-scope",
        turn=1,
        call_id="call-invalid-shell-scope",
        tool_name="run_shell",
        arguments={"command": "git status --short"},
        execution_scope=ToolExecutionScope(cwd=scope_cwd),
        metadata={"cwd": tmp_path.resolve().as_posix()},
    )

    rule = DangerGuard(kongming_home=tmp_path / ".kongming").match(request)

    assert rule is not None
    assert rule.name == "shell-execution-scope-missing"
    assert rule.action is DangerAction.BLOCK


@pytest.mark.unit
async def test_non_shell_mode_lookup_keeps_metadata_cwd(tmp_path: Path) -> None:
    """非 Shell 请求继续使用 metadata cwd 查询处置模式。"""
    cwd = tmp_path.resolve().as_posix()
    resolver = _RecordingModeResolver(modes={cwd: ApprovalDispositionMode.FULL_TRUST})
    interactive = _RecordingApproval()
    home = tmp_path / ".kongming"
    chain = build_safety_chain(
        _config(),
        interactive_approval=interactive,
        permissions_manager=PermissionsManager(home),
        danger_guard=DangerGuard(kongming_home=home),
        disposition_resolver=resolver,
    )
    request = ApprovalRequest(
        run_id="run-non-shell-context",
        session_id="session-non-shell-context",
        turn=1,
        call_id="call-non-shell-context",
        tool_name="list_dir",
        arguments={"path": cwd},
        metadata={"cwd": cwd, "thread_id": "thread-non-shell-context"},
    )

    decision = await chain.decide(request)

    assert resolver.seen_cwds == [cwd]
    assert decision.outcome == "approved"
    assert decision.metadata[ApprovalMetadataKeys.DECISION_SOURCE] == "full_trust"
    assert interactive.requests == []
