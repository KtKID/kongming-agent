"""CLI 审批管理器链路的端到端测试。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from cli.approval_manager_sink import CLIApprovalEventSink
from config_loader.models import (
    ApprovalConfig,
    Config,
    ModelConfig,
    RunnerConfig,
    SessionConfig,
    TraceConfig,
)
from core.contracts import ApprovalAction, ApprovalRequest
from safety import SafetyGatedApproval, build_safety_chain
from safety.approval_manager import get_approval_manager, make_manager_prompt_fn, reset_for_testing
from safety.approval_rules import ApprovalRules
from safety.auto_approval import AutoApprovalPolicy, ConfigStore, ProjectConfig
from safety.auto_approval.rules import RuleSet, load_default_rules
from tools.approval import build_default_approval


def _config(tmp_path: Path) -> Config:
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="stub-model",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        runner=RunnerConfig(max_turns=3),
        session=SessionConfig(backend="memory", store_path=str(tmp_path / "sessions.db")),
        trace=TraceConfig(output_path=str(tmp_path / "trace.jsonl")),
        approval=ApprovalConfig(mode="interactive"),
    )


def _policy(
    tmp_path: Path,
    *,
    enabled: bool,
    rule_set: RuleSet | None = None,
) -> AutoApprovalPolicy:
    store = ConfigStore(tmp_path / "auto_approval")
    store.set(ProjectConfig(cwd="/proj", enabled=enabled, timeout_ms=10))
    return AutoApprovalPolicy(
        rule_set or RuleSet(version=1, default_timeout_ms=10, rules=()),
        store,
    )


def _approval_provider(
    cfg: Config,
    *,
    policy: AutoApprovalPolicy | None,
    actions: Sequence[ApprovalAction],
) -> tuple[SafetyGatedApproval, list[ApprovalRequest]]:
    reset_for_testing()
    captured: list[ApprovalRequest] = []
    remaining = list(actions)

    async def prompt(request: ApprovalRequest) -> ApprovalAction:
        captured.append(request)
        if not remaining:
            raise AssertionError("终端审批提示被意外触发")
        return remaining.pop(0)

    prompt.__action_aware__ = True  # type: ignore[attr-defined]
    manager = get_approval_manager(rules=ApprovalRules(policy=policy))
    manager.register_event_sink(CLIApprovalEventSink(manager, prompt))
    prompt_fn = make_manager_prompt_fn(
        manager,
        "cli-session",
        channel="cli",
        default_cwd="/proj",
    )
    base = build_default_approval("interactive", prompt_fn=prompt_fn)
    return build_safety_chain(cfg, interactive_approval=base), captured


def _request(command: str, *, call_id: str = "call-1") -> ApprovalRequest:
    return ApprovalRequest(
        run_id="run-1",
        session_id="cli-session",
        turn=1,
        call_id=call_id,
        tool_name="run_shell",
        arguments={"command": command},
        metadata={"cwd": "/proj"},
    )


@pytest.mark.e2e
# 验证 CLI 自动允许命中时直接批准，并且不触发终端审批提示。
async def test_cli_auto_allow_executes_without_terminal_prompt(tmp_path: Path) -> None:
    approval, captured = _approval_provider(
        _config(tmp_path),
        policy=_policy(tmp_path, enabled=True),
        actions=(),
    )

    decision = await approval.decide(_request("ls"))

    assert decision.outcome == "approved"
    assert captured == []


@pytest.mark.e2e
@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (ApprovalAction.ACCEPT_ONCE, "approved"),
        (ApprovalAction.REJECT, "rejected"),
    ],
)
# 验证 CLI 人工审批路径会调用终端提示，并按用户选择映射批准或拒绝。
async def test_cli_ask_path_uses_terminal_prompt(
    tmp_path: Path,
    action: ApprovalAction,
    expected: str,
) -> None:
    approval, captured = _approval_provider(
        _config(tmp_path),
        policy=_policy(tmp_path, enabled=False),
        actions=(action,),
    )

    decision = await approval.decide(_request("ls"))

    assert decision.outcome == expected
    assert len(captured) == 1
    assert captured[0].metadata["approval_channel"] == "cli"


@pytest.mark.e2e
# 验证真实危险规则命中时，CLI 自动审批仍进入终端提示并可被拒绝。
async def test_cli_danger_rule_forces_prompt_and_can_reject(tmp_path: Path) -> None:
    approval, captured = _approval_provider(
        _config(tmp_path),
        policy=_policy(tmp_path, enabled=True, rule_set=load_default_rules()),
        actions=(ApprovalAction.REJECT,),
    )

    decision = await approval.decide(_request("rm -rf tmp"))

    assert decision.outcome == "rejected"
    assert len(captured) == 1


@pytest.mark.e2e
# 验证 CLI 本次会话同意写入审批管理器线程授权，第二次同类调用跳过终端提示。
async def test_cli_accept_for_session_hits_grant_store_on_second_call(tmp_path: Path) -> None:
    approval, captured = _approval_provider(
        _config(tmp_path),
        policy=_policy(tmp_path, enabled=False),
        actions=(ApprovalAction.ACCEPT_FOR_SESSION,),
    )

    first = await approval.decide(_request("ls", call_id="call-1"))
    second = await approval.decide(_request("ls -la", call_id="call-2"))

    assert first.outcome == "approved"
    assert second.outcome == "approved"
    assert len(captured) == 1
    assert second.metadata["decision_source"] == "standard"
    assert approval.grant_store.session_grants("cli-session") == ()


@pytest.mark.e2e
# 验证 CLI 本次会话授权不会绕过后续真实危险规则。
async def test_cli_session_grant_does_not_skip_blocked_rules(tmp_path: Path) -> None:
    policy = _policy(tmp_path, enabled=False, rule_set=load_default_rules())
    approval, captured = _approval_provider(
        _config(tmp_path),
        policy=policy,
        actions=(ApprovalAction.ACCEPT_FOR_SESSION, ApprovalAction.REJECT),
    )

    first = await approval.decide(_request("ls", call_id="call-1"))
    policy.set_enabled("/proj", True)
    second = await approval.decide(_request("rm -rf tmp", call_id="call-2"))

    assert first.outcome == "approved"
    assert second.outcome == "rejected"
    assert len(captured) == 2
    assert captured[1].metadata["matched_rule"] == "bash_rm_any"
    assert approval.grant_store.session_grants("cli-session") == ()


@pytest.mark.e2e
# 验证缺少自动审批策略时，CLI 本次会话授权采用失败关闭并继续要求人工审批。
async def test_cli_session_grant_fails_closed_when_policy_missing(tmp_path: Path) -> None:
    approval, captured = _approval_provider(
        _config(tmp_path),
        policy=None,
        actions=(ApprovalAction.ACCEPT_FOR_SESSION, ApprovalAction.REJECT),
    )

    first = await approval.decide(_request("ls", call_id="call-1"))
    second = await approval.decide(_request("rm -rf tmp", call_id="call-2"))

    assert first.outcome == "approved"
    assert second.outcome == "rejected"
    assert len(captured) == 2
    assert approval.grant_store.session_grants("cli-session") == ()
