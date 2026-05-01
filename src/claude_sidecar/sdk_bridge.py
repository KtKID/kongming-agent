"""SDK Bridge —— ``claude_agent_sdk.ClaudeSDKClient`` 封装。

实现 :class:`claude_sidecar.runtime.SdkBridge` Protocol，是 sidecar 跟
Claude SDK 之间的"接线员"。**只负责把电话接通**，不负责翻译 SDK 输出
（那是 :mod:`claude_sidecar.translator` 的事）。

核心约束（[E9 决议](../../plugins/client/XSpace/docs/claude-code-runtime/discussion-kongming-sidecar-contract.md)）：

- ``include_partial_messages=True`` **硬开**，不暴露给调用方关闭
- 多 run 复用同一 ``ClaudeSDKClient``：第二次 ``handle_start`` 不重新建 client，
  直接 ``client.query(prompt=...)`` 发新消息
- ``contextPackPath`` 用 ``SystemPromptFile{type:'file', path:...}`` 注入，sidecar 不读文件正文

``continuePointSnapshot`` 处理（方案 D，见 plan.md）：

- 有 ``contextPackPath`` 时：``system_prompt`` = ``SystemPromptFile``，
  ``continue_point_snapshot`` 拼到首条 ``query()`` 的 prompt 前缀
- 没 ``contextPackPath`` 时：``system_prompt`` = ``SystemPromptPreset(append=continue_point_snapshot)``
- 都没有：``system_prompt`` = ``None``（SDK 默认）

interrupt / reconnect 在本 task 留 ``NotImplementedError``，由 #5 lifecycle task 实现。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    CLINotFoundError,
    ProcessError,
)

from claude_sidecar.runtime import CanUseToolCallback, MessageTranslator, RunContext

if TYPE_CHECKING:
    from claude_sidecar.output import OutputWriter
    from claude_sidecar.protocol import ReconnectRequest, StartRequest


ClientFactory = Callable[[ClaudeAgentOptions], ClaudeSDKClient]


class ClaudeSdkBridge:
    """``claude_agent_sdk.ClaudeSDKClient`` 的 sidecar 侧封装。

    实现 :class:`claude_sidecar.runtime.SdkBridge` Protocol。
    通过 ``client_factory`` 让单测注入 mock，避免真发 SDK 请求。

    Args:
        translator: SDK message → SidecarEvent 翻译器，由 #3 task 提供具体实现
        writer: SDK 启动失败 / pump 异常时 emit ``claude_transport_error``
        client_factory: 默认 ``ClaudeSDKClient``；测试用 mock 替换
    """

    def __init__(
        self,
        translator: MessageTranslator,
        writer: OutputWriter,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._translator = translator
        self._writer = writer
        self._client_factory: ClientFactory = client_factory or ClaudeSDKClient
        self._can_use_tool: CanUseToolCallback | None = None
        self._client: ClaudeSDKClient | None = None
        self._message_pump_task: asyncio.Task[None] | None = None
        self._current_context: RunContext | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # SdkBridge Protocol
    # ------------------------------------------------------------------

    async def handle_start(self, request: StartRequest) -> None:
        """处理 ``start`` 命令：首次建 client，二次复用。"""
        async with self._lock:
            context = RunContext(
                workspace_id=request.workspace_id,
                thread_id=request.thread_id,
                run_id=request.run_id,
                runtime_session_id=None,  # SDK init 后由 translator 回填
            )

            if self._client is None:
                # 首次：建 client + connect + 起 pump
                try:
                    options = self._options_from_request(request)
                    self._client = self._client_factory(options)
                    await self._client.connect()
                except (CLINotFoundError, CLIConnectionError, ProcessError) as exc:
                    # 首次启动失败 → run 未建立 → transport_error 不带 runId
                    self._client = None
                    await self._writer.emit_transport_error(
                        f"sdk start failed: {exc!r}",
                        recoverable=False,
                        thread_id=request.thread_id,
                    )
                    return
                self._current_context = context
                self._message_pump_task = asyncio.create_task(self._pump_messages())
            else:
                # 二次：复用 client，重置 translator 状态
                self._translator.reset_for_new_run(context)
                self._current_context = context

            # 拼 prompt（continue_point_snapshot 在有 contextPackPath 时拼到 user prompt 前缀）
            prompt = self._build_prompt(request)
            try:
                await self._client.query(prompt=prompt)
            except (CLIConnectionError, ProcessError) as exc:
                # query() 失败：发 transport_error 但不强行重置 client
                # 二次失败时已有 run，runId 填上
                await self._writer.emit_transport_error(
                    f"sdk query failed: {exc!r}",
                    recoverable=False,
                    thread_id=request.thread_id,
                    run_id=request.run_id,
                )

    def set_can_use_tool(self, callback: CanUseToolCallback | None) -> None:
        """注入 #4 task 提供的 can_use_tool 回调。

        必须在首次 ``handle_start`` 之前调用——client 一旦建好，options 已固化，
        本 task 不实现运行时切换。在 client 已建后再调用此函数 = no-op
        （不抛错，但 callback 不会生效）。
        """
        self._can_use_tool = callback

    async def interrupt(self) -> None:
        """中断当前 run。由 #5 lifecycle task 实现。"""
        raise NotImplementedError("interrupt is implemented by claude-sidecar-lifecycle (#5)")

    async def reconnect(self, request: ReconnectRequest) -> None:
        """续接 SDK 会话。由 #5 lifecycle task 实现。"""
        raise NotImplementedError("reconnect is implemented by claude-sidecar-lifecycle (#5)")

    async def shutdown(self) -> None:
        """关闭 SDK 客户端 + 取消 pump 任务，幂等。"""
        if self._message_pump_task is not None:
            self._message_pump_task.cancel()
            # cancel 后 await 会抛 CancelledError；pump 内部异常也忽略
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._message_pump_task
            self._message_pump_task = None

        if self._client is not None:
            # disconnect 失败不阻塞 shutdown 流程；进程级 exit 兜底释放
            with contextlib.suppress(Exception):
                await self._client.disconnect()
            self._client = None

        self._current_context = None

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _options_from_request(self, request: StartRequest) -> ClaudeAgentOptions:
        """把 ``StartRequest`` 字段映射到 ``ClaudeAgentOptions``。

        强制开启 ``include_partial_messages=True``。
        ``can_use_tool`` 用 ``self._can_use_tool``（如果已注入）。
        """
        kwargs: dict[str, Any] = {
            "include_partial_messages": True,  # 硬约束 [E9 决议]
        }
        if request.model is not None:
            kwargs["model"] = request.model
        if request.cwd is not None:
            kwargs["cwd"] = request.cwd
        if request.permission_mode is not None:
            kwargs["permission_mode"] = request.permission_mode
        if request.allowed_tools is not None:
            kwargs["allowed_tools"] = list(request.allowed_tools)
        if request.disallowed_tools is not None:
            kwargs["disallowed_tools"] = list(request.disallowed_tools)
        if request.resume is not None:
            kwargs["resume"] = request.resume
        if self._can_use_tool is not None:
            kwargs["can_use_tool"] = self._can_use_tool

        # system_prompt：方案 D（contextPackPath 优先 → SystemPromptFile；
        # 否则 continue_point_snapshot → SystemPromptPreset.append）
        system_prompt = self._build_system_prompt(request)
        if system_prompt is not None:
            kwargs["system_prompt"] = system_prompt

        return ClaudeAgentOptions(**kwargs)

    @staticmethod
    def _build_system_prompt(request: StartRequest) -> Any | None:
        """构造 ``ClaudeAgentOptions.system_prompt``。

        - 有 ``contextPackPath``：``SystemPromptFile{type:'file', path:...}``
          （``continue_point_snapshot`` 走 user prompt 前缀，不进 system_prompt）
        - 仅 ``continue_point_snapshot``：``SystemPromptPreset{type:'preset',
          preset:'claude_code', append:...}``
        - 都没有：返回 None
        """
        if request.context_pack_path is not None:
            return {
                "type": "file",
                "path": request.context_pack_path,
            }
        if request.continue_point_snapshot is not None:
            return {
                "type": "preset",
                "preset": "claude_code",
                "append": request.continue_point_snapshot,
            }
        return None

    @staticmethod
    def _build_prompt(request: StartRequest) -> str:
        """构造首条 query prompt。

        当 ``contextPackPath`` 跟 ``continue_point_snapshot`` 同时存在时，
        snapshot 走 user prompt 前缀（避免 SystemPromptFile 跟 SystemPromptPreset
        互斥的限制）。
        """
        if request.context_pack_path is not None and request.continue_point_snapshot is not None:
            return f"<continue>\n{request.continue_point_snapshot}\n</continue>\n\n{request.prompt}"
        return request.prompt

    async def _pump_messages(self) -> None:
        """后台任务：消费 SDK message 流，喂给 translator。

        并发约束：本 task 假设 XSpace 串行发 ``start``（一次 run 完才发下一个），
        ``self._current_context`` 在 ``handle_start`` 切换时 pump 拿到的就是新值。
        真正并发场景由 #5 lifecycle task 评估。
        """
        assert self._client is not None, "pump started without client"
        try:
            async for msg in self._client.receive_messages():
                ctx = self._current_context
                if ctx is None:
                    # 防御：不应发生（context 在建 client 前已设置）
                    continue
                await self._translator.handle(msg, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # SDK 流出错：emit transport_error，但不主动重置 client
            # （上游决定 reconnect / shutdown）
            ctx = self._current_context
            await self._writer.emit_transport_error(
                f"sdk message stream error: {exc!r}",
                recoverable=False,
                thread_id=ctx.thread_id if ctx else None,
                run_id=ctx.run_id if ctx else None,
            )
