"""unit：CLI ApprovalManager 两选项终端 prompt。

覆盖当前 CLI 主链路：
1. 只处理 ``approval_channel="cli"`` 的 manager pending；
2. 终端人工输入只支持 ``y/yes`` 允许一次，其余输入拒绝；
3. 自动同意 / 自动拒绝 deadline 由 manager metadata 驱动；
4. 非 TTY 路径不会阻塞读取 stdin。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.contracts import ApprovalAction, ApprovalRequest
from hosts.cli.approval import (
    _resolve_cli_manager_deadline_ms,
    _resolve_cli_manager_timeout,
    build_cli_action_prompt,
)


def _req(metadata: dict[str, Any] | None = None) -> ApprovalRequest:
    """构造 CLI prompt 单测使用的审批请求。"""
    return ApprovalRequest(
        run_id="r1",
        session_id="s1",
        turn=1,
        call_id="call-1",
        tool_name="write_file",
        arguments={"path": "/tmp/x.txt"},
        metadata=metadata or {},
    )


@pytest.fixture
def patch_cli_manager_input(monkeypatch: pytest.MonkeyPatch):
    """提供 CLI manager 两选项输入替身，并记录 deadline。"""

    def _setup(answer: str | None, *, is_tty: bool = True) -> list[int]:
        deadlines: list[int] = []

        def fake_readline(timeout: Any) -> str | None:
            deadlines.append(timeout.deadline_ms)
            return answer

        monkeypatch.setattr(
            "hosts.cli.approval._blocking_cli_manager_readline_with_countdown",
            fake_readline,
        )
        monkeypatch.setattr("hosts.cli.approval._stdin_is_tty", lambda: is_tty)
        return deadlines

    return _setup


def _cli_metadata(**extra: Any) -> dict[str, Any]:
    """构造带 CLI manager channel 的 metadata。"""
    metadata: dict[str, Any] = {"approval_channel": "cli", "timeout_ms": 10_000}
    metadata.update(extra)
    return metadata


# 验证工厂返回 action-aware prompt，供 InteractiveApproval 走 ApprovalAction 分支。
def test_returned_function_is_action_aware() -> None:
    prompt = build_cli_action_prompt()
    assert getattr(prompt, "__action_aware__", False) is True


# 验证非 manager 请求直接拒绝，并且不读取终端输入。
@pytest.mark.asyncio
async def test_non_manager_request_rejects_without_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(_timeout: Any) -> str:
        raise AssertionError("非 manager 请求不应读取 CLI 输入")

    monkeypatch.setattr(
        "hosts.cli.approval._blocking_cli_manager_readline_with_countdown",
        fail_if_called,
    )
    monkeypatch.setattr("hosts.cli.approval._stdin_is_tty", lambda: True)

    prompt = build_cli_action_prompt()
    action = await prompt(_req())

    assert action is ApprovalAction.REJECT


# 验证 CLI manager 输入 y 时只允许本次调用。
@pytest.mark.asyncio
async def test_cli_manager_y_returns_accept_once(patch_cli_manager_input: Any) -> None:
    patch_cli_manager_input("y", is_tty=True)

    prompt = build_cli_action_prompt()
    action = await prompt(_req(metadata=_cli_metadata()))

    assert action is ApprovalAction.ACCEPT_ONCE


# 验证 CLI manager 输入 yes 时只允许本次调用。
@pytest.mark.asyncio
async def test_cli_manager_yes_returns_accept_once(patch_cli_manager_input: Any) -> None:
    patch_cli_manager_input("Yes", is_tty=True)

    prompt = build_cli_action_prompt()
    action = await prompt(_req(metadata=_cli_metadata()))

    assert action is ApprovalAction.ACCEPT_ONCE


# 验证 CLI manager 输入 n 时拒绝。
@pytest.mark.asyncio
async def test_cli_manager_n_returns_reject(patch_cli_manager_input: Any) -> None:
    patch_cli_manager_input("n", is_tty=True)

    prompt = build_cli_action_prompt()
    action = await prompt(_req(metadata=_cli_metadata()))

    assert action is ApprovalAction.REJECT


# 验证 CLI manager 空回车默认拒绝。
@pytest.mark.asyncio
async def test_cli_manager_empty_input_rejects(patch_cli_manager_input: Any) -> None:
    patch_cli_manager_input("", is_tty=True)

    prompt = build_cli_action_prompt()
    action = await prompt(_req(metadata=_cli_metadata()))

    assert action is ApprovalAction.REJECT


# 验证 CLI manager 输入 s 不再代表本次会话同意。
@pytest.mark.asyncio
async def test_cli_manager_session_key_rejects(patch_cli_manager_input: Any) -> None:
    patch_cli_manager_input("s", is_tty=True)

    prompt = build_cli_action_prompt()
    action = await prompt(_req(metadata=_cli_metadata()))

    assert action is ApprovalAction.REJECT


# 验证 CLI manager 任意未知输入拒绝。
@pytest.mark.asyncio
async def test_cli_manager_invalid_input_rejects(patch_cli_manager_input: Any) -> None:
    patch_cli_manager_input("maybe", is_tty=True)

    prompt = build_cli_action_prompt()
    action = await prompt(_req(metadata=_cli_metadata()))

    assert action is ApprovalAction.REJECT


# 验证危险 metadata 也走同一套 y/n 两选项。
@pytest.mark.asyncio
async def test_cli_manager_elevated_metadata_still_uses_two_choice(
    patch_cli_manager_input: Any,
) -> None:
    patch_cli_manager_input("y", is_tty=True)

    prompt = build_cli_action_prompt()
    action = await prompt(
        _req(
            metadata=_cli_metadata(
                severity="elevated",
                matched_rule="bash_rm_any",
            ),
        ),
    )

    assert action is ApprovalAction.ACCEPT_ONCE


# 验证 CLI manager 自动同意 deadline 到点后返回允许一次。
@pytest.mark.asyncio
async def test_cli_manager_auto_approve_timeout_accepts(
    patch_cli_manager_input: Any,
) -> None:
    patch_cli_manager_input(None, is_tty=True)

    prompt = build_cli_action_prompt()
    action = await prompt(
        _req(metadata=_cli_metadata(auto_approve_at_ms=110_000)),
    )

    assert action is ApprovalAction.ACCEPT_ONCE


# 验证 CLI manager 自动拒绝 deadline 到点后返回拒绝。
@pytest.mark.asyncio
async def test_cli_manager_auto_reject_timeout_rejects(
    patch_cli_manager_input: Any,
) -> None:
    patch_cli_manager_input(None, is_tty=True)

    prompt = build_cli_action_prompt()
    action = await prompt(
        _req(metadata=_cli_metadata(auto_reject_at_ms=110_000)),
    )

    assert action is ApprovalAction.REJECT


# 验证 CLI manager 非 TTY 且无自动 deadline 时直接拒绝。
@pytest.mark.asyncio
async def test_cli_manager_non_tty_without_auto_deadline_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(_timeout: Any) -> str:
        raise AssertionError("非 TTY 不应读取 CLI manager 输入")

    monkeypatch.setattr(
        "hosts.cli.approval._blocking_cli_manager_readline_with_countdown",
        fail_if_called,
    )
    monkeypatch.setattr("hosts.cli.approval._stdin_is_tty", lambda: False)

    prompt = build_cli_action_prompt()
    action = await prompt(_req(metadata=_cli_metadata()))

    assert action is ApprovalAction.REJECT


# 验证 CLI manager 非 TTY 携带自动同意 deadline 时等待后自动允许。
@pytest.mark.asyncio
async def test_cli_manager_non_tty_auto_approve_waits_then_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waited: list[int] = []

    async def fake_wait(deadline_ms: int) -> None:
        waited.append(deadline_ms)

    monkeypatch.setattr("hosts.cli.approval._stdin_is_tty", lambda: False)
    monkeypatch.setattr("hosts.cli.approval._wait_until_deadline", fake_wait)

    prompt = build_cli_action_prompt()
    action = await prompt(_req(metadata=_cli_metadata(auto_approve_at_ms=123_456)))

    assert action is ApprovalAction.ACCEPT_ONCE
    assert waited == [123_456]


# 验证 CLI manager EOF 视为拒绝。
@pytest.mark.asyncio
async def test_cli_manager_eof_returns_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_eof(_timeout: Any) -> str:
        raise EOFError

    monkeypatch.setattr(
        "hosts.cli.approval._blocking_cli_manager_readline_with_countdown",
        raise_eof,
    )
    monkeypatch.setattr("hosts.cli.approval._stdin_is_tty", lambda: True)

    prompt = build_cli_action_prompt()
    action = await prompt(_req(metadata=_cli_metadata()))

    assert action is ApprovalAction.REJECT


# 验证 CLI manager Ctrl-C 视为拒绝。
@pytest.mark.asyncio
async def test_cli_manager_keyboard_interrupt_returns_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_keyboard_interrupt(_timeout: Any) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "hosts.cli.approval._blocking_cli_manager_readline_with_countdown",
        raise_keyboard_interrupt,
    )
    monkeypatch.setattr("hosts.cli.approval._stdin_is_tty", lambda: True)

    prompt = build_cli_action_prompt()
    action = await prompt(_req(metadata=_cli_metadata()))

    assert action is ApprovalAction.REJECT


# 验证 CLI manager 终端等待最多 10 秒，更早的自动 deadline 优先。
def test_cli_manager_deadline_caps_terminal_wait_to_ten_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hosts.cli.approval.time.time", lambda: 100.0)

    assert _resolve_cli_manager_deadline_ms({"timeout_ms": 60_000}) == 110_000
    auto_approve = _resolve_cli_manager_timeout(
        {"timeout_ms": 60_000, "auto_approve_at_ms": 105_000},
    )
    assert auto_approve.deadline_ms == 105_000
    assert auto_approve.default_action is ApprovalAction.ACCEPT_ONCE

    capped_auto_approve = _resolve_cli_manager_timeout(
        {"timeout_ms": 60_000, "auto_approve_at_ms": 130_000},
    )
    assert capped_auto_approve.deadline_ms == 110_000
    assert capped_auto_approve.default_action is ApprovalAction.ACCEPT_ONCE

    auto_reject = _resolve_cli_manager_timeout(
        {"timeout_ms": 60_000, "auto_reject_at_ms": 105_000},
    )
    assert auto_reject.deadline_ms == 105_000
    assert auto_reject.default_action is ApprovalAction.REJECT
