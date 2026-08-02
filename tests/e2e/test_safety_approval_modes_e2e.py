"""full_trust 普通放行与 danger 强制人工审批端到端测试。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.contracts import ApprovalDecision, ApprovalRequest, ToolExecutionScope
from infrastructure.config.models import Config, ModelSelectionConfig
from safety.approval.chain import build_safety_chain
from safety.approval.permissions_manager import PermissionsManager
from safety.approval.rule_models import PermissionRuleRecord
from safety.auto_approval.disposition import ApprovalDispositionMode


@dataclass(frozen=True)
class _FullTrustResolver:
    """端到端测试使用的完全信任模式门户。"""

    def mode_for(self, cwd: str) -> ApprovalDispositionMode:
        """为测试 cwd 返回完全信任。"""
        return ApprovalDispositionMode.FULL_TRUST


@dataclass
class _RecordingApproval:
    """记录所有真正进入人工审批终点的请求。"""

    requests: list[ApprovalRequest] = field(default_factory=list)

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """模拟用户显式点击一次允许。"""
        self.requests.append(request)
        return ApprovalDecision(outcome="approved", metadata={"source": "user"})


def _config() -> Config:
    """构造最小配置；处置模式由 cwd 门户提供。"""
    return Config(
        model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"),
    )


def _request(*, danger: bool, cwd: Path) -> ApprovalRequest:
    """构造普通 list_dir 或可审计的递归删除请求。"""
    canonical_cwd = cwd.resolve().as_posix()
    return ApprovalRequest(
        run_id="run-full-trust",
        session_id="child-session",
        turn=1,
        call_id="call-danger" if danger else "call-normal",
        tool_name="run_shell" if danger else "list_dir",
        arguments=(
            {"command": "rm -rf generated", "cwd": canonical_cwd}
            if danger
            else {"path": canonical_cwd}
        ),
        execution_scope=ToolExecutionScope(cwd=canonical_cwd if danger else None),
        metadata={"thread_id": "thread-root", "cwd": canonical_cwd},
    )


async def test_full_trust_skips_permissions_but_force_ask_reaches_consent(
    tmp_path: Path,
) -> None:
    """完全信任放行普通请求，破坏性命令仍进入人工审批。"""
    interactive = _RecordingApproval()
    permissions = PermissionsManager(tmp_path / ".kongming")
    await permissions.replace(
        "thread-root",
        allow=[],
        deny=[PermissionRuleRecord(expression="list_dir", scope_cwd=None)],
        expected_revision=0,
    )
    chain = build_safety_chain(
        _config(),
        interactive_approval=interactive,
        permissions_manager=permissions,
        disposition_resolver=_FullTrustResolver(),
    )

    normal = await chain.decide(_request(danger=False, cwd=tmp_path))
    danger = await chain.decide(_request(danger=True, cwd=tmp_path))

    assert normal.outcome == "approved"
    assert normal.metadata["decision_source"] == "full_trust"
    assert danger.outcome == "approved"
    assert danger.metadata["decision_source"] == "danger"
    assert danger.metadata["danger"] is True
    assert danger.metadata["remember_allowed"] is False
    assert len(interactive.requests) == 1
    assert interactive.requests[0].call_id == "call-danger"
