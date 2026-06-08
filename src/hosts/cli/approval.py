"""CLI ApprovalManager 终端审批 UX。

本模块只服务 ``ApprovalManager`` 的 ``channel="cli"`` pending 请求。CLI
审批入口统一由 ``src/hosts/cli/main.py`` 装配：

1. ``ApprovalManager.request(channel="cli")`` 创建 pending；
2. ``CLIApprovalEventSink`` 将 pending 投影成 ``ApprovalRequest``；
3. 本模块显示终端两选项提示；
4. sink 将结果回写 ``manager.resolve()``。

CLI 人工选项只有两种：

- ``y`` / ``yes``：允许一次；
- ``n`` / EOF / Ctrl-C / 其他输入：拒绝；
- 空回车：使用当前默认动作。

自动审批由 manager metadata 驱动：

- ``auto_approve_at_ms``：倒计时到点自动同意；
- ``auto_reject_at_ms``：倒计时到点自动拒绝。

缺少 ``approval_channel="cli"`` 的请求直接拒绝，避免旧 direct prompt 链路
重新进入 CLI。
"""

from __future__ import annotations

import asyncio
import select
import sys
import time
from dataclasses import dataclass
from typing import Any

import click

from core.contracts import ApprovalAction, ApprovalRequest
from tools.runtime.approval import PromptActionFn, mark_action_aware

_CLI_MANAGER_MAX_WAIT_MS = 10_000


@dataclass(frozen=True)
class _CliManagerTimeout:
    """CLI manager 倒计时配置。"""

    deadline_ms: int
    default_action: ApprovalAction


def build_cli_action_prompt() -> PromptActionFn:
    """构造 CLI ApprovalManager prompt 函数。"""

    @mark_action_aware
    async def _prompt(request: ApprovalRequest) -> ApprovalAction:
        return await _prompt_user(request)

    return _prompt


async def _prompt_user(request: ApprovalRequest) -> ApprovalAction:
    """根据 manager metadata 显示终端审批，并返回最终动作。"""
    metadata: dict[str, Any] = dict(request.metadata or {})
    is_tty = _stdin_is_tty()
    is_elevated = _metadata_is_elevated(metadata)
    _print_request_summary(request, is_elevated=is_elevated, is_tty=is_tty)

    if metadata.get("approval_channel") != "cli":
        click.echo("[approval] 非 CLI manager 审批请求默认拒绝。", err=True)
        return ApprovalAction.REJECT

    return await _prompt_cli_manager_two_choice(metadata=metadata, is_tty=is_tty)


async def _prompt_cli_manager_two_choice(
    *,
    metadata: dict[str, Any],
    is_tty: bool,
) -> ApprovalAction:
    """CLI manager 两选项：允许一次 / 拒绝，超时按规则默认动作处理。"""
    timeout = _resolve_cli_manager_timeout(metadata)
    if not is_tty:
        if _has_auto_deadline(metadata):
            default_text = (
                "同意" if timeout.default_action is ApprovalAction.ACCEPT_ONCE else "拒绝"
            )
            click.echo(f"[approval] CLI 非 TTY 请求将在倒计时后自动{default_text}。", err=True)
            await _wait_until_deadline(timeout.deadline_ms)
            return timeout.default_action
        click.echo("[approval] CLI 非 TTY 请求默认拒绝。", err=True)
        return ApprovalAction.REJECT

    raw = await _read_cli_manager_choice(timeout=timeout)
    if raw is None:
        return timeout.default_action
    answer = (raw or "").strip().lower()
    if answer in {"y", "yes"}:
        return ApprovalAction.ACCEPT_ONCE
    if answer == "":
        return timeout.default_action
    return ApprovalAction.REJECT


async def _read_cli_manager_choice(*, timeout: _CliManagerTimeout) -> str | None:
    """读取 CLI manager 两选项输入；None 表示倒计时到点。"""
    try:
        return await asyncio.to_thread(_blocking_cli_manager_readline_with_countdown, timeout)
    except (EOFError, KeyboardInterrupt):
        return ""


async def _wait_until_deadline(deadline_ms: int) -> None:
    """非 TTY 路径等待默认动作 deadline。"""
    now_ms = int(time.time() * 1000)
    delay_seconds = max(0.0, (deadline_ms - now_ms) / 1000.0)
    await asyncio.sleep(delay_seconds)


def _blocking_cli_manager_readline_with_countdown(timeout: _CliManagerTimeout) -> str | None:
    """单行动态倒计时读取输入，避免每秒刷出新行。"""
    while True:
        now_ms = int(time.time() * 1000)
        remaining_ms = timeout.deadline_ms - now_ms
        if remaining_ms <= 0:
            final_text = (
                "自动同意" if timeout.default_action is ApprovalAction.ACCEPT_ONCE else "自动拒绝"
            )
            sys.stdout.write(f"\r\033[K[approval] {final_text}。\n")
            sys.stdout.flush()
            return None

        prompt_text = _format_cli_manager_prompt(
            remaining_ms=remaining_ms,
            default_action=timeout.default_action,
        )
        sys.stdout.write("\r\033[K" + prompt_text)
        sys.stdout.flush()

        wait_seconds = min(1.0, max(0.0, remaining_ms / 1000.0))
        try:
            readable, _, _ = select.select([sys.stdin], [], [], wait_seconds)
        except (OSError, ValueError):
            line = sys.stdin.readline()
            if line == "":
                raise EOFError from None
            return line.rstrip("\n")

        if not readable:
            continue

        line = sys.stdin.readline()
        if line == "":
            raise EOFError from None
        return line.rstrip("\n")


def _format_cli_manager_prompt(
    *,
    remaining_ms: int,
    default_action: ApprovalAction,
) -> str:
    """格式化单行倒计时 prompt。"""
    remaining_seconds = max(0, (remaining_ms + 999) // 1000)
    auto_text = "自动同意" if default_action is ApprovalAction.ACCEPT_ONCE else "自动拒绝"
    enter_text = "默认同意" if default_action is ApprovalAction.ACCEPT_ONCE else "默认拒绝"
    return (
        f"允许一次？[y]=允许  [n]=拒绝  [Enter]={enter_text}  {auto_text} {remaining_seconds}s > "
    )


def _stdin_is_tty() -> bool:
    """读取 stdin TTY 状态；无 stdin 时按非 TTY 处理。"""
    try:
        return bool(sys.stdin.isatty())
    except (ValueError, AttributeError):
        return False


def _metadata_is_elevated(metadata: dict[str, Any]) -> bool:
    """CLI 摘要展示用：危险规则 metadata 按高风险显示。"""
    return bool(
        metadata.get("severity") == "elevated"
        or metadata.get("matched_rule")
        or metadata.get("blocked_by_rule")
        or metadata.get("auto_reject_at_ms")
        or metadata.get("autoRejectAtMs")
    )


def _resolve_cli_manager_timeout(metadata: dict[str, Any]) -> _CliManagerTimeout:
    """解析 CLI 等待截止时间与默认动作。"""
    now_ms = int(time.time() * 1000)
    candidates = [now_ms + _CLI_MANAGER_MAX_WAIT_MS]
    auto_reject_at_ms = _first_int_metadata(
        metadata,
        "auto_reject_at_ms",
        "autoRejectAtMs",
    )
    if auto_reject_at_ms is not None:
        candidates.append(auto_reject_at_ms)
        return _CliManagerTimeout(
            deadline_ms=min(candidates),
            default_action=ApprovalAction.REJECT,
        )

    auto_approve_at_ms = _first_int_metadata(
        metadata,
        "auto_approve_at_ms",
        "autoApproveAtMs",
    )
    if auto_approve_at_ms is not None:
        candidates.append(auto_approve_at_ms)
        return _CliManagerTimeout(
            deadline_ms=min(candidates),
            default_action=ApprovalAction.ACCEPT_ONCE,
        )

    timeout_ms = _first_int_metadata(metadata, "timeout_ms", "timeoutMs")
    if timeout_ms is not None and timeout_ms > 0:
        candidates.append(now_ms + timeout_ms)
    return _CliManagerTimeout(
        deadline_ms=min(candidates),
        default_action=ApprovalAction.REJECT,
    )


def _resolve_cli_manager_deadline_ms(metadata: dict[str, Any]) -> int:
    """测试辅助：返回 CLI manager deadline。"""
    return _resolve_cli_manager_timeout(metadata).deadline_ms


def _has_auto_deadline(metadata: dict[str, Any]) -> bool:
    """判断请求是否携带自动审批 deadline。"""
    return (
        _first_int_metadata(metadata, "auto_reject_at_ms", "autoRejectAtMs") is not None
        or _first_int_metadata(metadata, "auto_approve_at_ms", "autoApproveAtMs") is not None
    )


def _first_int_metadata(metadata: dict[str, Any], *keys: str) -> int | None:
    """按顺序读取第一个可转成 int 的 metadata 值。"""
    for key in keys:
        value = metadata.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _print_request_summary(
    request: ApprovalRequest,
    *,
    is_elevated: bool,
    is_tty: bool,
) -> None:
    """统一打印请求摘要，便于用户判断。"""
    args_preview = _format_arguments(request.arguments)
    reason = f" reason={request.reason}" if request.reason else ""
    severity = "ELEVATED" if is_elevated else "STANDARD"
    suffix = "" if is_tty else " (non-TTY)"
    click.echo(
        f"[approval/{severity}{suffix}] tool={request.tool_name} args={args_preview}{reason}",
        err=True,
    )


def _format_arguments(arguments: dict[str, object] | None) -> str:
    """把工具参数截断成一行摘要。"""
    if not arguments:
        return "{}"
    parts = []
    for key, value in arguments.items():
        text = repr(value)
        if len(text) > 80:
            text = text[:77] + "..."
        parts.append(f"{key}={text}")
    return "{" + ", ".join(parts) + "}"


__all__ = ["build_cli_action_prompt"]
