"""Thread permissions 记住、隔离与进程重启恢复端到端测试。

关键流程：thread A 首次人工允许并记住，第二次由本子静默放行；thread B 的
同一请求仍进入 pending；重新构造 PermissionsManager 后 A 继续命中落盘规则。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from core.contracts import ApprovalDecision, ApprovalRequest
from infrastructure.config.models import Config, ModelSelectionConfig
from safety.approval.chain import SafetyGatedApproval, build_safety_chain
from safety.approval.events import PendingApprovalView
from safety.approval.manager import ApprovalManager, make_manager_prompt_fn
from safety.approval.permissions_manager import PermissionsManager
from safety.auto_approval.disposition import ApprovalDispositionMode
from tools.runtime.approval import InteractiveApproval


class _ResolvingSink:
    """按预设二态决定立即处理真实 ApprovalManager pending。"""

    def __init__(
        self,
        manager: ApprovalManager,
        decisions: list[Mapping[str, object]],
    ) -> None:
        self._manager = manager
        self._decisions = decisions
        self.pending: list[PendingApprovalView] = []

    async def emit_approval_required(self, *, pending: PendingApprovalView) -> None:
        """记录 pending 并通过真实 manager resolve 入口处理。"""
        self.pending.append(pending)
        decision = dict(self._decisions.pop(0))
        if decision.get("remember") is True and pending.remember_rule is not None:
            decision["rememberRule"] = {
                "expression": pending.remember_rule.expression,
                "displayText": pending.remember_rule.display_text,
                "scopeCwd": pending.remember_rule.scope_cwd,
            }
        accepted = await self._manager.resolve(
            pending.thread_id,
            pending.request_id,
            decision,
        )
        assert accepted is True

    async def emit_approval_removed(self, *, request_id: str, reason: str) -> None:
        """本测试只需确认 pending 经过统一清理出口。"""
        assert request_id
        assert reason in {"user_decided", "cancelled", "timeout"}


class _FailIfPromptedApproval:
    """重启恢复路径若再次进入人工审批则立即失败。"""

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """把意外询问转成清晰断言，避免等待 pending 超时。"""
        raise AssertionError(f"unexpected approval prompt: {request.call_id}")


class _UserModeResolver:
    """线程权限测试使用固定用户审批模式。"""

    def mode_for(self, cwd: str) -> ApprovalDispositionMode:
        """返回 user，保持 permissions 优先级可观测。"""
        return ApprovalDispositionMode.USER


def _config() -> Config:
    """构造最小本地配置；处置模式由 cwd 门户提供。"""
    return Config(
        model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"),
    )


def _request(thread_id: str, *, cwd: Path, call_id: str) -> ApprovalRequest:
    """构造两个顶层 thread 共用的同形 read_file 请求。"""
    return ApprovalRequest(
        run_id=f"run-{thread_id}",
        session_id=f"child-{thread_id}",
        turn=1,
        call_id=call_id,
        tool_name="read_file",
        arguments={"path": str(cwd / "notes.md")},
        metadata={"thread_id": thread_id, "cwd": str(cwd)},
    )


def _chain_with_manager(
    *,
    home: Path,
    thread_id: str,
    decisions: list[Mapping[str, object]],
) -> tuple[SafetyGatedApproval, _ResolvingSink, PermissionsManager]:
    """装配真实 permissions、pending manager 与人工交互终点。"""
    permissions = PermissionsManager(home)
    manager = ApprovalManager(permissions_manager=permissions)
    sink = _ResolvingSink(manager, decisions)
    manager.register_event_sink(sink)
    prompt = make_manager_prompt_fn(manager, thread_id)
    chain = build_safety_chain(
        _config(),
        interactive_approval=InteractiveApproval(prompt),
        permissions_manager=permissions,
        disposition_resolver=_UserModeResolver(),
    )
    return chain, sink, permissions


async def test_remember_is_thread_scoped_and_survives_restart(tmp_path: Path) -> None:
    """A 记住后静默放行，B 仍 pending，重建门户后 A 继续命中。"""
    home = tmp_path / ".kongming"
    thread_a = "thread-aaaaaaaaaaaa"
    thread_b = "thread-bbbbbbbbbbbb"
    chain_a, sink_a, permissions_a = _chain_with_manager(
        home=home,
        thread_id=thread_a,
        decisions=[{"allow": True, "remember": True}],
    )

    first = await chain_a.decide(_request(thread_a, cwd=tmp_path, call_id="call-a-1"))
    second = await chain_a.decide(_request(thread_a, cwd=tmp_path, call_id="call-a-2"))

    assert first.outcome == "approved"
    assert second.outcome == "approved"
    assert second.metadata["decision_source"] == "permissions"
    assert len(sink_a.pending) == 1
    snapshot_a = await permissions_a.snapshot(thread_a)
    assert snapshot_a.revision == 1
    assert len(snapshot_a.allow) == 1
    assert snapshot_a.allow[0].expression.startswith("read_file(")
    assert snapshot_a.allow[0].expression.endswith("notes.md/**)")
    assert snapshot_a.allow[0].scope_cwd is None

    chain_b, sink_b, permissions_b = _chain_with_manager(
        home=home,
        thread_id=thread_b,
        decisions=[{"allow": False, "remember": False}],
    )
    decision_b = await chain_b.decide(_request(thread_b, cwd=tmp_path, call_id="call-b-1"))
    assert decision_b.outcome == "rejected"
    assert len(sink_b.pending) == 1
    assert sink_b.pending[0].thread_id == thread_b
    assert (await permissions_b.snapshot(thread_b)).revision == 0

    restarted_permissions = PermissionsManager(home)
    restarted_chain = build_safety_chain(
        _config(),
        interactive_approval=_FailIfPromptedApproval(),
        permissions_manager=restarted_permissions,
        disposition_resolver=_UserModeResolver(),
    )
    after_restart = await restarted_chain.decide(
        _request(thread_a, cwd=tmp_path, call_id="call-a-3")
    )
    assert after_restart.outcome == "approved"
    assert after_restart.metadata["decision_source"] == "permissions"
