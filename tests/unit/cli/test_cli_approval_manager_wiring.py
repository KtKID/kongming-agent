"""CLI 审批管理器装配测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import hosts.cli.main as cli_main
from core.contracts import ApprovalAction, ApprovalRequest
from safety.approval.manager import reset_for_testing


def _install_prompt_module(monkeypatch, prompt) -> None:
    """在 Windows 测试中注入最小 CLI prompt 模块，隔离 POSIX termios。"""
    module = ModuleType("hosts.cli.approval")
    module.build_cli_action_prompt = lambda: prompt  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hosts.cli.approval", module)


# 验证 CLI 提示函数会通过 CLI 接收器进入审批管理器，再回到终端审批函数。
async def test_cli_manager_prompt_fn_routes_through_cli_sink(monkeypatch, tmp_path: Path) -> None:
    reset_for_testing()
    monkeypatch.chdir(tmp_path)
    captured: list[ApprovalRequest] = []

    async def prompt(request: ApprovalRequest) -> ApprovalAction:
        captured.append(request)
        return ApprovalAction.ACCEPT_ONCE

    prompt.__action_aware__ = True  # type: ignore[attr-defined]
    _install_prompt_module(monkeypatch, prompt)

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


# 验证 CLI 装配断开旧倒计时 policy，审批请求不再投影 deadline。
async def test_cli_manager_prompt_fn_disconnects_auto_approval_deadline(
    monkeypatch,
    tmp_path: Path,
) -> None:
    reset_for_testing()
    monkeypatch.chdir(tmp_path)
    captured: list[ApprovalRequest] = []

    async def prompt(request: ApprovalRequest) -> ApprovalAction:
        captured.append(request)
        return ApprovalAction.ACCEPT_ONCE

    prompt.__action_aware__ = True  # type: ignore[attr-defined]
    _install_prompt_module(monkeypatch, prompt)

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
    assert "auto_approve_at_ms" not in captured[0].metadata
    assert "auto_reject_at_ms" not in captured[0].metadata
