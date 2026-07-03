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
from enum import StrEnum
from typing import Any

from application.agents.cell import AgentCell
from application.agents.registry import TaskRegistry
from core.contracts import EventSink
from core.result import Result
from hosts.shared.session_bridge import SessionBridge
from hosts.web.app_support.host_adapter import WebHostAdapter
from hosts.web.threads.metadata import ThreadMetadata
from runtime_assembly.native_runtime import NativeRuntime


class ThreadCellStatus(StrEnum):
    """cell 状态机。

    设计：只有 ``EVICTING`` 是 Manager 主动写入的"独立事实"——它表达
    "ThreadManager 已决定 evict 这个 cell"，无法从其它字段推导。``RUNNING`` /
    ``AWAITING_APPROVAL`` / ``IDLE`` 全是派生量，由 ``ThreadManager._effective_status``
    从 ``current_run_task`` / pending approval 现算，不落字段，避免多点手工同步漂移。

    成员（``StrEnum``，序列化值即小写字符串，与 Web wire 协议保持一致）：

    - ``IDLE``：未在跑 turn（默认占位）
    - ``RUNNING``：正在跑 turn（``current_run_task`` 非空，现算）
    - ``AWAITING_APPROVAL``：当前 thread 有待审批（现算）
    - ``EVICTING``：ThreadManager 已开始 evict_cell；后续 send 应被 closed 标记吞掉
    """

    IDLE = "idle"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    EVICTING = "evicting"


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
        adapter: 当前 :class:`WebHostAdapter` 实例；持有 HostAdapter 输出兼容入口。
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
        pending_inputs: 当前 thread 的后续普通输入队列，后端真源；只存尚未启动
            的输入，已经启动的输入交给 current_run_task 追踪。
        pending_input_lock: 队列和普通 run start gate 的互斥锁；入队、出队、即时
            启动判断必须在同一把锁内完成，避免并发提交创建两个 run。
        pending_input_sequence: 队列项 FIFO 序号，只在本 cell 内递增。
        pending_input_version: 队列 snapshot 版本号，用于前端丢弃旧帧；只有队列
            内容或顺序发生变化时递增。
        pending_input_drain_block_reason: 阻止 run done callback 继续 drain 的
            终止原因；evict / shutdown / delete / runtime refresh failed 写入，避免
            已失效 runtime 继续消费用户输入。
        runtime_preset_id: 当前 runtime 构造时使用的 preset。thread metadata
            允许先被 REST 更新；下一次发送前用本字段判断是否需要重建 runtime。
        preset_refresh_lock: 串行化同一 cell 的 runtime preset 刷新，避免并发
            rebuild 产生 runtime 泄漏或覆盖顺序不确定。
    """

    thread_id: str
    metadata: ThreadMetadata
    runtime: NativeRuntime
    bridge: SessionBridge
    adapter: WebHostAdapter
    event_sinks: list[EventSink] = field(default_factory=list)
    boot_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_active_at: float = field(default_factory=time.time)
    status: ThreadCellStatus = ThreadCellStatus.IDLE
    current_run_task: asyncio.Task[Result] | None = None

    # pending input queue 的状态归属在 cell 内存中。ThreadManager 是唯一写入者；
    # WS 路由和前端只通过 snapshot / changed / started 帧观察它。
    pending_inputs: list[Any] = field(default_factory=list)
    pending_input_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_input_sequence: int = 0
    pending_input_version: int = 0
    # drain_block_reason 是队列消费的停止闸门。evict、delete、shutdown、runtime
    # refresh failed 会写入原因，done callback 看到后停止启动下一条。
    pending_input_drain_block_reason: str | None = None
    runtime_preset_id: str = ""
    preset_refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ------------------------------------------------------------------
    # agent-tree-v0.1（模块 C）：ThreadCell 演进为树态 owner（不新建 ConversationTree）
    # ------------------------------------------------------------------
    # 三字段 + bump_epoch 让 ThreadCell 既是 host 装配外壳，又是 agent 树态 owner。
    # 用户决策：不新建 ConversationTree 类，epoch/registry/root_agent 归入 ThreadCell。
    # task-4 阶段新机制（agent_loop + mailbox）与旧机制（current_run_task /
    # pending_inputs）并存过渡：旧路径保留不破坏现有功能，agent_loop 是新主路径
    # （task-5 多 agent 形态时切换生产路径）。
    #
    # root_agent 默认 None：旧 _build_cell 路径 / 旧测试不构造 AgentCell 也能建 cell；
    # 真正接入 agent_loop 的 cell 由 manager 显式 set root_agent（opt-in 并存）。
    root_agent: AgentCell | None = None
    registry: TaskRegistry = field(default_factory=TaskRegistry)
    epoch: int = 0

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

    # ------------------------------------------------------------------
    # agent-tree-v0.1（模块 C）：epoch 唯一 bump 入口
    # ------------------------------------------------------------------
    def bump_epoch(self) -> int:
        """epoch 唯一 bump 入口，输入为空，输出为 bump 后的新 epoch。

        ``cancel_subtree`` 触发：把世代计数器 +1，让旧世代内部投递（``child_result``
        / ``system_notice``）在 mailbox 消费侧被 epoch 门卫丢弃。``user_message``
        永不过期（用户输入不携带世代语义，门卫判定时跳过 user_message）。

        返回 bump 后的新 epoch（单调递增）。调用方一般是 :class:`TaskRegistry`
        编排 / Web InterruptFrame：``bump_epoch()`` → ``registry.cancel_subtree(root)``
        → purge mailbox 旧世代内部投递。
        """
        self.epoch += 1
        return self.epoch


__all__ = ["ThreadCell", "ThreadCellStatus"]
