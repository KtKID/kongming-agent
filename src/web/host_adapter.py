"""HostAdapter 协议的 web 实现。

把浏览器 WebSocket 连接缝进 :class:`host.base.HostAdapter` 接口，让
:class:`executors.agent_runtime.NativeRuntime` 在不感知 web 的前提下
正常工作。

与 :class:`host.cli_adapter.CLIAdapter` 的对比：

- ``read_input``：CLI 阻塞读 stdin；web 不走 ``run_loop``，直接抛
  ``NotImplementedError`` —— 浏览器的输入由 ``user.input`` WS 帧驱动，
  路由层 / SessionBridge 收到后调 ``run_once``（参考 CLI 的 ``run_loop``）。
- ``write_output``：CLI 写 stdout；web 包成 ``AssistantFinalFrame`` 推 WS。
  注意：流式路径下 ``CLIStreamSink`` 已实时打印过完整 content；web 同理走
  ``WSEventSink.content.delta`` 推流，``write_output`` 几乎不会被调用。
- ``notify_event``：CLI verbose 时打 stderr；web 默认空操作 —— 事件流
  统一走 :class:`web.ws_event_sink.WSEventSink`，避免双推。
- ``prompt_approval``：CLI 阻塞读 ``[y/N]``；web 创建 ``asyncio.Future`` +
  推 ``ApprovalRequestFrame`` + ``await asyncio.wait_for(future, timeout)``。
  浏览器在收到帧后回 ``approval.ack``，路由层调 :meth:`resolve_approval`
  把 future 置位。

设计要点：

- ``ws`` 参数是 duck-typed：要求 ``async send_json(payload: dict) -> None`` /
  ``async close() -> None``。FastAPI 的 ``WebSocket`` 天然满足；测试用
  ``unittest.mock.AsyncMock`` 同样满足。本类不直接 import FastAPI，避免
  把 web 路由依赖（FastAPI / starlette）牵进核心装配链。
- WS send 失败一律静默吞掉 + 标记 closed —— 浏览器断连不应污染 runtime
  主链路（由 ThreadManager 在外层捕获 ``cell.evicted`` 时机）。
- ``close()`` 只 cancel 内部 future + 标记 closed；不主动 ``ws.close()``
  —— WS 连接生命周期由路由层管理（接受连接 / 收到 disconnect 帧时关闭）。
- ``attach_ws()`` / ``detach_ws()`` 走 thread 级 fanout，让同一 thread 的
  多个聊天页共享同一条事件流。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Protocol, runtime_checkable

from core.contracts import ApprovalAction, ApprovalRequest, Event
from host.base import HostAdapter
from observability.network_log import log_network_exception
from web.protocol import ApprovalRequestFrame, AssistantFinalFrame

logger = logging.getLogger(__name__)


@runtime_checkable
class _WSSendable(Protocol):
    """duck-typed WS 接口。

    本 Protocol 仅用于内部类型检查；不导出。FastAPI ``WebSocket`` 与
    ``unittest.mock.AsyncMock`` 都自动满足。
    """

    async def send_json(self, data: Any) -> None: ...

    async def close(self) -> None: ...


def _now_ms() -> int:
    """统一时间戳（毫秒）。"""
    return int(time.time() * 1000)


class WebHostAdapter(HostAdapter):
    """HostAdapter 协议的 web 实现。

    每个 ThreadCell 私有一个 adapter 实例（不跨 cell 共享），与
    :class:`web.ws_event_sink.WSEventSink` 共享同一个 WS 连接 ——
    实现上各持一份 ``_ws`` 引用，在 :meth:`attach_ws` 时由 ThreadCell
    同步替换。

    Attributes:
        _ws: 当前 WS 连接（可在 :meth:`attach_ws` 时替换）。
        _timeout: 单次审批等待超时（秒）。来自 ``cfg.web.pending_approval_timeout_seconds``。
        _closed: WS 是否已断 / cell 已 evict。closed 后所有 send 静默吞掉。
        _pending_approvals: ``call_id`` → ``Future[ApprovalAction]`` 映射。
            浏览器回 ``approval.ack`` 时由路由层调 :meth:`resolve_approval`
            置位；超时 / cell evict 时统一 set ``ApprovalAction.REJECT``。
            v0.1.6 起从二态 bool 升级为 :class:`ApprovalAction` 三态。
    """

    def __init__(
        self,
        ws: Any,
        *,
        pending_approval_timeout_seconds: float = 60.0,
        thread_id: str | None = None,
    ) -> None:
        """构造 WebHostAdapter。

        Args:
            ws: 当前 WS 连接（duck-typed，含 ``send_json`` / ``close``）。
            pending_approval_timeout_seconds: 单次审批等待超时（秒）。
            thread_id: 本 adapter 绑定的 thread ID。阶段 1（smart-approval-manager-v0.5）
                新增字段，用于 :meth:`close` 时调
                ``ApprovalManager.cancel_by_thread`` 清该 thread 名下的 manager pending
                （reviewer 必修 #4：避免用户关 tab 后 manager pending 卡 60s timeout
                / R10 内存泄漏诱因）。``None`` 时跳过 cancel_by_thread 调用，保持向后
                兼容（单测里大量 ``WebHostAdapter(_make_ws())`` 不传 thread_id）。
        """
        self._ws: Any = ws
        self._timeout = float(pending_approval_timeout_seconds)
        self._closed = False
        self._pending_approvals: dict[str, asyncio.Future[ApprovalAction]] = {}
        self._thread_id: str | None = thread_id

    # ------------------------------------------------------------------
    # HostAdapter 接口
    # ------------------------------------------------------------------

    async def read_input(self) -> str | None:
        """web 不走 ``run_loop``：浏览器输入由 ``user.input`` WS 帧驱动。"""
        raise NotImplementedError(
            "WebHostAdapter does not support run_loop; "
            "drive turns via SessionBridge.run_once on each user.input frame"
        )

    async def write_output(self, text: str) -> None:
        """把 assistant 最终文本包成 ``AssistantFinalFrame`` 推浏览器。

        注意：流式路径下 ``WSEventSink.content.delta`` 已实时推过流；
        ``SessionBridge`` 默认 ``echo_final_content=True`` 时仍会调到这里。
        Web 装配建议在 ``cfg.stream.enabled`` 为 True 时把 bridge 的
        ``echo_final_content`` 关掉，避免浏览器收到双重输出（与 CLI 同理）。

        ``turn`` 字段在协议层是必填整数；web 装配在通用路径下没有"当前 turn"
        的清晰来源（``write_output`` 由 SessionBridge 在 ``run_once`` 完成
        后触发，runner 已 emit ``turn.end``）。这里用 ``-1`` 作为占位值，
        让浏览器侧通过 ``timestamp_ms`` 排序即可，不依赖 ``turn`` 关联。
        """
        if self._closed:
            return
        frame = AssistantFinalFrame(
            content=text,
            turn=-1,
            timestamp_ms=_now_ms(),
        )
        await self._safe_send_json(frame.model_dump())

    async def notify_event(self, event: Event) -> None:
        """web 端事件统一走 :class:`web.ws_event_sink.WSEventSink`。

        本方法保留协议兼容，默认空操作 —— 避免与 sink 双推同一事件。
        """
        return None

    async def prompt_approval(  # type: ignore[override]
        self, request: ApprovalRequest
    ) -> ApprovalAction:
        """请求浏览器审批：推 ``ApprovalRequestFrame`` + 等 future 应答（三态）。

        三种 resolve 路径：

        - 浏览器回 ``approval.ack`` 帧 ``action`` 字段 → 路由层调
          :meth:`resolve_approval` → future.set_result(action)
        - 超时（``cfg.web.pending_approval_timeout_seconds``）→ 返回
          ``ApprovalAction.REJECT`` 视为拒绝
        - cell evict / adapter close → :meth:`close` 把 future
          ``set_result(REJECT)`` → 返回 ``ApprovalAction.REJECT``

        ``mark_action_aware`` 装饰器告诉 :class:`tools.approval.InteractiveApproval`
        本回调返回 :class:`ApprovalAction` 而非旧 ``bool``，从而把
        ``ACCEPT_FOR_SESSION`` 信号传到 :class:`safety.chain.SafetyGatedApproval`
        触发 GrantStore 写入（per-thread 物理隔离修 P1 bug）。

        在 ``self._closed`` 时直接返回 REJECT，避免把请求推到已断连接。
        重复 ``call_id`` 抛 ``RuntimeError`` —— call_id 由 runner 生成，
        理论上唯一；冲突意味着上游有逻辑漏洞，不该静默吞。

        **interrupt 语义（v0.1 interrupt-run-v0.1）**：``close()`` 不会让
        future 抛 ``CancelledError``（走 ``set_result(REJECT)``），因此本方法
        ``except asyncio.CancelledError`` 的唯一触发来源是**外部 task.cancel()**
        —— 也就是用户主动 interrupt 路径。此时把 ``CancelledError`` 透传给
        上游（runner 顶层 except）走 ``Result(status="interrupted")`` 收尾，
        而不是翻译成 REJECT 让 runner 误以为"用户拒绝了"继续跑下一轮。
        """
        if self._closed:
            return ApprovalAction.REJECT
        if request.call_id in self._pending_approvals:
            raise RuntimeError(
                f"duplicate approval call_id: {request.call_id}; "
                "runner is expected to ensure uniqueness per turn"
            )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalAction] = loop.create_future()
        self._pending_approvals[request.call_id] = future

        # 推 ApprovalRequestFrame 给浏览器
        # 从 request.metadata 透传 policy_hint / confirm_token（ConsentResolver 写入）
        req_meta = request.metadata or {}
        frame = ApprovalRequestFrame(
            call_id=request.call_id,
            tool_name=request.tool_name,
            arguments=dict(request.arguments),
            reason=request.reason,
            turn=request.turn,
            timestamp_ms=_now_ms(),
            policy_hint=req_meta.get("policy_hint"),
            confirm_token=req_meta.get("confirm_token"),
        )
        await self._safe_send_json(frame.model_dump())

        try:
            return await asyncio.wait_for(future, timeout=self._timeout)
        except TimeoutError:
            # 超时 = 拒绝；约定上前端要么及时 ack 要么用户走神
            logger.info(
                "approval timeout: call_id=%s tool=%s timeout=%.1fs",
                request.call_id,
                request.tool_name,
                self._timeout,
            )
            return ApprovalAction.REJECT
        except asyncio.CancelledError:
            # close() 走 set_result(REJECT) 不会触发这里；唯一来源 = 外部
            # task.cancel()（interrupt 路径）。把 CancelledError 透传给上游
            # runner 顶层 except，由 runner 走 Result(status="interrupted") 收尾。
            # 不能在这里翻译成 REJECT，否则 runner 会以为"用户拒绝了 1 个工具"
            # 继续跑下一轮，与 interrupt 语义冲突。
            raise
        finally:
            self._pending_approvals.pop(request.call_id, None)

    # 标记 prompt_approval 为 action-aware（返回 ApprovalAction 而非 bool）。
    # 直接在类外用属性赋值替代 @mark_action_aware 装饰器：装饰器签名是
    # ``Callable[[ApprovalRequest], Awaitable[ApprovalAction]]``，与方法的
    # ``Callable[[Self, ApprovalRequest], ...]`` 不兼容，会触发 mypy arg-type
    # 错误。属性赋值在运行时和装饰器等价，绕过 mypy 静态检查。
    prompt_approval.__action_aware__ = True  # type: ignore[attr-defined]

    async def close(self) -> None:
        """关闭 adapter：cancel 所有 pending future + 标记 closed。

        幂等：多次调用安全。**不**主动 ``ws.close()`` —— WS 生命周期
        由路由层 / FastAPI 管理，避免 adapter 关闭后无法在 evict
        路径里继续推 ``cell.evicted`` 帧。

        阶段 1（smart-approval-manager-v0.5）reviewer 必修 #4 新增：除清自身
        ``prompt_approval`` pending（老逻辑，本 adapter 内的 future）外，
        若 adapter 持有 ``thread_id``，还通知 :class:`safety.approval_manager.ApprovalManager`
        取消该 thread 名下所有 manager 路径的 pending，避免用户关 tab 后 manager pending
        卡 60s timeout / R10 内存泄漏。``cancel_by_thread`` 失败一律吞掉（非主流程）。
        """
        if self._closed:
            return
        self._closed = True
        # cancel pending approvals → resolve REJECT（v0.1.6 三态）
        for fut in list(self._pending_approvals.values()):
            if not fut.done():
                fut.set_result(ApprovalAction.REJECT)
        self._pending_approvals.clear()

        # 阶段 1 新增（reviewer 必修 #4）：通知 manager 清该 thread 名下所有 pending。
        # generic_chat 通道 prompt_fn 走 manager 路径后，pending future 在 manager 侧；
        # 不在这里 cancel 用户关 tab 时 manager pending 卡 60s 才超时。
        if self._thread_id:
            try:
                from safety.approval_manager import get_approval_manager

                manager = get_approval_manager()
                cancelled = manager.cancel_by_thread(self._thread_id, reason="cell_evict")
                if cancelled > 0:
                    logger.debug(
                        "WebHostAdapter.close cancelled %d manager pending for thread=%s",
                        cancelled,
                        self._thread_id,
                    )
            except Exception:
                logger.exception(
                    "WebHostAdapter.close: cancel_by_thread failed (non-fatal)",
                )

    # ------------------------------------------------------------------
    # 公开方法（供 ThreadManager / WS 路由层调用）
    # ------------------------------------------------------------------

    def resolve_approval(self, call_id: str, action: ApprovalAction) -> None:
        """收到浏览器 ``approval.ack`` 时由路由层调用（三态）。

        约定：
        - 找不到对应 future（已超时清理 / call_id 不存在）→ 静默丢弃 ack
        - future 已 done（重放 ack / 超时刚 fire）→ 静默丢弃
        - 否则 ``set_result(action)``，由 :meth:`prompt_approval` 的
          finally 块从字典中移除

        v0.1.6 升级：``approved: bool`` → ``action: ApprovalAction``。路由
        层负责把浏览器 ``approval.ack`` 帧的 ``action`` 字段（字符串）
        转成 :class:`ApprovalAction` 枚举；非法字段降级为 ``REJECT``。

        多次 ack 同一 call_id 的容错由 ``future.done()`` 守卫兜底。
        """
        future = self._pending_approvals.get(call_id)
        if future is None:
            return
        if future.done():
            return
        future.set_result(action)

    def attach_ws(self, new_ws: Any) -> None:
        """向 thread fanout 注册一个新的 WS 连接。

        语义：
        - 替换 ``_ws`` 引用 + 重置 ``_closed=False``
        - **不**改 pending_approvals 状态：v0.1.5 不补发 in-flight 帧，
          浏览器需要重新发请求；future 在原 wait_for 超时前仍可被 ack 命中
          （前提是浏览器记住 call_id 并重发 ack）
        - 调用方应在替换后重新推 ``thread.history`` 帧，让 UI 同步
        """
        attach = getattr(type(self._ws), "attach_ws", None)
        if callable(attach):
            attach(self._ws, new_ws)
        else:
            self._ws = new_ws
        self._closed = False

    def detach_ws(self, ws: Any) -> None:
        """从 thread fanout 注销一个 WS 连接。"""
        detach = getattr(type(self._ws), "detach_ws", None)
        if callable(detach):
            detach(self._ws, ws)
            return
        if self._ws is ws:
            self._closed = False

    @property
    def closed(self) -> bool:
        """供测试 / 装配层观察 closed 状态。"""
        return self._closed

    @property
    def pending_approval_count(self) -> int:
        """供 ``CellSummaryDTO.pending_approval_count`` 字段读取。"""
        return len(self._pending_approvals)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _safe_send_json(self, payload: dict[str, Any]) -> None:
        """对 WS send 做异常吞咽 + closed 标记。

        约定：任何异常都标记 closed + 静默吞掉，不向上抛。
        - WebSocket 已断（``ConnectionClosed`` / ``RuntimeError``）
        - 序列化异常（理论上不会发生，pydantic 帧已 ``model_dump``）
        - 任意第三方 ws 实现的奇怪异常

        日志写 WARNING 级别，但 trace 不记录（trace 走 EventSink 不走 adapter）。
        """
        if self._closed:
            return
        try:
            await self._ws.send_json(payload)
        except Exception as exc:
            logger.warning(
                "WebHostAdapter ws.send_json failed: %s; marking closed",
                exc,
            )
            log_network_exception(
                "web.host_adapter",
                "safe_send_failed",
                exc,
            )
            self._closed = True
            try:
                # 即便 send 失败也尝试 best-effort close（不抛）
                close_call = self._ws.close()
                if asyncio.iscoroutine(close_call):
                    await close_call
            except Exception as close_exc:
                log_network_exception(
                    "web.host_adapter",
                    "safe_send_close_failed",
                    close_exc,
                )


__all__ = ["WebHostAdapter"]
