"""CLI 审批提示行为单测。"""

from __future__ import annotations

import time
from typing import Any

import cli.approval as cli_approval
from core.contracts import ApprovalAction


# 验证安全路径已有自动同意 deadline 时，空回车走默认同意，避免残留换行误拒绝。
async def test_cli_manager_blank_input_uses_auto_approve_default(monkeypatch: Any) -> None:
    async def fake_read(*, timeout: Any) -> str:
        del timeout
        return ""

    monkeypatch.setattr(cli_approval, "_read_cli_manager_choice", fake_read)

    action = await cli_approval._prompt_cli_manager_two_choice(
        metadata={"auto_approve_at_ms": int(time.time() * 1000) + 10_000},
        is_tty=True,
    )

    assert action is ApprovalAction.ACCEPT_ONCE


# 验证普通人工审批路径的空回车仍保持默认拒绝。
async def test_cli_manager_blank_input_rejects_without_auto_deadline(monkeypatch: Any) -> None:
    async def fake_read(*, timeout: Any) -> str:
        del timeout
        return ""

    monkeypatch.setattr(cli_approval, "_read_cli_manager_choice", fake_read)

    action = await cli_approval._prompt_cli_manager_two_choice(metadata={}, is_tty=True)

    assert action is ApprovalAction.REJECT


# 验证危险路径已有自动拒绝 deadline 时，空回车走默认拒绝。
async def test_cli_manager_blank_input_uses_auto_reject_default(monkeypatch: Any) -> None:
    async def fake_read(*, timeout: Any) -> str:
        del timeout
        return ""

    monkeypatch.setattr(cli_approval, "_read_cli_manager_choice", fake_read)

    action = await cli_approval._prompt_cli_manager_two_choice(
        metadata={"auto_reject_at_ms": int(time.time() * 1000) + 10_000},
        is_tty=True,
    )

    assert action is ApprovalAction.REJECT


# 验证提示文案明确展示 Enter 的默认动作。
def test_cli_manager_prompt_names_enter_default_action() -> None:
    approve_prompt = cli_approval._format_cli_manager_prompt(
        remaining_ms=10_000,
        default_action=ApprovalAction.ACCEPT_ONCE,
    )
    reject_prompt = cli_approval._format_cli_manager_prompt(
        remaining_ms=10_000,
        default_action=ApprovalAction.REJECT,
    )

    assert "[Enter]=默认同意" in approve_prompt
    assert "[Enter]=默认拒绝" in reject_prompt
