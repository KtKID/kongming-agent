"""HostAdapter 协议的 web 实现。

把浏览器 WebSocket 连接缝进 :class:`host.base.HostAdapter` 接口，让
:class:`runtime_assembly.SessionEngine` 在不感知 web 的前提下
正常工作。

与 :class:`host.cli_adapter.CLIAdapter` 的对比：

- ``read_input``：CLI 阻塞读 stdin；web 不走 ``run_loop``，直接抛
  ``NotImplementedError`` —— 浏览器的输入由 ``user.input`` WS 帧驱动，
  路由层交给 ThreadManager / HostDispatcher 启动 run（参考 CLIInteractiveLoop）。
- ``write_output``：CLI 写 stdout；web 包成 ``AssistantFinalFrame`` 推 WS。
  注意：流式路径下 ``CLIStreamSink`` 已实时打印过完整 content；web 同理走
  ``WSEventSink.content.delta`` 推流，``write_output`` 几乎不会被调用。
- ``notify_event``：CLI verbose 时打 stderr；web 默认空操作 —— 事件流
  统一走 :class:`web.websocket.event_sink.WSEventSink`，避免双推。
- ``prompt_approval``：generic_chat 显式审批由
  :class:`safety.approval.manager.ApprovalManager` 统一处理。

设计要点：

- ``ws`` 参数是 duck-typed：要求 ``async send_json(payload: dict) -> None`` /
  ``async close() -> None``。FastAPI 的 ``WebSocket`` 天然满足；测试用
  ``unittest.mock.AsyncMock`` 同样满足。本类不直接 import FastAPI，避免
  把 web 路由依赖（FastAPI / starlette）牵进核心装配链。
- WS send 失败一律静默吞掉 + 标记 closed —— 浏览器断连不应污染 runtime
  主链路（由 ThreadManager 在外层捕获 ``cell.evicted`` 时机）。
- ``close()`` 只标记 closed；不主动 ``ws.close()`` —— WS 连接生命周期由
  路由层管理。ApprovalManager pending 清理由 ThreadManager 在 evict 路径处理。
- ``attach_ws()`` / ``detach_ws()`` 走 thread 级 fanout，让同一 thread 的
  多个聊天页共享同一条事件流。
"""

from __future__ import annotations

import logging
import time
from inspect import isawaitable
from typing import Any, Protocol, runtime_checkable

from typing_extensions import override

from core.contracts import Event
from core.result import Result
from hosts.shared.base import HostAdapter
from hosts.web.protocol import AssistantFinalFrame
from network.network_log import log_network_exception

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
    :class:`web.websocket.event_sink.WSEventSink` 共享同一个 WS 连接 ——
    实现上各持一份 ``_ws`` 引用，在 :meth:`attach_ws` 时由 ThreadCell
    同步替换。

    Attributes:
        _ws: 当前 WS 连接（可在 :meth:`attach_ws` 时替换）。
        _closed: WS 是否已断 / cell 已 evict。closed 后所有 send 静默吞掉。
    """

    def __init__(
        self,
        ws: Any,
    ) -> None:
        """构造 WebHostAdapter。

        Args:
            ws: 当前 WS 连接（duck-typed，含 ``send_json`` / ``close``）。
        """
        self._ws: Any = ws
        self._closed = False

    # ------------------------------------------------------------------
    # HostAdapter 接口
    # ------------------------------------------------------------------

    @override
    async def read_input(self) -> str | None:
        """web 不走 ``run_loop``：浏览器输入由 ``user.input`` WS 帧驱动。"""
        raise NotImplementedError(
            "WebHostAdapter does not support run_loop; "
            "drive turns via host_dispatcher.submit on each user.input frame"
        )

    @override
    async def write_output(self, text: str) -> None:
        """把 assistant 最终文本包成 ``AssistantFinalFrame`` 推浏览器。

        注意:流式路径下 ``WSEventSink.content.delta`` 已实时推过流,
        本方法在 web 主路径下几乎不会被调用(只剩命令输出 / [interrupted]
        / [send-now] merged 这类纯文案走这里)。Result 渲染走
        :meth:`render_result`。
        """
        if self._closed:
            return
        frame = AssistantFinalFrame(
            content=text,
            turn=-1,
            timestamp_ms=_now_ms(),
        )
        await self._safe_send_json(frame.model_dump())

    @override
    async def render_result(self, result: Result) -> None:
        """web 侧 :class:`Result` 渲染:无 error 时 no-op,有 error 时兜底推帧。

        web 主路径下 run 的内容 / 用量 / interrupt 全走 :class:`WSEventSink`
        实时帧(``content.delta`` / ``UsageFrame`` / ``RunInterruptedFrame``),
        **不依赖** bridge 层的 Result 回显。唯一需要兜底的是 ``result.error``
        非空时——error 走 ``error`` event 经 sink 推 ``ErrorFrame``,但兜底再
        推一条 ``AssistantFinalFrame`` 让前端一定能看到错误文本(等价老
        ``echo_final_content=False`` + error 行不受开关控制的合语义)。
        """
        if result.error is None:
            return
        if self._closed:
            return
        frame = AssistantFinalFrame(
            content=f"[error] {type(result.error).__name__}: {result.error.message}",
            turn=-1,
            timestamp_ms=_now_ms(),
        )
        await self._safe_send_json(frame.model_dump())

    @override
    async def notify_event(self, event: Event) -> None:
        """web 端事件统一走 :class:`web.websocket.event_sink.WSEventSink`。

        本方法保留协议兼容，默认空操作 —— 避免与 sink 双推同一事件。
        """
        return None

    @override
    async def close(self) -> None:
        """关闭 adapter：只标记 closed。

        幂等：多次调用安全。**不**主动 ``ws.close()`` —— WS 生命周期
        由路由层 / FastAPI 管理，避免 adapter 关闭后无法在 evict
        路径里继续推 ``cell.evicted`` 帧。
        """
        if self._closed:
            return
        self._closed = True

    def attach_ws(self, new_ws: Any) -> None:
        """向 thread fanout 注册一个新的 WS 连接。

        语义：
        - 替换 ``_ws`` 引用 + 重置 ``_closed=False``
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
                "hosts.web.app_support.host_adapter",
                "safe_send_failed",
                exc,
            )
            self._closed = True
            try:
                # 即便 send 失败也尝试 best-effort close（不抛）
                close_call = self._ws.close()
                if isawaitable(close_call):
                    await close_call
            except Exception as close_exc:
                log_network_exception(
                    "hosts.web.app_support.host_adapter",
                    "safe_send_close_failed",
                    close_exc,
                )


__all__ = ["WebHostAdapter"]
