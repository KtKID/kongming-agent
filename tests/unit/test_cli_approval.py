"""unit：cli/approval.py 三按钮 UX 行为。

补 x-cr P1 #2：cli/approval.py 单测覆盖率为 0 的盲区。

覆盖：
1. TTY + standard：[y]/[s]/[p]/[n] 各路径正确映射 ApprovalAction
2. TTY + elevated：仅 typed confirm，token 比对正确
3. 非 TTY：仅 [y]/[n]
4. [p]persist 二次确认拒绝时降级 ACCEPT_FOR_SESSION
5. elevated 缺 confirm_token 时拒绝
6. 无效输入循环重试
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from cli.approval import (
    _resolve_cli_manager_deadline_ms,
    _resolve_cli_manager_timeout,
    build_cli_action_prompt,
)
from core.contracts import ApprovalAction, ApprovalRequest


def _req(metadata: dict[str, Any] | None = None) -> ApprovalRequest:
    return ApprovalRequest(
        run_id="r1",
        session_id="s1",
        turn=1,
        call_id="call-1",
        tool_name="write_file",
        arguments={"path": "/tmp/x.txt"},
        metadata=metadata or {},
    )


def _make_input_iter(answers: list[str]) -> Iterator[str]:
    """把答案列表变成迭代器，每次 _read_line 取下一个。"""
    yield from answers


@pytest.fixture
def patch_input(monkeypatch: pytest.MonkeyPatch):
    """提供一个工具：注入答案序列 + 强制 stdin TTY 状态。"""

    def _setup(answers: list[str], *, is_tty: bool = True) -> None:
        it = _make_input_iter(answers)

        def fake_blocking_readline(_prompt: str) -> str:
            try:
                return next(it)
            except StopIteration as exc:
                raise EOFError from exc

        monkeypatch.setattr("cli.approval._blocking_readline", fake_blocking_readline)
        monkeypatch.setattr("cli.approval._stdin_is_tty", lambda: is_tty)

    return _setup


@pytest.fixture
def patch_cli_manager_input(monkeypatch: pytest.MonkeyPatch):
    """提供 CLI manager 两选项输入替身，并记录 deadline。"""

    def _setup(answer: str | None, *, is_tty: bool = True) -> list[int]:
        deadlines: list[int] = []

        def fake_readline(timeout: Any) -> str | None:
            deadlines.append(timeout.deadline_ms)
            return answer

        monkeypatch.setattr(
            "cli.approval._blocking_cli_manager_readline_with_countdown",
            fake_readline,
        )
        monkeypatch.setattr("cli.approval._stdin_is_tty", lambda: is_tty)
        return deadlines

    return _setup


# ---------------------------------------------------------------------------
# §1 TTY + standard 路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tty_standard_y_returns_accept_once(patch_input: Any) -> None:
    patch_input(["y"], is_tty=True)
    prompt = build_cli_action_prompt()
    action = await prompt(_req())
    assert action is ApprovalAction.ACCEPT_ONCE


@pytest.mark.asyncio
async def test_tty_standard_s_returns_accept_for_session(patch_input: Any) -> None:
    patch_input(["s"], is_tty=True)
    prompt = build_cli_action_prompt()
    action = await prompt(_req())
    assert action is ApprovalAction.ACCEPT_FOR_SESSION


@pytest.mark.asyncio
async def test_tty_standard_p_with_confirmed_persist(patch_input: Any) -> None:
    """[p] 主选 + 二次确认 [y] → ACCEPT_PERSIST。"""
    patch_input(["p", "y"], is_tty=True)
    prompt = build_cli_action_prompt()
    action = await prompt(_req())
    assert action is ApprovalAction.ACCEPT_PERSIST


@pytest.mark.asyncio
async def test_tty_standard_p_with_rejected_persist_downgrades(patch_input: Any) -> None:
    """[p] 主选 + 二次确认 [n] → 降级 ACCEPT_FOR_SESSION（保留放行初衷）。"""
    patch_input(["p", "n"], is_tty=True)
    prompt = build_cli_action_prompt()
    action = await prompt(_req())
    assert action is ApprovalAction.ACCEPT_FOR_SESSION


@pytest.mark.asyncio
async def test_tty_standard_n_returns_reject(patch_input: Any) -> None:
    patch_input(["n"], is_tty=True)
    prompt = build_cli_action_prompt()
    action = await prompt(_req())
    assert action is ApprovalAction.REJECT


@pytest.mark.asyncio
async def test_tty_standard_empty_input_treated_as_reject(patch_input: Any) -> None:
    """回车默认 = reject（安全优先）。"""
    patch_input([""], is_tty=True)
    prompt = build_cli_action_prompt()
    action = await prompt(_req())
    assert action is ApprovalAction.REJECT


@pytest.mark.asyncio
async def test_tty_standard_invalid_input_loops_until_valid(patch_input: Any) -> None:
    """无效输入循环；最终输入 [n] 才退出。"""
    patch_input(["foo", "bar", "n"], is_tty=True)
    prompt = build_cli_action_prompt()
    action = await prompt(_req())
    assert action is ApprovalAction.REJECT


# ---------------------------------------------------------------------------
# §2 TTY + elevated 路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tty_elevated_correct_token_returns_accept_once(patch_input: Any) -> None:
    patch_input(["abc12345"], is_tty=True)
    prompt = build_cli_action_prompt()
    action = await prompt(
        _req(metadata={"policy_hint": "elevated", "confirm_token": "abc12345"}),
    )
    assert action is ApprovalAction.ACCEPT_ONCE


@pytest.mark.asyncio
async def test_tty_elevated_wrong_token_rejects_no_retry(patch_input: Any) -> None:
    """elevated 输入 token 错误一次即拒绝，不重试（防爆破 / 反反射点头）。"""
    patch_input(["wrongtoken"], is_tty=True)
    prompt = build_cli_action_prompt()
    action = await prompt(
        _req(metadata={"policy_hint": "elevated", "confirm_token": "abc12345"}),
    )
    assert action is ApprovalAction.REJECT


@pytest.mark.asyncio
async def test_tty_elevated_missing_token_rejects_safely(patch_input: Any) -> None:
    """elevated 但 metadata 缺 confirm_token → 保守拒绝（不向用户读输入）。"""
    patch_input([], is_tty=True)  # 不应被读取
    prompt = build_cli_action_prompt()
    action = await prompt(_req(metadata={"policy_hint": "elevated"}))
    assert action is ApprovalAction.REJECT


@pytest.mark.asyncio
async def test_tty_elevated_does_not_offer_session_or_persist(patch_input: Any) -> None:
    """elevated 路径不接受 [s] / [p]：输入 's' 也视为非 token，REJECT。"""
    patch_input(["s"], is_tty=True)  # 不是 token
    prompt = build_cli_action_prompt()
    action = await prompt(
        _req(metadata={"policy_hint": "elevated", "confirm_token": "ttkn1234"}),
    )
    assert action is ApprovalAction.REJECT


# ---------------------------------------------------------------------------
# §3 非 TTY（CI / 管道）路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_tty_y_returns_accept_once(patch_input: Any) -> None:
    patch_input(["y"], is_tty=False)
    prompt = build_cli_action_prompt()
    action = await prompt(_req())
    assert action is ApprovalAction.ACCEPT_ONCE


@pytest.mark.asyncio
async def test_non_tty_n_returns_reject(patch_input: Any) -> None:
    patch_input(["n"], is_tty=False)
    prompt = build_cli_action_prompt()
    action = await prompt(_req())
    assert action is ApprovalAction.REJECT


@pytest.mark.asyncio
async def test_non_tty_empty_returns_reject(patch_input: Any) -> None:
    patch_input([""], is_tty=False)
    prompt = build_cli_action_prompt()
    action = await prompt(_req())
    assert action is ApprovalAction.REJECT


# ---------------------------------------------------------------------------
# §3.5 CLI manager 两选项路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_manager_y_returns_accept_once_without_token_flow(
    patch_cli_manager_input: Any,
) -> None:
    """CLI manager 模式遇到 elevated metadata 时仍只接受 y/n 两选项。"""
    patch_cli_manager_input("y", is_tty=True)
    prompt = build_cli_action_prompt()
    action = await prompt(
        _req(
            metadata={
                "approval_channel": "cli",
                "policy_hint": "elevated",
                "confirm_token": "abc12345",
                "timeout_ms": 10_000,
            },
        ),
    )
    assert action is ApprovalAction.ACCEPT_ONCE


@pytest.mark.asyncio
async def test_cli_manager_session_key_rejects(
    patch_cli_manager_input: Any,
) -> None:
    """CLI manager 模式只接受 y，输入 s 直接按拒绝处理。"""
    patch_cli_manager_input("s", is_tty=True)
    prompt = build_cli_action_prompt()
    action = await prompt(_req(metadata={"approval_channel": "cli", "timeout_ms": 10_000}))
    assert action is ApprovalAction.REJECT


@pytest.mark.asyncio
async def test_cli_manager_empty_input_rejects(
    patch_cli_manager_input: Any,
) -> None:
    """CLI manager 模式回车默认拒绝。"""
    patch_cli_manager_input("", is_tty=True)
    prompt = build_cli_action_prompt()
    action = await prompt(_req(metadata={"approval_channel": "cli", "timeout_ms": 10_000}))
    assert action is ApprovalAction.REJECT


@pytest.mark.asyncio
async def test_cli_manager_auto_approve_timeout_accepts(
    patch_cli_manager_input: Any,
) -> None:
    """CLI manager 安全路径倒计时到点后自动同意。"""
    patch_cli_manager_input(None, is_tty=True)
    prompt = build_cli_action_prompt()
    action = await prompt(
        _req(metadata={"approval_channel": "cli", "auto_approve_at_ms": 110_000}),
    )
    assert action is ApprovalAction.ACCEPT_ONCE


@pytest.mark.asyncio
async def test_cli_manager_auto_reject_timeout_rejects(
    patch_cli_manager_input: Any,
) -> None:
    """CLI manager 危险路径倒计时到点后自动拒绝。"""
    patch_cli_manager_input(None, is_tty=True)
    prompt = build_cli_action_prompt()
    action = await prompt(
        _req(metadata={"approval_channel": "cli", "auto_reject_at_ms": 110_000}),
    )
    assert action is ApprovalAction.REJECT


@pytest.mark.asyncio
async def test_cli_manager_non_tty_defaults_to_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI manager 非 TTY 直接拒绝，避免后台等待 stdin。"""

    def fail_if_called(_timeout: Any) -> str:
        raise AssertionError("非 TTY 不应读取 CLI manager 输入")

    monkeypatch.setattr(
        "cli.approval._blocking_cli_manager_readline_with_countdown",
        fail_if_called,
    )
    monkeypatch.setattr("cli.approval._stdin_is_tty", lambda: False)

    prompt = build_cli_action_prompt()
    action = await prompt(_req(metadata={"approval_channel": "cli", "timeout_ms": 10_000}))
    assert action is ApprovalAction.REJECT


def test_cli_manager_deadline_caps_terminal_wait_to_ten_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI manager 终端等待最多 10 秒；更早的 auto deadline 优先。"""
    monkeypatch.setattr("cli.approval.time.time", lambda: 100.0)

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

    assert (
        _resolve_cli_manager_deadline_ms(
            {"timeout_ms": 60_000, "auto_reject_at_ms": 105_000},
        )
        == 105_000
    )


# ---------------------------------------------------------------------------
# §4 EOF / KeyboardInterrupt 安全降级
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eof_treated_as_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdin EOF 视为 REJECT（标准 _read_line 已处理）。"""

    async def fake_to_thread(_func: Any, _prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)
    monkeypatch.setattr("cli.approval._stdin_is_tty", lambda: True)
    prompt = build_cli_action_prompt()
    action = await prompt(_req())
    assert action is ApprovalAction.REJECT


@pytest.mark.asyncio
async def test_keyboard_interrupt_treated_as_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_to_thread(_func: Any, _prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)
    monkeypatch.setattr("cli.approval._stdin_is_tty", lambda: True)
    prompt = build_cli_action_prompt()
    action = await prompt(_req())
    assert action is ApprovalAction.REJECT


# ---------------------------------------------------------------------------
# §5 build_cli_action_prompt 自身契约
# ---------------------------------------------------------------------------


def test_returned_function_is_action_aware() -> None:
    """工厂返回的函数必须被 mark_action_aware 装饰。"""
    prompt = build_cli_action_prompt()
    # 装饰器添加了 __action_aware__ 属性
    assert getattr(prompt, "__action_aware__", False) is True


@pytest.mark.asyncio
async def test_config_path_hint_propagates_to_persist_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`config_path_hint` 应该在 persist 二次确认文案中出现。"""
    prompts_seen: list[str] = []
    answers = iter(["p", "n"])

    def fake_block_seq(prompt_text: str) -> str:
        prompts_seen.append(prompt_text)
        return next(answers)

    monkeypatch.setattr("cli.approval._blocking_readline", fake_block_seq)
    monkeypatch.setattr("cli.approval._stdin_is_tty", lambda: True)

    prompt = build_cli_action_prompt(config_path_hint="custom/path/foo.yaml")
    await prompt(_req())
    assert any("custom/path/foo.yaml" in p for p in prompts_seen)
