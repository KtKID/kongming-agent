"""sidecar 主循环。

职责：

1. 启动后立即输出 ``claude_sidecar_ready`` 事件（首条 stdout）
2. 进入 stdin readline 循环，每行交给 :func:`parse_request` 解析
3. 命令路由：
   - ``start`` → :class:`~claude_sidecar.sdk_bridge.ClaudeSdkBridge.handle_start`
   - ``shutdown`` → ``bridge.shutdown()`` 后干净退出
   - ``interrupt`` / ``reconnect`` / ``resolve_approval`` → 暂回 ``not_implemented``
     （由 #4 / #5 task 接管）
4. ``shutdown`` / stdin EOF / SIGTERM 干净退出（退出码 0）
5. 未捕获异常兜底：emit ``claude_transport_error(recoverable=false)`` 后退出码 1

stdin 读用 ``asyncio.to_thread(stdin.readline)``——简单、跨平台、不依赖
``loop.connect_read_pipe`` 的 fd 操作。

工厂注入设计：
  ``run_sidecar`` 接收 ``sdk_bridge_factory`` / ``translator_factory`` 两个可选参数，
  默认 lazy import 真实实现（``ClaudeSdkBridge`` / ``ClaudeMessageTranslator``）。
  如果 :mod:`claude_sidecar.translator` 还没合并（#3 task 未 ready），
  start 命令自动 fallback 到 ``not_implemented`` 旧路径。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, TextIO

from claude_sidecar.output import OutputWriter
from claude_sidecar.protocol import (
    ParseError,
    ResolveApprovalRequest,
    ShutdownRequest,
    StartRequest,
    parse_request,
)

if TYPE_CHECKING:
    from claude_sidecar.runtime import MessageTranslator, SdkBridge


SdkBridgeFactory = Callable[["MessageTranslator", OutputWriter], "SdkBridge"]
TranslatorFactory = Callable[[OutputWriter], "MessageTranslator"]


class _GracefulExit(Exception):
    """内部信号：从主循环的处理函数里请求干净退出（不是错误）。"""


async def run_sidecar(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    *,
    sdk_bridge_factory: SdkBridgeFactory | None = None,
    translator_factory: TranslatorFactory | None = None,
) -> int:
    """sidecar 进程主循环。

    Args:
        stdin: 输入流，默认 ``sys.stdin``。测试时可注入 ``io.StringIO``。
        stdout: 输出流，默认 ``sys.stdout``。
        sdk_bridge_factory: 默认 ``ClaudeSdkBridge``；测试可注入 mock。
        translator_factory: 默认 ``ClaudeMessageTranslator``（由 #3 task 提供）；
            测试可注入 mock。如果 import 失败（#3 未 ready），start 命令走
            ``not_implemented`` fallback。

    Returns:
        进程退出码。0 表示干净退出（shutdown / EOF），1 表示未捕获异常兜底。
    """
    actual_stdin = stdin if stdin is not None else sys.stdin
    writer = OutputWriter(stdout)

    # 首条事件：ready。所有 import 已在文件顶部完成，此处发 ready 满足契约 §7.2。
    await writer.emit_ready()

    # 装配 sdk_bridge：lazy import + 防御性 fallback
    bridge = _try_build_bridge(writer, sdk_bridge_factory, translator_factory)

    try:
        try:
            while True:
                line = await asyncio.to_thread(actual_stdin.readline)
                if line == "":
                    # EOF == shutdown == 退出码 0
                    return 0
                stripped = line.strip()
                if not stripped:
                    # 空行容忍（不报错也不响应）
                    continue
                try:
                    await _handle_line(stripped, writer, bridge)
                except _GracefulExit:
                    return 0
        except (KeyboardInterrupt, SystemExit):
            # 让 SIGINT / SIGTERM 正常传播（不当作业务异常）
            raise
        except Exception as exc:
            # 未捕获异常兜底：先告诉 XSpace 我崩了，再退出
            await writer.emit_transport_error(
                f"sidecar internal error: {exc!r}",
                recoverable=False,
            )
            return 1
    finally:
        # 进程退出前关闭 SDK client（如果建了）
        if bridge is not None:
            with contextlib.suppress(Exception):
                await bridge.shutdown()


def _try_build_bridge(
    writer: OutputWriter,
    sdk_bridge_factory: SdkBridgeFactory | None,
    translator_factory: TranslatorFactory | None,
) -> SdkBridge | None:
    """尝试装配 translator + sdk_bridge。

    任一缺失（特别是 #3 task 还没合并 ``translator.py``）就返回 None，
    main 循环里 ``start`` 命令走 ``not_implemented`` fallback。

    Returns:
        装配好的 :class:`SdkBridge` 实例，或 None（fallback 模式）。
    """
    # 装配 translator
    actual_translator_factory = translator_factory
    if actual_translator_factory is None:
        try:
            # lazy import：translator.py 可能还没合并
            from claude_sidecar.translator import ClaudeMessageTranslator
        except (ImportError, AttributeError):
            return None  # #3 task 还没合并

        def actual_translator_factory(w: OutputWriter) -> MessageTranslator:
            return ClaudeMessageTranslator(w)

    try:
        translator = actual_translator_factory(writer)
    except Exception:
        return None

    # 装配 sdk_bridge
    actual_sdk_bridge_factory = sdk_bridge_factory
    if actual_sdk_bridge_factory is None:
        from claude_sidecar.sdk_bridge import ClaudeSdkBridge

        def actual_sdk_bridge_factory(t: MessageTranslator, w: OutputWriter) -> SdkBridge:
            return ClaudeSdkBridge(t, w)

    try:
        return actual_sdk_bridge_factory(translator, writer)
    except Exception:
        return None


async def _handle_line(
    line: str,
    writer: OutputWriter,
    bridge: SdkBridge | None,
) -> None:
    """处理单行 stdin 输入。"""
    parsed = parse_request(line)

    if isinstance(parsed, ParseError):
        # 启动期/run 未建立时的解析失败 → 不带 runId
        await writer.emit_transport_error(
            f"parse error ({parsed.reason}): {parsed.detail}",
            recoverable=False,
        )
        return

    request = parsed.request

    # shutdown：先关 SDK client，再 emit ack，最后抛 _GracefulExit
    if isinstance(request, ShutdownRequest):
        if bridge is not None:
            with contextlib.suppress(Exception):
                await bridge.shutdown()
        await writer.emit_transport_error(
            "shutdown received",
            recoverable=False,
            thread_id=request.thread_id,
        )
        raise _GracefulExit()

    # start：路由到 sdk_bridge；fallback 时回 not_implemented
    if isinstance(request, StartRequest):
        if bridge is not None:
            await bridge.handle_start(request)
            return
        # fallback：translator 还没 ready
        await writer.emit_transport_error(
            "command 'start' not implemented (translator unavailable)",
            recoverable=True,
            thread_id=request.thread_id,
            run_id=request.run_id,
        )
        return

    # interrupt / reconnect / resolve_approval：未实现，等 #4 / #5 task
    thread_id = getattr(request, "thread_id", None)
    run_id = getattr(request, "run_id", None)
    # ResolveApprovalRequest 没有 run_id 字段（runId 在 approval_requested 事件里关联）
    if isinstance(request, ResolveApprovalRequest):
        run_id = None

    await writer.emit_transport_error(
        f"command '{request.type}' not implemented yet",
        recoverable=True,
        thread_id=thread_id,
        run_id=run_id,
    )
