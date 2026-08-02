"""CLI 宿主适配器。

把终端 stdin/stdout 接到统一 host bridge。本文件里同时提供：

- :class:`CLIAdapter`：实现 :class:`host.base.HostAdapter`，用
  ``prompt_toolkit`` 异步读输入、用 ``click.echo`` 输出。
- :class:`CLIEventSink`：实现 :class:`core.contracts.EventSink`，
  只在 verbose 模式下把关键节点翻成单行可读进度，不 verbose 时完全静默。

设计约束：

- 本文件只消费 ``core.contracts`` 里的协议和 ``host.base`` 的基类；
  不反向 import ``cli/``，也不 import ``safety/`` / ``infrastructure.tracing/``。
- ``prompt_approval``（v0.1.6）返回 :class:`core.contracts.ApprovalAction`
  的 3 档结构化决策（``ACCEPT_ONCE`` / ``ACCEPT_FOR_SESSION`` / ``REJECT``），
  由 ``InteractiveApproval`` 通过 ``__action_aware__`` 标记自动识别并翻成
  ``ApprovalDecision``。
  ``ACCEPT_PERSIST`` 当前未在 CLI / web 暴露（后端 dead path），等前端"永久
  同意"按钮设计稳定后再加。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import click
from typing_extensions import override

from core.contracts import ApprovalAction, ApprovalRequest, Event, ProviderUsageSnapshot
from core.result import Result
from hosts.shared.base import HostAdapter

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from prompt_toolkit import PromptSession as _PromptSession


class CLIAdapter(HostAdapter):
    """终端宿主适配器。

    - ``read_input``：用 :class:`prompt_toolkit.PromptSession` 异步读行；
      命中 EOF / Ctrl-D 时返回 ``None`` 让 bridge 干净退出。
    - ``write_output``：用 :func:`click.echo` 打印，避免 stdout buffer
      问题。
    - ``notify_event``：verbose 时写单行进度，非 verbose 时完全静默。
    - ``prompt_approval``：在终端打一行 "允许？[y=once / s=session / N=reject]"；
      返回 :class:`ApprovalAction` 三档决策；默认拒绝（裸回车 / 任意其他输入），
      避免误确认。
    """

    def __init__(
        self,
        *,
        prompt: str = "kongming > ",
        verbose: bool = False,
        pre_prompt_hook: Callable[[], Awaitable[None]] | None = None,
        streaming: bool = True,
    ) -> None:
        """初始化 CLIAdapter。

        Args:
            prompt: 输入提示符。
            verbose: 是否在 stderr 打单行进度。
            pre_prompt_hook: v0.3 cron-delivery M5——在每次 prompt **之前**
                调用,给 cron-delivery sink 等需要"在 prompt 间隙 flush"的组件用。
                hook 内部抛异常会被静默捕获(不影响主 prompt 流程)。
            streaming: 是否处于流式输出模式。``True``(默认)时
                :meth:`render_result` 跳过 final.content 的二次打印
                (``CLIStreamSink`` 已实时打字打过);``False`` 时完整打印 content
                (非流式 fallback 路径)。由 cli/main.py 根据
                ``cfg.stream.enabled`` + provider 是否实现 ``SupportsLLMStream``
                算好传入(``stream_active``),取代历史的 bridge ``echo_final_content``
                开关(决策点下沉到 adapter 内部)。默认 True 因为 CLI 主路径默认开流式
                (``cfg.stream.enabled`` 默认 True);裸构造 ``CLIAdapter()`` 的测试
                若要验证非流式 content 打印,需显式传 ``streaming=False``。
        """
        self._prompt = prompt
        self._verbose = verbose
        self._session: _PromptSession[Any] | None = None
        self._closed = False
        self._pre_prompt_hook = pre_prompt_hook
        self._streaming = streaming

    # ------------------------------------------------------------------
    # HostAdapter 实现
    # ------------------------------------------------------------------

    @override
    async def read_input(self) -> str | None:
        # v0.3 M5：pre_prompt_hook 在 prompt_async 之前调；典型场景是
        # CliDeliverySink.drain_pending → click.echo flush cron 投递 buffer。
        # hook 失败不阻塞 prompt；用户仍能输入。
        # 失败用 logger.warning 留诊断线索（避免 silent failure 让 cron 通知
        # 永远丢但用户不知道为什么 —— R2 round 1 P1-2 修复）。
        if self._pre_prompt_hook is not None:
            try:
                await self._pre_prompt_hook()
            except Exception:
                import logging

                logging.getLogger(__name__).warning(
                    "pre_prompt_hook failed; cron delivery buffer may be stuck",
                    exc_info=True,
                )

        session = self._ensure_session()
        try:
            text: str = await session.prompt_async(self._prompt)
        except EOFError:
            return None
        except KeyboardInterrupt:
            # 空行位置 Ctrl-C：当一次"取消本次输入"处理，返回空串，
            # 由 CLIInteractiveLoop 的 strip() 过滤掉；若用户反复 Ctrl-C，
            # run_loop 在更外层捕获并调用 HostDispatcher interrupt。
            return ""
        return text

    @override
    async def write_output(self, text: str) -> None:
        # click.echo 会处理 encoding & flush，比裸 print 稳。
        click.echo(text)

    @override
    async def render_result(self, result: Result) -> None:
        """把一次 run 的最终结果渲染到终端(接管原 bridge 的 _write_runtime_result)。

        三段渲染:

        1. **final content**:``streaming=False`` 时打(``streaming=True`` 已由
           ``CLIStreamSink`` 实时打字打过,跳过避免视觉双重输出)。
        2. **error 行**:``[error] {type}: {msg}``——result.error 非空时打
           (无论 streaming 与否,error 不走流式通道)。
        3. **token 汇总行**:``[tokens ↑X ↓Y =Z]``——metadata.usage 非空时打。
           token 是 run 收尾的汇总信息,流式期没打过,所以 streaming 不门控它
           (与 Web 不同——Web 有自己的 ``UsageFrame`` 推 StatusLine,在
           ``WebHostAdapter.render_result`` 里跳过)。

        决策点全在 adapter 内部:bridge 不再知道这些格式化字符串。
        """
        final = result.final_message
        if not self._streaming and final is not None and final.content:
            click.echo(final.content)
        if result.error is not None:
            click.echo(f"[error] {type(result.error).__name__}: {result.error.message}")
        usage = result.metadata.get("usage")
        if isinstance(usage, dict) and usage:
            try:
                snapshot = ProviderUsageSnapshot.from_payload(usage)
            except ValueError:
                return
            prompt_t = _render_token_metric(snapshot.input_total_tokens.value)
            completion_t = _render_token_metric(snapshot.output_total_tokens.value)
            total_t = _render_token_metric(snapshot.total_tokens.value)
            click.echo(f"[tokens ↑{prompt_t} ↓{completion_t} ={total_t}]")

    @override
    async def notify_event(self, event: Event) -> None:
        if not self._verbose:
            return
        line = _render_event_line(event)
        if line is not None:
            click.echo(line, err=True)

    @override
    async def prompt_approval(  # type: ignore[override]
        self, request: ApprovalRequest
    ) -> ApprovalAction:
        """v0.1.6 三档 CLI 审批。

        输入映射：

        - ``y`` / ``yes`` → :attr:`ApprovalAction.ACCEPT_ONCE`（仅本次）
        - ``s`` / ``session`` → :attr:`ApprovalAction.ACCEPT_FOR_SESSION`
          （当前 thread 内写入 permissions，后续同类调用按规则判定）
        - 其他（含空回车 / ``n`` / ``no`` / EOF / Ctrl-C / 任意其他字符）→
          :attr:`ApprovalAction.REJECT`

        默认 reject 而非 once，是安全约定：误按回车不应该意外放行。

        ``ACCEPT_PERSIST`` 当前未提供 CLI 入口；后端写盘 wiring 也未接通
        （统一规则持久化由 `PermissionsManager` 负责），等前端“允许并记住”
        按钮设计稳定后再统一打开。
        """
        args_preview = _format_arguments(request.arguments)
        reason = f" reason={request.reason}" if request.reason else ""
        click.echo(
            f"命中审批：tool={request.tool_name} args={args_preview}{reason}",
            err=True,
        )
        # 审批本质是同步阻塞交互；用 asyncio.to_thread 把同步 readline
        # 挪到子线程，避免阻塞事件循环，同时也不和 prompt_toolkit 的事件
        # 循环抢 TTY。
        try:
            raw = await asyncio.to_thread(
                _blocking_readline,
                "允许？[y=once / s=session / N=reject] ",
            )
        except (EOFError, KeyboardInterrupt):
            # 审批被中断等同于拒绝。
            return ApprovalAction.REJECT
        answer = (raw or "").strip().lower()
        if answer in {"y", "yes"}:
            return ApprovalAction.ACCEPT_ONCE
        if answer in {"s", "session"}:
            return ApprovalAction.ACCEPT_FOR_SESSION
        return ApprovalAction.REJECT

    # 标记 prompt_approval 为 action-aware（返回 ApprovalAction 而非 bool）。
    # 直接在类外用属性赋值替代 @mark_action_aware 装饰器：装饰器签名是
    # ``Callable[[ApprovalRequest], Awaitable[ApprovalAction]]``，与方法的
    # ``Callable[[Self, ApprovalRequest], ...]`` 不兼容，会触发 mypy arg-type
    # 错误。属性赋值在运行时和装饰器等价，绕过 mypy 静态检查。
    # 与 ``web.app_support.host_adapter.WebHostAdapter`` 同模式。
    prompt_approval.__action_aware__ = True  # type: ignore[attr-defined]

    @override
    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # prompt_toolkit 的 PromptSession 不持有需要显式关闭的资源，
        # 但保留钩子，方便未来切到需要关闭 app 的 UI 框架。
        self._session = None

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _ensure_session(self) -> _PromptSession[Any]:
        if self._session is None:
            # 延迟 import：prompt_toolkit 是重依赖，只在真的进入交互时加载。
            from prompt_toolkit import PromptSession

            self._session = PromptSession()
        return self._session


class CLIEventSink:
    """CLI 的 :class:`core.contracts.EventSink` 实现。

    verbose 模式下把 runtime 关键节点翻成单行进度；非 verbose 时完全静默，
    保持终端输出干净。show_reasoning 独立于 verbose：即使非 verbose，只要开启
    show_reasoning 就会在每轮响应后打印模型思考内容（stdout，非 stderr）。

    结构上满足 :class:`core.contracts.EventSink` Protocol（单个 async
    ``emit`` 方法）。
    """

    def __init__(self, *, verbose: bool = False, show_reasoning: bool = False) -> None:
        self._verbose = verbose
        self._show_reasoning = show_reasoning

    async def emit(self, event: Event) -> None:
        if self._show_reasoning and event.kind == "llm.response":
            _render_reasoning(event)
        if not self._verbose:
            return
        line = _render_event_line(event)
        if line is not None:
            click.echo(line, err=True)


# ---------------------------------------------------------------------------
# 渲染 helper
# ---------------------------------------------------------------------------


def _render_event_line(event: Event) -> str | None:
    """把一个 :class:`core.contracts.Event` 翻成一行终端可读进度。

    只翻译 v1-mini 直接关心的几种 kind，其他返回 ``None`` 由调用方决定
    是否落默认输出（这里默认完全吞掉，保持终端清爽）。
    """
    kind = event.kind
    turn = event.turn
    payload = event.payload or {}
    if kind == "turn.start":
        return f"[turn.start] turn={turn}"
    if kind == "turn.end":
        return f"[turn.end] turn={turn}"
    if kind == "tool.call.start":
        return (
            f"[tool.call.start] turn={turn} "
            f"tool={payload.get('tool_name')} call_id={payload.get('call_id')}"
        )
    if kind == "tool.call.end":
        ok = payload.get("ok")
        reason = payload.get("reason")
        suffix = f" reason={reason}" if reason else ""
        return f"[tool.call.end] turn={turn} tool={payload.get('tool_name')} ok={ok}{suffix}"
    if kind == "approval.request":
        return f"[approval.request] turn={turn} tool={payload.get('tool_name')}"
    if kind == "approval.decision":
        return (
            f"[approval.decision] turn={turn} "
            f"tool={payload.get('tool_name')} outcome={payload.get('outcome')}"
        )
    if kind == "error":
        return f"[error] turn={turn} type={payload.get('type')} msg={payload.get('message')}"
    # safety v0.1.4 DecisionTrace（M8.5）：3 个新事件 kind 在 verbose 模式下
    # 各打一行可读进度，方便人工跟踪决策路径。
    if kind == "tool.denied":
        rule = payload.get("matched_rule") or "unknown"
        reason = payload.get("reason") or ""
        return (
            f"[hard_block:{rule}] x {payload.get('tool_name')} "
            f"{_format_path_or_command(payload)} - REJECTED ({reason})"
        )
    if kind == "tool.approval_required":
        severity = payload.get("decision_source") or "standard"
        return f"[{severity}] ? {payload.get('tool_name')} {_format_path_or_command(payload)}"
    if kind == "tool.silently_allowed":
        source = payload.get("decision_source") or "intrinsic"
        return (
            f"[silent_allow:{source}] v {payload.get('tool_name')} "
            f"{_format_path_or_command(payload)}"
        )
    return None


def _format_path_or_command(payload: dict[str, object]) -> str:
    """从 trace event payload 里提取并截断 path_or_command。"""
    raw = payload.get("path_or_command")
    if not isinstance(raw, str):
        return ""
    if len(raw) > 80:
        return raw[:77] + "..."
    return raw


def _render_reasoning(event: Event) -> None:
    """把 llm.response 事件里的 reasoning_content 打印到 stdout。

    只在 provider_metadata 含非空 reasoning_content 时输出，否则静默。
    """
    payload = event.payload or {}
    meta = payload.get("provider_metadata") or {}
    content = meta.get("reasoning_content")
    if not content:
        return
    total_len = meta.get("reasoning_content_length", len(content))
    truncated = total_len > len(content)
    suffix = f" …（共 {total_len} 字符，已截断）" if truncated else ""
    click.echo(f"\n[thinking turn={event.turn}]{suffix}\n{content}\n")


def _format_arguments(arguments: dict[str, object]) -> str:
    """把工具参数截断后渲染成一行可读字符串，避免刷屏。"""
    if not arguments:
        return "{}"
    preview = []
    for key, value in arguments.items():
        text = repr(value)
        if len(text) > 80:
            text = text[:77] + "..."
        preview.append(f"{key}={text}")
    return "{" + ", ".join(preview) + "}"


def _render_token_metric(value: int | None) -> str:
    """渲染 canonical token 指标，输入为可选数值，输出为数字或未知占位。"""
    return str(value) if value is not None else "?"


def _blocking_readline(prompt_text: str) -> str:
    """同步读一行（审批等阻塞人机交互点专用）。

    独立出来是为了：
    - 不和 :mod:`prompt_toolkit` 的异步事件循环打架；
    - 让测试可以通过 monkeypatch 换掉这个点。
    """
    sys.stdout.write(prompt_text)
    sys.stdout.flush()
    return sys.stdin.readline().rstrip("\n")


__all__ = ["CLIAdapter", "CLIEventSink"]
