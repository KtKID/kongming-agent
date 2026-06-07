"""CLI 审批管理器装配测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cli.approval
import cli.main as cli_main
from core.contracts import ApprovalAction, ApprovalRequest
from safety.approval_manager import reset_for_testing


class _AutoAllowDecision:
    auto_eligible = True
    blocked_by_rule = None
    timeout_ms = 10_000


class _AutoAllowPolicy:
    def classify(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        cwd: str,
        is_elevated: bool,
    ) -> _AutoAllowDecision:
        del tool_name, tool_input, cwd, is_elevated
        return _AutoAllowDecision()

    def is_enabled_for(self, cwd: str) -> bool:
        del cwd
        return True


# 验证 CLI 提示函数会通过 CLI 接收器进入审批管理器，再回到终端审批函数。
async def test_cli_manager_prompt_fn_routes_through_cli_sink(monkeypatch, tmp_path: Path) -> None:
    reset_for_testing()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_build_cli_auto_approval_policy", lambda: None)
    captured: list[ApprovalRequest] = []

    async def prompt(request: ApprovalRequest) -> ApprovalAction:
        captured.append(request)
        return ApprovalAction.ACCEPT_FOR_SESSION

    prompt.__action_aware__ = True  # type: ignore[attr-defined]
    monkeypatch.setattr(cli.approval, "build_cli_action_prompt", lambda: prompt)

    prompt_fn = cli_main._build_cli_manager_prompt_fn("cli-session")
    action = await prompt_fn(
        ApprovalRequest(
            run_id="run-1",
            session_id="cli-session",
            turn=2,
            call_id="call-1",
            tool_name="run_shell",
            arguments={"command": "ls"},
            reason="needs approval",
            metadata={},
        )
    )

    assert action == ApprovalAction.ACCEPT_ONCE
    assert captured[0].run_id == "run-1"
    assert captured[0].session_id == "cli-session"
    assert captured[0].turn == 2
    assert captured[0].call_id == "call-1"
    assert captured[0].metadata["approval_channel"] == "cli"
    assert captured[0].metadata["cwd"] == str(tmp_path)


# 验证 CLI 自动允许命中时会进入 CLI 接收器，并携带自动同意 deadline。
async def test_cli_manager_prompt_fn_auto_allow_projects_deadline(
    monkeypatch,
    tmp_path: Path,
) -> None:
    reset_for_testing()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_build_cli_auto_approval_policy", lambda: _AutoAllowPolicy())
    captured: list[ApprovalRequest] = []

    async def prompt(request: ApprovalRequest) -> ApprovalAction:
        captured.append(request)
        return ApprovalAction.ACCEPT_ONCE

    prompt.__action_aware__ = True  # type: ignore[attr-defined]
    monkeypatch.setattr(cli.approval, "build_cli_action_prompt", lambda: prompt)

    prompt_fn = cli_main._build_cli_manager_prompt_fn("cli-session")
    action = await prompt_fn(
        ApprovalRequest(
            run_id="run-1",
            session_id="cli-session",
            turn=1,
            call_id="call-1",
            tool_name="run_shell",
            arguments={"command": "ls"},
            metadata={},
        )
    )

    assert action == ApprovalAction.ACCEPT_ONCE
    assert len(captured) == 1
    assert captured[0].metadata["approval_channel"] == "cli"
    assert captured[0].metadata["auto_approve_at_ms"] is not None
    assert captured[0].metadata["auto_reject_at_ms"] is None
