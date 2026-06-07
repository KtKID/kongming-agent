"""CLI 审批 UX。

提供 :func:`build_cli_action_prompt` 工厂，返回一个
:class:`tools.approval.PromptActionFn`，被 :class:`tools.approval.InteractiveApproval`
透明消费。

UX 形态：

- CLI manager：``[y]允许一次 / [n]拒绝 / [Enter]默认动作``，带单行倒计时
- TTY + standard：``[y]es once / [s]ession / [p]ersist / [n]o``
- TTY + elevated：``[y]es (typed confirm) / [n]o``，[s]/[p] 隐藏
- 非 TTY（CI / 管道）：``[y]es / [n]o``，[s]/[p] 隐藏，[y] 等价 ACCEPT_ONCE

[p]persist 选中后弹"This will write rule to .kongming/config.yaml. Confirm?"
二次确认；通过返回 ``ACCEPT_PERSIST``，否则降级 ``ACCEPT_FOR_SESSION``。

不依赖：

- 本模块只 import :mod:`core.contracts` + :mod:`tools.approval`，不反向
  import :mod:`safety/`：cli → tools → core。

实现注意：

- 输入读取走 :func:`asyncio.to_thread`，避免阻塞事件循环、且不与
  :mod:`prompt_toolkit` 抢 TTY（与 :class:`host.cli_adapter.CLIAdapter.prompt_approval`
  策略一致）。
- elevated 模式的 ``confirm_token`` 校验逻辑由调用方（M4 ConsentResolver）
  在 ``ApprovalRequest.metadata`` 中带入；本 prompt 函数仅在 UI 上要求
  用户输入 token，比对结果决定 accept/reject。
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
from tools.approval import PromptActionFn, mark_action_aware

# 二次确认提示文案：persist 持久化路径强 UX 提醒
_PERSIST_CONFIRM_PROMPT = (
    "This will write rule to .kongming/config.yaml (safety.allow_writes / "
    "safety.allow_tools_silent). Confirm? [y/N] "
)
_CLI_MANAGER_MAX_WAIT_MS = 10_000


@dataclass(frozen=True)
class _CliManagerTimeout:
    """CLI manager 倒计时配置。"""

    deadline_ms: int
    default_action: ApprovalAction


def build_cli_action_prompt(
    *,
    config_path_hint: str = ".kongming/config.yaml",
) -> PromptActionFn:
    """构造一个三按钮 CLI prompt 函数。

    Args:
        config_path_hint: 二次确认提示中显示的目标 yaml 路径，仅 UI 文案用。

    Returns:
        被 :func:`tools.approval.mark_action_aware` 标记的协程函数；
        :class:`tools.approval.InteractiveApproval` 据此走 action 分支。
    """

    @mark_action_aware
    async def _prompt(request: ApprovalRequest) -> ApprovalAction:
        return await _prompt_user(request, config_path_hint=config_path_hint)

    return _prompt


async def _prompt_user(
    request: ApprovalRequest,
    *,
    config_path_hint: str,
) -> ApprovalAction:
    """根据请求 metadata 与 TTY 状态分发 UX，返回最终 ApprovalAction。"""
    metadata: dict[str, Any] = dict(request.metadata or {})
    is_cli_manager = metadata.get("approval_channel") == "cli"
    is_elevated = (
        _metadata_is_elevated(metadata)
        if is_cli_manager
        else metadata.get("policy_hint") == "elevated"
    )
    confirm_token: str | None = metadata.get("confirm_token")
    is_tty = _stdin_is_tty()

    # 打印请求摘要（始终输出，给所有路径用）
    _print_request_summary(request, is_elevated=is_elevated, is_tty=is_tty)

    # ---- CLI manager 路径：仅单次允许 / 拒绝 ----
    if is_cli_manager:
        return await _prompt_cli_manager_two_choice(metadata=metadata, is_tty=is_tty)

    # ---- elevated 路径：仅 typed confirm + 拒绝 ----
    if is_elevated:
        return await _prompt_elevated(confirm_token=confirm_token)

    # ---- 非 TTY 路径：仅 [y]/[n]，[y] = ACCEPT_ONCE ----
    if not is_tty:
        return await _prompt_yes_no(label="允许？[y/N] ")

    # ---- 标准 TTY 路径：三按钮 UX ----
    return await _prompt_three_button(config_path_hint=config_path_hint)


# ---------------------------------------------------------------------------
# 各路径的具体 UX
# ---------------------------------------------------------------------------


async def _prompt_three_button(*, config_path_hint: str) -> ApprovalAction:
    """``[y]es once / [s]ession / [p]ersist / [n]o`` 四向按钮。"""
    label = "允许？[y]=once  [s]=session  [p]=persist  [n]=no  > "
    while True:
        raw = await _read_line(label)
        answer = (raw or "").strip().lower()
        if answer in {"y", "yes", "o", "once"}:
            return ApprovalAction.ACCEPT_ONCE
        if answer in {"s", "session"}:
            return ApprovalAction.ACCEPT_FOR_SESSION
        if answer in {"p", "persist"}:
            confirmed = await _confirm_persist(config_path_hint=config_path_hint)
            if confirmed:
                return ApprovalAction.ACCEPT_PERSIST
            # 二次确认拒绝 → 降级到 session（保留用户初衷"我倾向放行"，
            # 但避免误改 yaml）。文档化此 UX 决策。
            click.echo("[approval] persist cancelled, downgraded to session grant.", err=True)
            return ApprovalAction.ACCEPT_FOR_SESSION
        if answer in {"n", "no", ""}:
            return ApprovalAction.REJECT
        click.echo("[approval] invalid input; expected y/s/p/n", err=True)


async def _confirm_persist(*, config_path_hint: str) -> bool:
    """[p]persist 二次确认。回车默认拒绝，避免误点。"""
    prompt = _PERSIST_CONFIRM_PROMPT.replace(".kongming/config.yaml", config_path_hint)
    raw = await _read_line(prompt)
    answer = (raw or "").strip().lower()
    return answer in {"y", "yes"}


async def _prompt_elevated(*, confirm_token: str | None) -> ApprovalAction:
    """elevated 路径：要求用户输入 confirm_token；不允许 [s]/[p]。

    没有 confirm_token 时退化为简单 yes/no（防止上游忘传导致永久拒绝）。
    输入 token 错误一次直接拒绝，不允许多次尝试（防爆破 / 反 LLM 反射点头）。
    """
    if not confirm_token:
        # 上游 metadata 缺 confirm_token：保守拒绝并提示
        click.echo(
            "[approval] elevated request without confirm_token; rejecting for safety.",
            err=True,
        )
        return ApprovalAction.REJECT
    label = (
        f"⚠️ ELEVATED 操作需要二次确认。请输入 confirm_token={confirm_token} 以放行 "
        f"（其他任何输入视为拒绝）: "
    )
    raw = await _read_line(label)
    if (raw or "").strip() == confirm_token:
        return ApprovalAction.ACCEPT_ONCE
    return ApprovalAction.REJECT


async def _prompt_yes_no(*, label: str) -> ApprovalAction:
    """非 TTY 兜底：仅 y/n。"""
    raw = await _read_line(label)
    answer = (raw or "").strip().lower()
    if answer in {"y", "yes"}:
        return ApprovalAction.ACCEPT_ONCE
    return ApprovalAction.REJECT


async def _prompt_cli_manager_two_choice(
    *,
    metadata: dict[str, Any],
    is_tty: bool,
) -> ApprovalAction:
    """CLI manager 专用两选项：允许一次 / 拒绝，超时按规则默认动作处理。"""
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
        return timeout.default_action

    raw = await _read_cli_manager_choice(timeout=timeout)
    if raw is None:
        return timeout.default_action
    answer = (raw or "").strip().lower()
    if answer in {"y", "yes"}:
        return ApprovalAction.ACCEPT_ONCE
    if answer == "":
        return timeout.default_action
    return ApprovalAction.REJECT


# ---------------------------------------------------------------------------
# 输入 helper
# ---------------------------------------------------------------------------


async def _read_line(prompt_text: str) -> str:
    """异步读一行（同步 readline 跑在线程池里）。

    与 :func:`host.cli_adapter._blocking_readline` 策略一致：避免与
    prompt_toolkit 的事件循环抢 TTY，同时不阻塞事件循环。
    """
    try:
        return await asyncio.to_thread(_blocking_readline, prompt_text)
    except (EOFError, KeyboardInterrupt):
        # 任何中断都视为拒绝，安全优先
        return ""


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


def _blocking_readline(prompt_text: str) -> str:
    sys.stdout.write(prompt_text)
    sys.stdout.flush()
    line = sys.stdin.readline()
    if line == "":  # EOF
        raise EOFError
    return line.rstrip("\n")


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
    remaining_seconds = max(0, (remaining_ms + 999) // 1000)
    auto_text = "自动同意" if default_action is ApprovalAction.ACCEPT_ONCE else "自动拒绝"
    enter_text = "默认同意" if default_action is ApprovalAction.ACCEPT_ONCE else "默认拒绝"
    return (
        f"允许一次？[y]=允许  [n]=拒绝  [Enter]={enter_text}  {auto_text} {remaining_seconds}s > "
    )


def _stdin_is_tty() -> bool:
    """``sys.stdin.isatty()`` 的封装；无 stdin 时按非 TTY 处理。"""
    try:
        return bool(sys.stdin.isatty())
    except (ValueError, AttributeError):
        return False


def _metadata_is_elevated(metadata: dict[str, Any]) -> bool:
    """CLI manager 摘要展示用：危险规则 / elevated 元数据都按高风险显示。"""
    return bool(
        metadata.get("policy_hint") == "elevated"
        or metadata.get("severity") == "elevated"
        or metadata.get("matched_rule")
        or metadata.get("blocked_by_rule")
        or metadata.get("auto_reject_at_ms")
        or metadata.get("autoRejectAtMs")
    )


def _resolve_cli_manager_timeout(metadata: dict[str, Any]) -> _CliManagerTimeout:
    """CLI 等待截止时间与默认动作。"""
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
    """兼容测试辅助：返回 CLI manager deadline。"""
    return _resolve_cli_manager_timeout(metadata).deadline_ms


def _has_auto_deadline(metadata: dict[str, Any]) -> bool:
    return (
        _first_int_metadata(metadata, "auto_reject_at_ms", "autoRejectAtMs") is not None
        or _first_int_metadata(metadata, "auto_approve_at_ms", "autoApproveAtMs") is not None
    )


def _first_int_metadata(metadata: dict[str, Any], *keys: str) -> int | None:
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
    """统一打印请求摘要，便于用户判断（写到 stderr）。"""
    args_preview = _format_arguments(request.arguments)
    reason = f" reason={request.reason}" if request.reason else ""
    severity = "ELEVATED" if is_elevated else "STANDARD"
    suffix = "" if is_tty else " (non-TTY)"
    click.echo(
        f"[approval/{severity}{suffix}] tool={request.tool_name} args={args_preview}{reason}",
        err=True,
    )


def _format_arguments(arguments: dict[str, object] | None) -> str:
    if not arguments:
        return "{}"
    parts = []
    for k, v in arguments.items():
        text = repr(v)
        if len(text) > 80:
            text = text[:77] + "..."
        parts.append(f"{k}={text}")
    return "{" + ", ".join(parts) + "}"


__all__ = ["build_cli_action_prompt"]
