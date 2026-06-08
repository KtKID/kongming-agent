"""单个 thread 的私有装配束。

:class:`ThreadCell` 是 ThreadManager 字典的 value 类型；每个 cell 自包含
runtime / bridge / adapter / event_sinks 与一份 boot_lock。cell 间不共享
任何可变状态，便于按 thread 独立 evict 而不影响其他 thread。

设计要点：

- **不用 ``frozen=True``**：cell 包含可变字段（``status`` / ``last_active_at``
  / ``current_run_task``），dataclass 必须可变。不可变字段（``thread_id`` /
  ``runtime`` 等）由约定 + 测试覆盖兜底。
- **不在 cell 里存 history**：history 在 ``runtime._sessions[thread_id]``
  里；cell 只持装配束。
- **不在 cell 里存 ws**：ws 引用同时在 ``adapter._ws`` 和每个
  ``WSEventSink._ws`` 里。重连时通过 :meth:`attach_ws` 同时替换两处引用，
  避免单一引用源被 ThreadManager / 路由层各自访问时不一致。
- **boot_lock 用 ``asyncio.Lock``**：per-thread 锁，让 ``boot_or_attach``
  对同一 thread_id 的并发调用串行（防止两次 build 同 runtime）。锁不跨
  cell 共享 — ThreadManager 的全局锁只保护 dict 读写，不持锁等 boot 完成。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from core.contracts import EventSink
from core.result import Result
from host.session_bridge import SessionBridge
from runtime_assembly.native_runtime import NativeRuntime
from web.app_support.host_adapter import WebHostAdapter
from web.threads.metadata import ThreadMetadata

# cell 状态机（单字段 status）。
# - idle：未在跑 turn，无 pending approval
# - running：正在跑 turn（runner.run 进行中）
# - awaiting_approval：runner 阻塞在 prompt_approval（pending future 非空）
# - evicting：ThreadManager 已开始 evict_cell；后续 send 应当被 closed 标记吞掉
ThreadCellStatus = Literal["idle", "running", "awaiting_approval", "evicting"]


@dataclass
class ThreadCell:
    """单个 thread 的私有装配束。

    Attributes:
        thread_id: thread ID，与 metadata.id / session_id 一致。
        metadata: 当前 thread 的元数据快照。rename / message_count 更新时
            重新赋值（dataclass 可变；metadata 本身 frozen）。
        runtime: per-thread 独立 :class:`NativeRuntime`（v0.1.5 不共享 LLM
            provider，每个 cell 一份 httpx pool；v0.1.6+ 再做共享）。
        bridge: 把 adapter / runtime 缝起来的 :class:`SessionBridge`；调用
            ``bridge.run_once(user_input)`` 触发一轮对话。
        adapter: 当前 :class:`WebHostAdapter` 实例；持有 WS 引用 +
            pending_approvals dict。
        event_sinks: cell 私有的事件 sink 列表（典型为
            ``[WSEventSink, JsonlTraceSink?]``；不跨 cell 共享）。
        boot_lock: per-cell asyncio.Lock，``boot_or_attach`` 对同 thread_id
            并发调用时串行化，避免两次 NativeRuntime.build。
        last_active_at: 最近一次"活动"时间戳（Unix 秒）。任何 WS 入帧 /
            出帧 / REST 显式访问 / run_once 完成 都应调 :meth:`touch`。
            ``_idle_eviction_loop`` 用此字段判定空闲。
        status: cell 当前状态。
        current_run_task: 当前正在执行的 ``run_once`` task；shutdown 时
            可 cancel 它快速结束（非 None 表示有进行中的 turn）。
    """

    thread_id: str
    metadata: ThreadMetadata
    runtime: NativeRuntime
    bridge: SessionBridge
    adapter: WebHostAdapter
    event_sinks: list[EventSink] = field(default_factory=list)
    boot_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_active_at: float = field(default_factory=time.time)
    status: ThreadCellStatus = "idle"
    current_run_task: asyncio.Task[Result] | None = None

    def attach_ws(self, new_ws: Any) -> None:
        """把一个新连接注册到 adapter / 所有 sink。

        约定：所有 sink 必须实现 ``attach_ws(new_ws)`` 接口才会被替换；
        没实现的 sink（如 JsonlTraceSink）不需要 ws，跳过即可。
        """
        self.adapter.attach_ws(new_ws)
        for sink in self.event_sinks:
            attach = getattr(sink, "attach_ws", None)
            if callable(attach):
                attach(new_ws)

    def detach_ws(self, ws: Any) -> None:
        """把一个断开的连接从 adapter / 所有 sink 注销。"""
        detach = getattr(self.adapter, "detach_ws", None)
        if callable(detach):
            detach(ws)
        for sink in self.event_sinks:
            detach_sink = getattr(sink, "detach_ws", None)
            if callable(detach_sink):
                detach_sink(ws)

    def touch(self) -> None:
        """更新 ``last_active_at``。

        调用时机（建议）：
        - WS 收到任何浏览器入帧时
        - SessionBridge.run_once 进入 / 完成时
        - REST 显式访问 thread（GET /api/threads/{id}/...）
        """
        self.last_active_at = time.time()

    @property
    def has_pending_approvals(self) -> bool:
        """便于 idle eviction 判定：有 pending approval 时不 evict。"""
        return self.adapter.pending_approval_count > 0


__all__ = ["ThreadCell", "ThreadCellStatus"]
