"""``safety.approval_rules`` 的 CLI 通道覆盖测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from safety.approval_rules import ApprovalRules
from safety.auto_approval import AutoApprovalPolicy, ConfigStore, ProjectConfig
from safety.auto_approval.rules import load_default_rules


def _real_policy(tmp_path: Path, *, enabled: bool = True) -> AutoApprovalPolicy:
    store = ConfigStore(tmp_path / "auto_approval")
    store.set(ProjectConfig(cwd="/proj", enabled=enabled, timeout_ms=10_000))
    return AutoApprovalPolicy(load_default_rules(), store)


class _RaisingPolicy:
    def classify(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        cwd: str,
        is_elevated: bool,
    ) -> Any:
        raise RuntimeError("boom")

    def is_enabled_for(self, cwd: str) -> bool:
        return True


# 验证真实规则集下，CLI 安全命令未命中启用规则时直接返回批准。
def test_cli_safe_command_with_real_policy_returns_immediate_approved(tmp_path: Path) -> None:
    policy = _real_policy(tmp_path)
    rules = ApprovalRules(policy=policy)

    dec = rules.classify(
        channel="cli",
        thread_id="cli-session",
        cwd="/proj",
        tool_name="run_shell",
        tool_input={"command": "ls"},
    )

    assert dec.is_immediate is True
    assert dec.immediate_outcome == "approved"
    assert dec.auto_approve_at_ms is None
    assert dec.auto_reject_at_ms is None
    assert dec.timeout_ms == 10_000


# 验证真实规则集下，CLI 危险命令命中 ``bash_rm_any`` 后强制进入人工审批。
def test_cli_blocked_rule_with_real_policy_forces_human_approval(tmp_path: Path) -> None:
    rules = ApprovalRules(policy=_real_policy(tmp_path))

    dec = rules.classify(
        channel="cli",
        thread_id="cli-session",
        cwd="/proj",
        tool_name="run_shell",
        tool_input={"command": "rm -rf tmp"},
    )

    assert dec.is_immediate is False
    assert dec.matched_rule == "bash_rm_any"
    assert dec.auto_approve_at_ms is None
    assert dec.auto_reject_at_ms is None
    assert dec.timeout_ms == 10_000


# 验证 CLI 本次会话授权只放行普通命令，后续真实危险规则仍会拦住。
def test_cli_session_grant_still_checks_real_blocked_rule_guard(tmp_path: Path) -> None:
    rules = ApprovalRules(policy=_real_policy(tmp_path))
    rules.add_session_grant(
        channel="cli",
        thread_id="cli-session",
        cwd="/proj",
        tool_name="run_shell",
    )

    allowed = rules.classify(
        channel="cli",
        thread_id="cli-session",
        cwd="/proj",
        tool_name="run_shell",
        tool_input={"command": "ls"},
    )

    assert allowed.is_immediate is True
    assert allowed.immediate_outcome == "approved"

    blocked = rules.classify(
        channel="cli",
        thread_id="cli-session",
        cwd="/proj",
        tool_name="run_shell",
        tool_input={"command": "rm -rf tmp"},
    )

    assert blocked.is_immediate is False
    assert blocked.matched_rule == "bash_rm_any"
    assert blocked.timeout_ms == 10_000


# 验证缺少自动审批策略时，CLI 本次会话授权采用失败关闭并回到人工审批。
def test_cli_session_grant_fails_closed_when_policy_missing() -> None:
    rules = ApprovalRules(policy=None)
    rules.add_session_grant(
        channel="cli",
        thread_id="cli-session",
        cwd="/proj",
        tool_name="run_shell",
    )

    dec = rules.classify(
        channel="cli",
        thread_id="cli-session",
        cwd="/proj",
        tool_name="run_shell",
        tool_input={"command": "rm -rf tmp"},
    )

    assert dec.is_immediate is False
    assert dec.immediate_outcome is None
    assert dec.matched_rule is None
    assert dec.timeout_ms == 60_000


# 验证自动审批策略抛异常时，CLI 通道回退到人工审批默认路径。
def test_cli_policy_exception_falls_back_to_ask() -> None:
    rules = ApprovalRules(policy=_RaisingPolicy())

    dec = rules.classify(
        channel="cli",
        thread_id="cli-session",
        cwd="/proj",
        tool_name="Bash",
        tool_input={"command": "ls"},
    )

    assert dec.is_immediate is False
    assert dec.immediate_outcome is None
    assert dec.matched_rule is None
    assert dec.timeout_ms == 60_000
