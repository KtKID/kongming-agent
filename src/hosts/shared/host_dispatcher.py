"""宿主侧投递门户 + root agent 树运行时资源生命周期 owner。

本模块是 CLI 和 Web 共用的**唯一投递入口**：宿主只传 ``text + SubmitMode``，
由 ``HostDispatcher`` 统一投递 mailbox 并管理 agent_loop / future FIFO / queue task
等运行时资源。它不是 agent（agent cell 在 ``AgentManager`` 里），也不存历史
（历史在 ``SessionEngine`` / ``sessions``）。

关键流程：
1. ``submit`` 按 mode 分流：QUEUE 入 future FIFO + AgentManager.submit；IMMEDIATE 走 runtime.steer。
2. ``ensure_started`` 首次调用时懒创建 ``AgentManager`` 并 ``boot_root``。
3. ``HostDispatcherDeliverSink`` 在 agent_loop 完成时回填 FIFO 队头 future。
4. ``aclose`` 负责 drain 或 interrupt 后收口 agent_loop 与排队任务。

关键函数：
- ``_callable_accepts_keyword``：判断 runtime.run 是否支持某个可选关键字。
- ``HostDispatcher.submit``：宿主侧统一投递入口（QUEUE 阻塞 / IMMEDIATE 插队）。
- ``HostDispatcher.interrupt``：打断当前 root agent 树。
- ``HostDispatcher.aclose``：关闭运行时资源（manager 实例 / agent_loop / futures / queue tasks）。
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from typing_extensions import override

from application.agents.cell import AgentCell
from application.agents.loop import DeliverSink, MailRunBridge
from application.agents.manager import (
    AgentManager,
    ChildDeliverSink,
    SpawnContext,
    SpawnResult,
    SubmitMode,
)
from application.agents.registry import (
    TaskProjection,
    TaskRegistrationContext,
    TaskRegistry,
)
from application.agents.subagent_tools import SpawnAgentRequest
from application.scheduled_runs.manager import (
    ScheduledRunDispatcher,
    ScheduledRunDispatcherFactory,
)
from core.agent_spec import AgentSpec
from core.contracts import SteerRequest
from core.mail import Mail
from core.outcome import Disposition
from core.result import Result
from runtime_assembly.session_engine import SessionEngine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubmitReceipt:
    """宿主投递回执。

    输入由 ``HostDispatcher.submit`` 生成；输出给调用方判断本轮投递是否被合并进
    当前活跃 run（IMMEDIATE 命中 steer）。``merged=True`` 时不会产生独立 Result，
    调用方不应再等待 future。
    """

    merged: bool


def _callable_accepts_keyword(fn: Callable[..., Any], keyword: str) -> bool:
    """判断 callable 是否接收指定关键字。

    输入为待检查 callable 和关键字名称；输出为 bool。runtime fake 常缺少完整签名，
    无法读取签名时按可接收处理，保持测试替身兼容。
    """
    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    if keyword in parameters:
        return True
    return any(param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values())


class HostDispatcherDeliverSink(DeliverSink):
    """root agent 的 Result FIFO 回填 sink。

    输入为 HostDispatcher 持有的 future 队列；输出行为是 agent_loop 完成一条
    root mail 时 resolve 队头 future。该类只处理同步 Result 契约胶水，不参与 UI
    渲染。
    """

    def __init__(self, result_futures: collections.deque[asyncio.Future[Result]]) -> None:
        """初始化 sink。

        输入为 Result future FIFO；输出为可交给 ``AgentManager.boot_root`` 的
        ``DeliverSink`` 实例。
        """
        self._result_futures = result_futures

    @override
    def deliver_up_or_ui(
        self,
        cell: AgentCell,
        disposition: Disposition,
        *,
        result: Result,
        run_epoch: int,
    ) -> None:
        """处理正常上投结果。

        输入为 agent_loop 完成的 Result；输出为空，副作用是 resolve 队头 future。
        """
        del cell, disposition, run_epoch
        self._resolve(result)

    @override
    def emit_only(
        self,
        cell: AgentCell,
        disposition: Disposition,
        *,
        result: Result,
        run_epoch: int,
    ) -> None:
        """处理仅发事件的结果。

        输入为 agent_loop 完成的 Result；输出为空，副作用是 resolve 队头 future。
        """
        del cell, disposition, run_epoch
        self._resolve(result)

    def _resolve(self, result: Result) -> None:
        """回填 FIFO 队头 future。

        输入为 Result；输出为空。空队列只记录 warning，避免 deliver 兜底路径影响
        agent_loop 稳定性。
        """
        if not self._result_futures:
            logger.warning(
                "root session deliver on empty future queue (status=%s); dropping result",
                result.status,
            )
            return
        future = self._result_futures.popleft()
        if not future.done():
            future.set_result(result)


class HostDispatcher:
    """root agent 生命周期 owner。

    输入为 runtime 和 session_id；输出为可运行文本输入、可中断、可关闭、可暴露当前
    AgentManager 的会话对象。它不处理命令解析，也不直接读写 UI。
    """

    def __init__(
        self,
        *,
        runtime: SessionEngine,
        session_id: str,
        thread_id: str | None = None,
        root_run_bridge: MailRunBridge | None = None,
        task_registration_context: TaskRegistrationContext | None = None,
        queued_result_handler: Callable[[Result], Awaitable[None]] | None = None,
        agent_tree_runtime_router: Any | None = None,
        approval_canceller: Callable[[str], int] | None = None,
    ) -> None:
        """初始化 root agent session。

        输入为已装配 runtime、session id、可选结果处理器和 agent-tree runtime router；
        输出为懒启动状态的 root session。结果处理器用于排队文本结果的宿主兜底
        渲染；runtime router 用于 Web/CLI 工具运行期按 session 找回同一棵 agent tree。
        """
        if not session_id:
            raise ValueError("session_id must not be empty")
        self._runtime = runtime
        self._session_id = session_id
        self._thread_id = thread_id or session_id
        self._root_run_bridge = root_run_bridge
        self._task_registration_context = task_registration_context
        self._queued_result_handler = queued_result_handler
        self._agent_tree_runtime_router = agent_tree_runtime_router
        self._approval_canceller = approval_canceller
        self._agent_manager: AgentManager | None = None
        self._result_futures: collections.deque[asyncio.Future[Result]] = collections.deque()
        self._queue_tasks: set[asyncio.Task[None]] = set()

    @property
    def runtime(self) -> SessionEngine:
        """返回当前 runtime。

        输入为空；输出为构造时注入的 runtime。
        """
        return self._runtime

    @property
    def session_id(self) -> str:
        """返回 session id。

        输入为空；输出为构造时注入的 session id。
        """
        return self._session_id

    @property
    def agent_manager(self) -> AgentManager | None:
        """返回当前 AgentManager。

        输入为空；输出为已懒启动的 manager 或 None。
        """
        return self._agent_manager

    def _require_agent_manager(self) -> AgentManager:
        """读取当前 AgentManager。

        输入为空；输出为当前 manager。尚未懒启动时抛 RuntimeError，供 spawn 工具得到
        清晰失败原因。
        """
        if self._agent_manager is None:
            raise RuntimeError("AgentManager is not booted for this root session")
        return self._agent_manager

    def get_agent(self, agent_id: str) -> AgentCell | None:
        """转发 AgentManager.get_agent。

        输入为 agent_id；输出为 AgentCell 或 None。
        """
        return self._require_agent_manager().get_agent(agent_id)

    def spawn(self, request: SpawnAgentRequest) -> SpawnResult:
        """转发 AgentManager.spawn。

        输入为 SpawnAgentRequest；输出为 SpawnResult。
        """
        return self._require_agent_manager().spawn(request)

    def list_task_records(
        self,
        *,
        include_finished: bool = False,
        limit: int = 50,
    ) -> tuple[TaskProjection, ...]:
        """查询 root thread 的任务投影，输入为过滤条件，输出为 AgentManager 快照。"""
        return self._require_agent_manager().list_task_records(
            self._thread_id,
            include_finished=include_finished,
            limit=limit,
        )

    async def submit(
        self,
        text: str,
        *,
        mode: SubmitMode = SubmitMode.QUEUE,
        attachments: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        steer_request: SteerRequest | None = None,
    ) -> SubmitReceipt:
        """宿主侧统一投递入口。

        输入为用户文本、投递模式和可选结构化上下文；输出为 SubmitReceipt，标记本次
        投递落到 QUEUE 还是 IMMEDIATE 路径。两个宿主（CLI/Web）只通过本方法投递 mailbox，
        不再有第二套 future FIFO 或 AgentManager 装配。

        分流语义：
        - ``QUEUE``：阻塞到本轮 run 完成，等 ``HostDispatcherDeliverSink`` 回填 future，
          并调用构造时注入的 ``queued_result_handler``。
        - ``IMMEDIATE``：探测 ``runtime.steer``，命中活跃 run 返回 merged=True，
          未命中返回 merged=False。QUEUE fallback 由宿主调用方显式发起。

        ``steer_request`` 仅 IMMEDIATE 模式生效：调用方（Web send-now）传完整的
        :class:`SteerRequest`（含 ``pending_input_id`` 消账主键），透传到 Runner 的
        steer buffer；未传时用 ``text`` 构造仅含文本的 request（CLI 等不消账路径）。

        ``SubmitReceipt.merged`` 为 True 表示 IMMEDIATE 命中活跃 run；False 表示走了
        QUEUE 或 IMMEDIATE 未命中。
        """
        if mode is SubmitMode.IMMEDIATE:
            request = steer_request if steer_request is not None else SteerRequest(text=text)
            return SubmitReceipt(merged=self._try_send_now(request))
        queue_metadata = dict(metadata or {})
        if attachments is not None:
            queue_metadata["attachments"] = attachments
        if references is not None:
            queue_metadata["references"] = references
        result = await self.run_text(
            text,
            attachments=attachments,
            references=references,
            metadata=queue_metadata or None,
        )
        if self._queued_result_handler is not None:
            await self._queued_result_handler(result)
        return SubmitReceipt(merged=False)

    async def run_text(
        self,
        user_input: str,
        *,
        reasoning_effort: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        repost_undelivered: bool = True,
    ) -> Result:
        """执行一次 root 文本输入。

        输入为用户文本和可选结构化上下文；输出为 runner 收口后的 Result。可选参数保留
        当前 root mailbox 通过 Message metadata 与 runtime 上下文传递主要信息。
        repost_undelivered 控制是否由 HostDispatcher 直接回投收尾残留输入；Web
        pending queue 会关闭该开关，把残留交回 ThreadManager 统一广播和 drain。
        """
        del reasoning_effort, attachments, references
        await self.ensure_started()
        assert self._agent_manager is not None
        loop = asyncio.get_event_loop()
        future: asyncio.Future[Result] = loop.create_future()
        self._result_futures.append(future)
        self._agent_manager.submit(user_input, mode=SubmitMode.QUEUE, metadata=metadata)
        result = await future
        if repost_undelivered:
            self._repost_undelivered(result)
        return result

    def _try_send_now(self, request: SteerRequest) -> bool:
        """显式立即发送一条 steer 请求。

        输入为 :class:`SteerRequest`（含 text + pending_input_id）；输出为 True 表示
        当前活跃 run 接收了插入，False 表示宿主调用方应显式走 ``submit(QUEUE)``。
        这里直接探测 runtime 能力，避免为了 send-now 启动新的 AgentManager。
        """
        steer = getattr(self._runtime, "steer", None)
        if callable(steer):
            return steer(self._session_id, request) is True
        return False

    def _submit_in_background(self, text: str) -> None:
        """后台提交一条 root 文本输入。

        输入为文本；输出为空，副作用是创建受 root session 管理的 task 并纳入
        ``aclose`` 收编。异步 task 是队列执行的实现细节。
        """
        task: asyncio.Task[None] = asyncio.create_task(
            self._run_queued_submission(text),
            name=f"root-session-queue-run-{self._session_id}",
        )
        self._queue_tasks.add(task)

        def _discard_queue_task(done: asyncio.Task[None] = task) -> None:
            """队列 task 结束回调。

            输入为已完成 task；输出为空，副作用是释放引用并记录异常。
            """
            self._queue_tasks.discard(done)
            if not done.cancelled():
                exc = done.exception()
                if exc is not None:
                    logger.error("root session queued text task failed: %r", exc)

        task.add_done_callback(_discard_queue_task)

    async def _run_queued_submission(self, text: str) -> None:
        """执行已排队文本。

        输入为文本；输出为空。Result 渲染由 ``submit(QUEUE)`` 统一处理。
        """
        await self.submit(text, mode=SubmitMode.QUEUE)

    def _repost_undelivered(self, result: Result) -> None:
        """回投 runner 未能注入的 steer 残留输入。

        输入为当前 Result；输出为空，副作用是从 metadata 中移除
        ``steer_undelivered`` 并创建排队文本任务。
        """
        leftovers = result.metadata.pop("steer_undelivered", None)
        if isinstance(leftovers, list):
            for text in leftovers:
                self._submit_in_background(str(text))

    async def ensure_started(self) -> None:
        """懒启动 root AgentManager。

        输入为空；输出为空。首次调用时创建 ``AgentManager``、``SpawnContext``、
        ``TaskRegistry`` 并调用 ``boot_root``。
        """
        if self._agent_manager is not None:
            return

        spec = getattr(self._runtime, "agent_spec", None) or AgentSpec(
            name="host-dispatcher",
            instructions="",
            default_model="host-dispatcher-stub",
            tool_names=(),
            max_turns=1,
        )
        runtime = self._runtime
        session_id = self._session_id
        thread_id = self._thread_id

        async def mail_run_bridge(mail_text: str, *, mail: Mail) -> Result:
            """执行 root agent 的一次 runtime.run。

            输入为 mail 文本和 mail 元数据；输出为 Result。
            """
            if self._root_run_bridge is not None:
                return await self._root_run_bridge(mail_text, mail=mail)
            mail_metadata = getattr(mail.payload, "metadata", {}) or {}
            metadata = dict(mail_metadata) if isinstance(mail_metadata, dict) else {}
            run_kwargs: dict[str, Any] = {
                "session_id": session_id,
                "event_context": {
                    "run_epoch": mail.epoch,
                    "mail_kind": mail.kind,
                    "mail_task_id": mail.task_id,
                    "conversation_id": session_id,
                },
            }
            reasoning_effort = metadata.get("reasoning_effort")
            if not isinstance(reasoning_effort, str):
                reasoning_effort = None
            if _callable_accepts_keyword(runtime.run, "agent_id"):
                run_kwargs["agent_id"] = mail.recipient_agent_id
            if _callable_accepts_keyword(runtime.run, "thread_id"):
                run_kwargs["thread_id"] = thread_id
            if metadata.get("continue_from_last_user_message") is True:
                continue_fn = getattr(runtime, "continue_from_last_user_message", None)
                if callable(continue_fn):
                    continue_kwargs = dict(run_kwargs)
                    if reasoning_effort is not None and _callable_accepts_keyword(
                        continue_fn, "reasoning_effort"
                    ):
                        continue_kwargs["reasoning_effort"] = reasoning_effort
                    return cast(Result, await continue_fn(**continue_kwargs))
            if reasoning_effort is not None and _callable_accepts_keyword(
                runtime.run, "reasoning_effort"
            ):
                run_kwargs["reasoning_effort"] = reasoning_effort
            attachments = metadata.get("attachments")
            if isinstance(attachments, list) and _callable_accepts_keyword(
                runtime.run, "attachments"
            ):
                run_kwargs["attachments"] = attachments
            references = metadata.get("references")
            if isinstance(references, list) and _callable_accepts_keyword(
                runtime.run, "references"
            ):
                run_kwargs["references"] = references
            return await runtime.run(mail_text, **run_kwargs)

        def mail_run_bridge_builder(child: AgentCell) -> MailRunBridge:
            """构造 child agent 的 runtime.run 闭包。

            输入为 child cell；输出为绑定 child 配置的 mail_run_bridge。
            """

            async def child_mail_run_bridge(mail_text: str, *, mail: Mail) -> Result:
                """执行 child agent 的一次 runtime.run。

                输入为 mail 文本和 mail 元数据；输出为 Result。
                """
                run_kwargs: dict[str, Any] = {
                    "session_id": child.session_id,
                    "event_context": {
                        "run_epoch": mail.epoch,
                        "mail_kind": mail.kind,
                        "mail_task_id": mail.task_id,
                        "conversation_id": session_id,
                    },
                }
                if _callable_accepts_keyword(runtime.run, "agent_id"):
                    run_kwargs["agent_id"] = child.agent_id
                if _callable_accepts_keyword(runtime.run, "thread_id"):
                    # child.session_id 只标识执行实例；审批本子继续归属 root session。
                    run_kwargs["thread_id"] = thread_id
                if _callable_accepts_keyword(runtime.run, "agent_spec"):
                    run_kwargs["agent_spec"] = child.spec
                if _callable_accepts_keyword(runtime.run, "max_turns"):
                    run_kwargs["max_turns"] = child.spec.max_turns
                if _callable_accepts_keyword(runtime.run, "enabled_tools"):
                    run_kwargs["enabled_tools"] = child.run_enabled_tools
                if child.run_lifecycle_hooks and _callable_accepts_keyword(
                    runtime.run, "lifecycle_hooks"
                ):
                    run_kwargs["lifecycle_hooks"] = child.run_lifecycle_hooks
                if child.run_max_tokens is not None and _callable_accepts_keyword(
                    runtime.run, "max_tokens"
                ):
                    run_kwargs["max_tokens"] = child.run_max_tokens
                if child.run_temperature is not None and _callable_accepts_keyword(
                    runtime.run, "temperature"
                ):
                    run_kwargs["temperature"] = child.run_temperature
                if child.run_timeout_seconds is not None and _callable_accepts_keyword(
                    runtime.run, "timeout_seconds"
                ):
                    run_kwargs["timeout_seconds"] = child.run_timeout_seconds
                if child.run_llm_request_metadata and _callable_accepts_keyword(
                    runtime.run, "llm_request_metadata"
                ):
                    run_kwargs["llm_request_metadata"] = child.run_llm_request_metadata
                child_llm = None
                preset_id = child.spec.metadata.get("model_preset_id")
                catalog_manager = getattr(runtime, "model_catalog_manager", None)
                runtime_config = getattr(runtime, "config", None)
                if (
                    isinstance(preset_id, str)
                    and preset_id.strip()
                    and catalog_manager is not None
                    and runtime_config is not None
                ):
                    from infrastructure.llm_providers.provider_factory import build_provider

                    resolved_model = catalog_manager.resolve_runtime(
                        runtime_config.model,
                        preset_id=preset_id,
                        reasoning_effort=child.spec.reasoning_effort,
                    )
                    parent_model = getattr(runtime, "model_config", None)
                    if resolved_model != parent_model:
                        child_llm = build_provider(
                            runtime_config,
                            catalog_manager=catalog_manager,
                            resolved_model=resolved_model,
                        )
                        run_kwargs["llm_provider"] = child_llm
                try:
                    return await runtime.run(mail_text, **run_kwargs)
                finally:
                    if child_llm is not None:
                        close_fn = getattr(child_llm, "aclose", None)
                        if callable(close_fn):
                            await close_fn()

            return child_mail_run_bridge

        def deliver_sink_builder(
            child: AgentCell, task_id: str, parent_mailbox: asyncio.Queue[Any]
        ) -> ChildDeliverSink:
            """构造 child agent 上投 sink。

            输入为 child cell、task_id 和父 mailbox；输出为 ``ChildDeliverSink``。
            """
            return ChildDeliverSink(
                child=child,
                task_id=task_id,
                parent_mailbox=parent_mailbox,
                parent_agent_id=child.parent_id or "",
            )

        registry = TaskRegistry(
            registration_context=self._task_registration_context,
        )

        def epoch_getter() -> int:
            """读取当前 root agent epoch。

            输入为空；输出为当前 manager 的 epoch。
            """
            assert self._agent_manager is not None
            return self._agent_manager._epoch

        ctx = SpawnContext(
            mail_run_bridge_builder=mail_run_bridge_builder,
            deliver_sink_builder=deliver_sink_builder,
            current_epoch_getter=epoch_getter,
            registry=registry,
            tool_lookup=getattr(runtime, "tools", None),
        )
        manager = AgentManager(ctx, approval_canceller=self._approval_canceller)
        runtime_steer = getattr(runtime, "steer", None)
        steer_fn = runtime_steer if callable(runtime_steer) else None
        manager.boot_root(
            spec=spec,
            session_id=session_id,
            mail_run_bridge=mail_run_bridge,
            deliver_sink=HostDispatcherDeliverSink(self._result_futures),
            steer_fn=steer_fn,
            enabled_tools=getattr(runtime, "enabled_tools_snapshot", None),
        )
        loop_task = next(
            (t for t in manager._loop_tasks if t.get_name().startswith("agent-loop-root-")),
            None,
        )
        if loop_task is not None:
            loop_task.add_done_callback(self._fail_all_pending_futures)
        self._agent_manager = manager
        bind_runtime_router = getattr(self._agent_tree_runtime_router, "bind_dispatcher", None)
        if callable(bind_runtime_router):
            bind_runtime_router(self, session_id=session_id)

    def _fail_all_pending_futures(self, loop_task: asyncio.Task[None]) -> None:
        """agent_loop 异常退出时 fail 全部 pending future。

        输入为已结束的 loop task；输出为空，副作用是给未完成 future 设置异常。
        """
        exc = loop_task.exception() if loop_task.cancelled() is False else None
        for future in self._result_futures:
            if not future.done():
                future.set_exception(exc or RuntimeError("agent_loop exited unexpectedly"))

    async def interrupt(self) -> None:
        """打断当前 root agent 树。

        输入为空；输出为空。内部调用 ``AgentManager.interrupt``，让 runner 收口为
        cancelled Result。
        """
        if self._agent_manager is None:
            return
        await self._agent_manager.interrupt()

    async def reset_for_reuse(self) -> None:
        """重置 root agent 生命周期。

        输入为空；输出为空。用于 interrupt 后清理已关闭 registry 的 manager，让下一条
        输入可重新懒启动。
        """
        for task in list(self._queue_tasks):
            if not task.done():
                task.cancel()
        if self._agent_manager is not None:
            await self._agent_manager.teardown_root()
        for future in list(self._result_futures):
            if not future.done():
                future.cancel()
        self._result_futures.clear()
        self._agent_manager = None

    def has_active_work(self) -> bool:
        """判断当前是否存在活跃工作。

        输入为空；输出 bool。CLI 交互循环用它决定 Ctrl-C 是打断还是退出。
        """
        if any(not f.done() for f in self._result_futures):
            return True
        if any(not t.done() for t in self._queue_tasks):
            return True
        if self._agent_manager is None or self._agent_manager._root_agent_id is None:
            return False
        root = self._agent_manager._cells.get(self._agent_manager._root_agent_id)
        return root is not None and root.state == "running"

    async def aclose(self, *, drain: bool = False) -> None:
        """关闭 root agent session。

        输入为 drain 开关；输出为空。``drain=True`` 等待在途任务自然完成，
        ``drain=False`` 先 interrupt 并取消队列任务。
        """
        pending_queue_tasks = [t for t in self._queue_tasks if not t.done()]
        if self._agent_manager is None and not pending_queue_tasks:
            return

        if not drain:
            await self.interrupt()
            for task in list(self._queue_tasks):
                if not task.done():
                    task.cancel()

        pending_futures = [f for f in self._result_futures if not f.done()]
        pending_tasks = [t for t in self._queue_tasks if not t.done()]
        awaitables: list[Awaitable[Result | None]] = [*pending_futures, *pending_tasks]
        if awaitables:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*awaitables, return_exceptions=True),
                    timeout=5.0,
                )

        await self.reset_for_reuse()


class ScheduledRunHostDispatcher:
    """把普通 HostDispatcher 收敛成 scheduled run 所需的最小门户。"""

    def __init__(self, dispatcher: HostDispatcher) -> None:
        """持有单次 scheduled run 的普通 thread dispatcher。"""
        self._dispatcher = dispatcher

    async def run_scheduled_text(
        self,
        user_input: str,
        *,
        metadata: dict[str, object],
    ) -> Result:
        """通过普通 root mailbox 运行定时输入并返回统一 Result。"""
        return await self._dispatcher.run_text(
            user_input,
            metadata=dict(metadata),
        )

    def list_task_records(
        self,
        *,
        include_finished: bool = False,
    ) -> tuple[TaskProjection, ...]:
        """转发当前 root thread 的 TaskRegistry 投影。"""
        return self._dispatcher.list_task_records(include_finished=include_finished)

    async def interrupt(self) -> None:
        """转发到真实 AgentManager interrupt。"""
        await self._dispatcher.interrupt()

    async def aclose(self) -> None:
        """主动中断残余 scheduled work 并释放普通 dispatcher。"""
        await self._dispatcher.aclose(drain=False)


def build_scheduled_run_dispatcher_factory(
    runtime: SessionEngine,
) -> ScheduledRunDispatcherFactory:
    """把已装配 runtime 绑定成逐 run 创建 HostDispatcher 的工厂。"""

    def factory(
        *,
        session_id: str,
        thread_id: str,
        root_run_bridge: MailRunBridge,
        task_registration_context: TaskRegistrationContext,
    ) -> ScheduledRunDispatcher:
        """为一次 run 创建 stable thread + fresh session dispatcher。"""
        return ScheduledRunHostDispatcher(
            HostDispatcher(
                runtime=runtime,
                session_id=session_id,
                thread_id=thread_id,
                root_run_bridge=root_run_bridge,
                task_registration_context=task_registration_context,
            )
        )

    return factory


__all__ = [
    "HostDispatcher",
    "HostDispatcherDeliverSink",
    "ScheduledRunHostDispatcher",
    "SubmitMode",
    "SubmitReceipt",
    "build_scheduled_run_dispatcher_factory",
]
