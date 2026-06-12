"""CLI 审批提示行为单测。"""

from __future__ import annotations

import io
import time
from typing import Any

import hosts.cli.approval as cli_approval
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


# 验证动态倒计时刷新时会把用户已输入但未回车的内容重画回来。
def test_cli_manager_readline_repaints_prompt_with_typed_buffer(monkeypatch: Any) -> None:
    stdout = io.StringIO()
    stdin = io.StringIO()
    stdin.fileno = lambda: 0  # type: ignore[method-assign]
    chars = iter(["y", "\n"])
    timeout = cli_approval._CliManagerTimeout(
        deadline_ms=1_002_500,
        default_action=ApprovalAction.ACCEPT_ONCE,
    )

    monkeypatch.setattr(
        cli_approval.time,
        "time",
        iter([1_000.0, 1_001.001]).__next__,
    )
    monkeypatch.setattr(cli_approval.termios, "tcgetattr", lambda _fd: ["old"])
    monkeypatch.setattr(cli_approval.termios, "tcsetattr", lambda _fd, _when, _attrs: None)
    monkeypatch.setattr(cli_approval.tty, "setcbreak", lambda _fd: None)
    monkeypatch.setattr(stdin, "read", lambda _size=1: next(chars))
    monkeypatch.setattr(cli_approval.sys, "stdin", stdin)
    monkeypatch.setattr(cli_approval.sys, "stdout", stdout)
    monkeypatch.setattr(
        cli_approval.select,
        "select",
        lambda _read, _write, _err, _timeout: ([stdin], [], []),
    )

    result = cli_approval._blocking_cli_manager_readline_with_countdown(timeout)

    output = stdout.getvalue()
    assert result == "y"
    assert output.count("允许一次？") == 2
    assert "自动同意 3s > y" in output
    assert "自动同意 2s > y" in output


# 验证连续输入普通字符时只回显字符，不反复重画完整倒计时 prompt。
def test_cli_manager_readline_printable_input_does_not_rerender_prompt(
    monkeypatch: Any,
) -> None:
    stdout = io.StringIO()
    stdin = io.StringIO()
    stdin.fileno = lambda: 0  # type: ignore[method-assign]
    chars = iter(["1", "2", "3", "\n"])
    timeout = cli_approval._CliManagerTimeout(
        deadline_ms=1_002_500,
        default_action=ApprovalAction.ACCEPT_ONCE,
    )

    monkeypatch.setattr(cli_approval.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(cli_approval.termios, "tcgetattr", lambda _fd: ["old"])
    monkeypatch.setattr(cli_approval.termios, "tcsetattr", lambda _fd, _when, _attrs: None)
    monkeypatch.setattr(cli_approval.tty, "setcbreak", lambda _fd: None)
    monkeypatch.setattr(stdin, "read", lambda _size=1: next(chars))
    monkeypatch.setattr(cli_approval.sys, "stdin", stdin)
    monkeypatch.setattr(cli_approval.sys, "stdout", stdout)
    monkeypatch.setattr(
        cli_approval.select,
        "select",
        lambda _read, _write, _err, _timeout: ([stdin], [], []),
    )

    result = cli_approval._blocking_cli_manager_readline_with_countdown(timeout)

    output = stdout.getvalue()
    assert result == "123"
    assert output.count("允许一次？") == 1
    assert output.endswith("123\n")
