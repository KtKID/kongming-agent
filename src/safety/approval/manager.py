"""审批层统一入口（v0.5 阶段 1）。

所有通道（claude_code / generic_chat / cron / evolution / cli）的审批请求最终
都进这个 manager。阶段 1 仅服务 generic_chat web；阶段 2-4 接入其他通道。

设计真源：``docs/safety-approval-manager-v0.5/10-architecture.md``。

字面值约定（spec 与 contract 漂移说明）：

- spec 20-data-model 描述 ``ApprovalDecision.outcome`` 为 ``"allowed"`` / ``"rejected"``；
- 实际 contract :data:`core.contracts.ApprovalOutcome` Literal 为
  ``"approved"`` / ``"rejected"`` / ``"cancelled"`` / ``"pending"``。
- manager 服从 **contract 真源**：所有构造 ``ApprovalDecision`` 的位置都用
  ``"approved"`` / ``"rejected"``。spec 文档的字面值漂移留待 v0.5 协议演进
  task 一起修，不在阶段 1 范围内擅自扩 Literal。
- ``ApprovalAction`` 二态映射保持 spec 含义：用户选 allow → ACCEPT_ONCE，
  用户选 deny → REJECT。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.contracts import ApprovalAction, ApprovalDecision, ApprovalRequest
from safety.approval.events import PendingApprovalView
from safety.approval.rules import ApprovalRules

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class _PendingApproval:
    """manager 内部 pending 审批的完整状态（不暴露给外部）。

    字段对齐 spec ``20-data-model#1`` 完整 12 字段；阶段 1 generic_chat
    大部分字段填 None，但 dataclass 字段必须有，避免阶段 2-4 通道接入时
    再改 dataclass 形态。

    Attributes:
        request_id: 全局唯一 ID（uuid hex），用户决策回写时按这个查 future
        channel: 通道名（generic_chat / claude_code / cron / evolution / cli）
        thread_id: 通道内 thread 标识
        cwd: 触发审批的工作目录
        tool_name: 工具名（如 ``Bash`` / ``Edit``）
        tool_input: 工具参数原始 dict
        metadata: 额外 metadata（由调用方注入，透传给 EventSink）
        severity: 'standard' / 'elevated'，rules.classify 返回
        matched_rule: 命中的规则 ID（阶段 1 generic_chat 恒 None）
        auto_approve_at_ms: 安全路径倒计时到点（阶段 1 generic_chat 恒 None）
        auto_reject_at_ms: 危险路径倒计时到点（阶段 1 generic_chat 恒 None）
        future: 等待用户决策的 Future；resolve / cancel / timeout 三路径都
            通过 ``set_result`` 唤醒 ``request()`` 的 ``await``
        arrived_at_ms: 到达 manager 的毫秒时间戳（用于 EventSink 渲染 / 排序）
        timeout_ms: 用户决策超时阈值（毫秒）；None 用 manager 默认值
    """

    request_id: str
    channel: str
    thread_id: str
    cwd: str
    tool_name: str
    tool_input: dict[str, Any]
    metadata: dict[str, Any]
    severity: str
    matched_rule: str | None
    auto_approve_at_ms: int | None
    auto_reject_at_ms: int | None
    future: asyncio.Future[ApprovalDecision]
    arrived_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    timeout_ms: int | None = None


# ---------------------------------------------------------------------------
# EventSink Protocol（manager 内部抽象，不放到 core.contracts）
# ---------------------------------------------------------------------------


class ApprovalEventSink(Protocol):
    """审批事件输出层抽象（manager 内部 Protocol）。

    阶段 1：仅 :class:`safety.inbox.event_sink.InboxEventSink` 一种实现；
    阶段 4：加 ``CLIEventSink``，Protocol 签名必须能覆盖所有可预见通道，
    避免阶段 4 改 Protocol 破坏阶段 1 实现（spec 60-risk R4）。

    设计原则：

    - emit 失败不抛：所有 emit 调用都被 manager 用 ``asyncio.gather(..., return_exceptions=True)``
      包裹，sink 内部也应吞自己的异常，审批主流程绝不因 sink 失败而断
    - emit 是 async：允许 sink 内做 ws fan-out / 网络 IO
    - sink 不持 state：每次调用都用 manager 传过来的 pending 上下文
    """

    async def emit_approval_required(self, *, pending: PendingApprovalView) -> None:
        """通知 sink 有新 pending 审批需要用户决策。

        Args:
            pending: 新创建 pending 的公开只读视图，sink 据此展示审批请求并注册
                按 request_id 路由的 resolve callback。
        """
        ...

    async def emit_approval_removed(self, *, request_id: str, reason: str) -> None:
        """通知 sink 某条 pending 已结束（user_decided / timeout / cancelled）。

        Args:
            request_id: 对应已 emit 过 required 的 pending
            reason: ``"user_decided"`` / ``"timeout"`` / ``"cancelled"``
        """
        ...


# ---------------------------------------------------------------------------
# Manager 主类
# ---------------------------------------------------------------------------


class ApprovalManager:
    """审批层统一入口（per-process 单例）。

    阶段 1 仅服务 generic_chat web 通道；接口预留 ``channel`` 参数 +
    :meth:`register_event_sink`，为阶段 2-4 接入更多通道做准备。

    线程安全与资源回收（spec 60-risk R9/R10）：

    - **R9 lock 安全**：``_lock`` 内只做 dict 增删 + subscriber 列表快照拷贝；
      ``await future`` / ``await IO`` 必须在 lock 外（避免 lock-and-block 死锁）
    - **R10 timeout 清理**：``resolve`` / ``cancel`` / ``timeout`` 三退出路径
      在 ``request()`` 的 ``finally`` 都通过 :meth:`_cleanup_pending` 清
      ``_pending`` + ``_timeout_tasks``，避免 timeout task 泄漏

    Attributes:
        _rules: per-channel + per-cwd 规则容器（策略层委托）
        _event_sinks: 输出层 fan-out 目标列表（可动态注册）
        _default_timeout_ms: 未指定超时时的默认值
        _pending: ``request_id`` → ``_PendingApproval`` 映射
        _timeout_tasks: ``request_id`` → ``asyncio.Task`` 映射（resolve/cancel 时取消）
        _auto_approve_tasks: ``request_id`` → ``asyncio.Task`` 映射；阶段 1.5
            generic_chat per-cwd 自动通过倒计时 task（rule_dec.auto_approve_at_ms
            非 None 时创建；resolve / cancel / timeout 三退出路径都要 cancel + pop）
        _lock: ``_pending`` / ``_timeout_tasks`` / ``_auto_approve_tasks`` /
            ``_event_sinks`` 互斥锁
    """

    def __init__(
        self,
        *,
        rules: ApprovalRules,
        event_sinks: list[ApprovalEventSink] | None = None,
        default_timeout_ms: int = 60_000,
    ) -> None:
        """构造 manager 单例。

        Args:
            rules: per-channel + per-cwd 规则容器（策略层委托；阶段 1 是空骨架）
            event_sinks: 输出层 fan-out 目标
                （阶段 1 InboxEventSink；阶段 4 加 CLIEventSink）
            default_timeout_ms: 未指定超时时的默认值（与 WebHostAdapter._timeout 一致）
        """
        self._rules = rules
        self._event_sinks: list[ApprovalEventSink] = list(event_sinks or [])
        self._default_timeout_ms = default_timeout_ms
        self._pending: dict[str, _PendingApproval] = {}
        self._timeout_tasks: dict[str, asyncio.Task[None]] = {}
        self._auto_approve_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    def register_event_sink(self, sink: ApprovalEventSink) -> None:
        """注册 EventSink（同步方法，装配期调用）。

        Args:
            sink: 实现 :class:`ApprovalEventSink` Protocol 的输出层实例
        """
        self._event_sinks.append(sink)

    def has_event_sink_type(self, sink_type: type[object]) -> bool:
        """判断指定类型的事件接收器是否已经注册。"""
        return any(isinstance(s, sink_type) for s in self._event_sinks)

    @property
    def pending_count(self) -> int:
        """当前 pending 审批数（仅供监控 / 测试断言用）。"""
        return len(self._pending)

    @property
    def timeout_task_count(self) -> int:
        """当前 timeout task 数（应等于 pending_count；用于 R10 内存泄漏监控）。"""
        return len(self._timeout_tasks)

    @property
    def auto_approve_task_count(self) -> int:
        """当前 auto-approve task 数（阶段 1.5；R10 内存泄漏监控用）。

        非 generic_chat / cwd config disabled 路径不创建 task，恒为 0；
        启用自动通过且 pending 未结束的 request 才计数。
        """
        return len(self._auto_approve_tasks)

    async def request(
        self,
        *,
        channel: str,
        thread_id: str,
        cwd: str,
        tool_name: str,
        tool_input: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
    ) -> ApprovalDecision:
        """请求用户审批。阻塞直到 resolve / timeout / cancel。

        流程：

        1. ``rules.classify`` 拿初步决策（severity / matched_rule / auto_*_at_ms）
        2. ``is_immediate=True`` → 直接 return（阶段 1 不触发；阶段 3 cron auto-allow 会）
        3. 创建 ``_PendingApproval`` + Future + timeout task（lock 内）
        4. fan-out 到所有 EventSink（lock 外 await）
        5. ``await future`` 等用户决策（lock 外）
        6. ``finally`` 清 ``_pending`` / ``_timeout_tasks``（R10）

        Args:
            channel: 通道名（generic_chat / claude_code / cron / evolution / cli）
            thread_id: 通道内 thread 标识（cancel_by_thread 按这个批量取消）
            cwd: 触发审批的工作目录
            tool_name: 工具名（如 ``Bash`` / ``Edit``）
            tool_input: 工具参数原始 dict
            metadata: 额外 metadata（透传给 EventSink；默认 ``{}``）
            timeout_ms: 用户决策超时（毫秒）；None 走 rules 返回或 manager 默认

        Returns:
            ``ApprovalDecision``，``outcome`` 为 ``"approved"`` / ``"rejected"``
            （contract Literal 真源；spec 描述的 ``"allowed"`` 是文档漂移）
        """
        rule_dec = self._rules.classify(
            channel=channel,
            thread_id=thread_id,
            cwd=cwd,
            tool_name=tool_name,
            tool_input=tool_input,
        )

        # 阶段 1 不会走 is_immediate 路径；为阶段 3 cron auto-allow 留接口
        if rule_dec.is_immediate:
            return ApprovalDecision(
                outcome=rule_dec.immediate_outcome or "rejected",  # type: ignore[arg-type]
                metadata={
                    "source": "rule_immediate",
                    "matched_rule": rule_dec.matched_rule,
                },
            )

        request_id = uuid.uuid4().hex
        actual_timeout_ms = timeout_ms or rule_dec.timeout_ms or self._default_timeout_ms
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()

        pending = _PendingApproval(
            request_id=request_id,
            channel=channel,
            thread_id=thread_id,
            cwd=cwd,
            tool_name=tool_name,
            tool_input=tool_input,
            metadata=metadata or {},
            severity=rule_dec.severity,
            matched_rule=rule_dec.matched_rule,
            auto_approve_at_ms=rule_dec.auto_approve_at_ms,
            auto_reject_at_ms=rule_dec.auto_reject_at_ms,
            future=future,
            timeout_ms=actual_timeout_ms,
        )

        # R9：lock 内只做 dict 增删 + sinks 快照拷贝；不 await
        # asyncio.create_task 本身是同步调用（仅 schedule，不 await），允许在 lock 内
        #
        # 阶段 1.5 task #6 设计要点（auto-approve 与 timeout 生命周期对齐）：
        #
        # - cwd 未启用自动通过（``auto_approve_at_ms is None``）→ 走原 fail-closed
        #   timeout 路径（_handle_timeout 到点 reject）
        # - cwd 启用自动通过 → 主路径是 _handle_auto_approve 到点 approve；
        #   timeout task 仍启动但 delay = auto_approve delay + 5s grace，作为
        #   **兜底保险**（防 auto-approve task 异常 crash 导致 pending 永挂）。
        #   正常路径 auto-approve 先触发 → cleanup cancel timeout task → 不会 race。
        async with self._lock:
            self._pending[request_id] = pending
            timeout_delay = actual_timeout_ms / 1000.0
            if rule_dec.auto_approve_at_ms is not None:
                now_ms = int(time.time() * 1000)
                auto_delay = max(0.0, (rule_dec.auto_approve_at_ms - now_ms) / 1000.0)
                auto_task = asyncio.create_task(self._handle_auto_approve(request_id, auto_delay))
                self._auto_approve_tasks[request_id] = auto_task
                # timeout 兜底必须晚于 auto-approve（避免 race 输给 timeout）；
                # 加 5s grace 给 auto-approve task 充足执行窗口
                timeout_delay = max(timeout_delay, auto_delay + 5.0)
            timeout_task = asyncio.create_task(self._handle_timeout(request_id, timeout_delay))
            self._timeout_tasks[request_id] = timeout_task
            sinks_snapshot = list(self._event_sinks)

        # R9：lock 外 fan-out（emit 失败不影响主流程）
        pending_view = _pending_to_view(
            pending,
            default_timeout_ms=self._default_timeout_ms,
        )
        await asyncio.gather(
            *(s.emit_approval_required(pending=pending_view) for s in sinks_snapshot),
            return_exceptions=True,
        )

        try:
            decision = await future
            # 反推 fan_out reason（基于 metadata.source）
            source = decision.metadata.get("source", "")
            if source == "manager_timeout":
                reason = "timeout"
            elif source == "manager_cancel":
                reason = "cancelled"
            elif source == "rule_auto_allow":
                reason = "auto_allowed"
            else:
                reason = "user_decided"
            await self._cleanup_pending(request_id, fan_out_reason=reason)
            return decision
        except BaseException:
            # 上层 cancel ``request()`` 的 task 时进这里：仍要清 pending 防泄漏
            await self._cleanup_pending(request_id, fan_out_reason="cancelled")
            raise

    def resolve(self, request_id: str, decision: dict[str, Any]) -> bool:
        """所有 UI 入口的决策回写点。

        被 :class:`safety.inbox.event_sink.InboxEventSink` 注册的 callback /
        CLIEventSink stdin 回调等调用。本方法只 set_result Future，清理由
        ``request()`` 的 finally 走 :meth:`_cleanup_pending` 完成。

        Args:
            request_id: 对应 :class:`_PendingApproval`.request_id
            decision: dict，包含：

                - ``allow`` (bool)：用户决策
                - ``message`` (str, 可选)：用户备注
                - ``rememberEntry`` (str, 可选)：claude_code 旧协议字段
                - ``rememberScope`` (str, 可选)：``"session"`` / ``"persistent"``
                  （阶段 1 generic_chat 不传；阶段 5 协议演进后支持）

        Returns:
            True = 找到 pending future 并 set_result；False = request_id 未知
            （重复 resolve / 已 timeout / 已 cancelled）
        """
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            return False

        allow = bool(decision.get("allow", False))
        remember_scope = decision.get("rememberScope")
        # spec 字面值用 'allowed'，contract Literal 用 'approved'；按 contract 真源
        outcome: str = "approved" if allow else "rejected"
        meta: dict[str, Any] = {
            "source": "user",
            "decided_by": "user",
            "matched_rule": pending.matched_rule,
        }
        if remember_scope == "session":
            meta["remember_for_session"] = True
        elif remember_scope == "persistent":
            meta["remember_persistent"] = True
        if msg := decision.get("message"):
            meta["message"] = msg
        if remember := decision.get("rememberEntry"):
            meta["rememberEntry"] = remember

        pending.future.set_result(
            ApprovalDecision(outcome=outcome, metadata=meta)  # type: ignore[arg-type]
        )

        # generic-chat-session-grant：用户点「本次会话都同意」+ allow=True
        # → 触发 thread 级授权写入；下次同 thread + cwd + tool 的
        # classify 命中 _thread_overrides → 立即允许。
        # 仅 allow=True + rememberScope="session" 时写入（拒绝时即使带
        # rememberScope 也不写——拒绝没有记忆语义）。
        if allow and remember_scope == "session":
            try:
                self._rules.add_session_grant(
                    channel=pending.channel,
                    thread_id=pending.thread_id,
                    cwd=pending.cwd,
                    tool_name=pending.tool_name,
                )
            except Exception:
                # 失败开放：授权写入失败不影响本次 resolve 的成功结果
                # （下次仍弹卡，等同没记住——可接受降级）
                logger.exception(
                    "add_session_grant failed: thread=%s cwd=%s tool=%s",
                    pending.thread_id,
                    pending.cwd,
                    pending.tool_name,
                )
        return True

    def cancel(self, request_id: str, reason: str = "cancelled") -> bool:
        """取消 pending 审批（abort / shutdown / ws 断 路径）。

        Args:
            request_id: 对应 pending 的 ID
            reason: 取消原因，写到 ``metadata.reason`` 便于 trace

        Returns:
            True = 找到 pending future 并 set_result(rejected)；False = 未知 request_id
        """
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(
            ApprovalDecision(
                outcome="rejected",
                metadata={"source": "manager_cancel", "reason": reason},
            )
        )
        return True

    def cancel_by_thread(self, thread_id: str, reason: str = "cancelled") -> int:
        """取消某 thread 名下所有 pending 审批（WebHostAdapter.close / cell evict 用）。

        reviewer 必修 #4：避免用户关 tab 后 pending 卡 60s timeout
        （spec 60-risk R10 内存泄漏现实诱因）。

        generic-chat-session-grant：同时清掉该 thread 的 session grant
        （:meth:`ApprovalRules.clear_thread_grants`）；与 pending cancel 同款
        生命周期，防止 thread 关闭后 grant 残留造成内存泄漏。

        Args:
            thread_id: 要清理的 thread
            reason: 写到每条 pending 的 metadata.reason

        Returns:
            实际取消的 pending 数量
        """
        # 拷贝列表避免迭代中改 dict
        targets = [
            req_id
            for req_id, p in self._pending.items()
            if p.thread_id == thread_id and not p.future.done()
        ]
        count = 0
        for req_id in targets:
            if self.cancel(req_id, reason=reason):
                count += 1
        # 清 thread 级本次会话授权（R10 同款防内存泄漏）；失败不抛
        try:
            self._rules.clear_thread_grants(thread_id)
        except Exception:
            logger.exception("clear_thread_grants failed: thread=%s", thread_id)
        return count

    async def _handle_timeout(self, request_id: str, timeout_seconds: float) -> None:
        """timeout task 主体：等 N 秒后把 pending future set_result(rejected)。

        reviewer 必修 #3：timeout 返回 fail-closed 拒绝
        （与 :meth:`web.app_support.host_adapter.WebHostAdapter.prompt_approval` 的 TimeoutError
        分支行为一致）。

        被 cancel 时（``task.cancel()``）静默退出，不动 future。

        Args:
            request_id: pending 的 ID
            timeout_seconds: 等待秒数
        """
        try:
            await asyncio.sleep(timeout_seconds)
        except asyncio.CancelledError:
            return
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            return
        pending.future.set_result(
            ApprovalDecision(
                outcome="rejected",
                metadata={"source": "manager_timeout", "reason": "timeout"},
            )
        )

    async def _handle_auto_approve(self, request_id: str, delay_seconds: float) -> None:
        """auto-approve task 主体：等 N 秒后把 pending future set_result(approved)。

        阶段 1.5 generic_chat per-cwd 自动通过倒计时；与 :meth:`_handle_timeout`
        对称：sleep → 查 pending → set_result。差异：

        - outcome 是 ``"approved"``（自动通过）而非 ``"rejected"``（fail-closed）
        - ``metadata.source`` 是 ``"rule_auto_allow"``（让 request() 反推 reason 为
          ``"auto_allowed"``，给 EventSink 区分自动通过路径 / 触发 audit 写入）

        被 cancel 时（``task.cancel()`` 由 :meth:`_cleanup_pending` 三退出路径触发）
        静默退出，不动 future。

        **race fail-safe**：sleep 醒来后必查 ``pending.future.done()``——如果用户
        在最后一刻手动 resolve / cancel / 上一秒 timeout 触发，future 已经被 set，
        本方法直接 return 不再 set_result（避免 ``InvalidStateError``）。

        Args:
            request_id: pending 的 ID
            delay_seconds: 等待秒数（``rule_dec.auto_approve_at_ms - now_ms``）
        """
        try:
            await asyncio.sleep(delay_seconds)
        except asyncio.CancelledError:
            return
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            return
        pending.future.set_result(
            ApprovalDecision(
                outcome="approved",
                metadata={
                    "source": "rule_auto_allow",
                    "reason": "auto_allow",
                    "matched_rule": pending.matched_rule,
                },
            )
        )

    async def _cleanup_pending(self, request_id: str, *, fan_out_reason: str | None) -> None:
        """三退出路径统一清理（R10）：pop ``_pending`` + cancel 并 pop
        ``_timeout_tasks`` / ``_auto_approve_tasks``。

        Args:
            request_id: pending ID
            fan_out_reason: 非 None 时调 ``emit_approval_removed`` fan-out；
                None 时仅清理不广播
        """
        async with self._lock:
            self._pending.pop(request_id, None)
            timeout_task = self._timeout_tasks.pop(request_id, None)
            auto_approve_task = self._auto_approve_tasks.pop(request_id, None)
            sinks_snapshot = list(self._event_sinks)
        if timeout_task and not timeout_task.done():
            timeout_task.cancel()
        if auto_approve_task and not auto_approve_task.done():
            auto_approve_task.cancel()
        if fan_out_reason is not None:
            await asyncio.gather(
                *(
                    s.emit_approval_removed(request_id=request_id, reason=fan_out_reason)
                    for s in sinks_snapshot
                ),
                return_exceptions=True,
            )


# ---------------------------------------------------------------------------
# 模块级单例
# ---------------------------------------------------------------------------


_singleton: ApprovalManager | None = None


def get_approval_manager(
    *,
    rules: ApprovalRules | None = None,
    event_sinks: list[ApprovalEventSink] | None = None,
    default_timeout_ms: int = 60_000,
) -> ApprovalManager:
    """获取或创建审批管理器单例。

    首次调用必须传 ``rules``（懒构造）；之后调用忽略所有参数，
    返回已构造实例。装配点（``src/hosts/web/run.py`` 等）首次调用时初始化；
    其余只读。

    Args:
        rules: 首次调用时使用；None 时构造空骨架 ApprovalRules
        event_sinks: 首次调用时使用
        default_timeout_ms: 首次调用时使用

    Returns:
        进程级单例 ApprovalManager
    """
    global _singleton
    if _singleton is None:
        if rules is None:
            # approval-rules-unified：ApprovalRules 不再持 default_timeout_ms，
            # 走失败关闭默认 60s（policy=None 时安全网）。manager 自己的
            # default_timeout_ms 仍按入参生效。
            rules = ApprovalRules()
        _singleton = ApprovalManager(
            rules=rules,
            event_sinks=event_sinks,
            default_timeout_ms=default_timeout_ms,
        )
    return _singleton


def reset_for_testing() -> None:
    """**仅测试用**：重置单例到 None；测试 fixture 在 setup/teardown 用。"""
    global _singleton
    _singleton = None


# ---------------------------------------------------------------------------
# make_manager_prompt_fn 工厂（评审必修 #2）
# ---------------------------------------------------------------------------


def make_manager_prompt_fn(
    manager: ApprovalManager,
    thread_id: str,
    *,
    channel: str = "generic_chat",
    default_cwd: str = "",
) -> Callable[[ApprovalRequest], Awaitable[ApprovalAction]]:
    """生成提示函数，内部调 ``manager.request``；返回值映射成 :class:`ApprovalAction`。

    替代现有 :meth:`web.app_support.host_adapter.WebHostAdapter.prompt_approval`
    （推 per-thread WS 旧模态）。``src/hosts/web/run.py`` 装配点用法：

    .. code-block:: python

        prompt_fn = make_manager_prompt_fn(manager, thread_id, default_cwd=resolved_cwd)
        approval = build_default_approval(mode, prompt_fn=prompt_fn)

    二态映射(reviewer 必修 #2，阶段 1):

    - ``decision.outcome="approved"`` → ``ApprovalAction.ACCEPT_ONCE``
    - ``decision.outcome="rejected"`` → ``ApprovalAction.REJECT``
    - ``decision.metadata["remember_for_session"]=True`` →
      ``ApprovalAction.ACCEPT_FOR_SESSION``

      .. warning::
         阶段 1 generic_chat 前端 Card 已隐藏「本 session」按钮，
         理论上不会触发此分支；留 TODO 标 stage 5 协议演进时接 GrantStore.put_session

    CLI 通道的本 session 授权已由 :class:`ApprovalRules` 的线程级覆盖接管。
    CLI 提示函数遇到 ``remember_for_session`` 时返回 ``ACCEPT_ONCE``，让后续
    调用继续进入审批管理器规则层，保证阻断规则每次都有机会先判定。

    返回的提示函数通过 ``__action_aware__`` 属性自动被
    :class:`tools.runtime.approval.InteractiveApproval` 识别为动作感知函数
    （返回 ApprovalAction）。

    工作目录解析优先级（thread-cwd-fallback 任务 #3）:

    1. ``req.metadata["cwd"]``：运行时显式传入，优先级最高
    2. ``default_cwd``：装配时由调用方解析后传入的兜底值（通常是 thread.cwd
       或 server 启动目录 ``app.state.workspace_root``）
    3. ``""``：两者都空时落到空字符串（与改前行为一致，兼容老调用方）

    Args:
        manager: ApprovalManager 单例
        thread_id: 该提示函数绑定的 thread（多 thread 一个 thread 一个提示函数）
        channel: 通道名（默认 ``"generic_chat"``，阶段 2 改 ``"claude_code"``）
        default_cwd: thread.cwd 空时的兜底工作目录（由装配点解析传入；
            通常是 server 启动目录 ``app.state.workspace_root``）；
            空字符串 = 不启用兜底（保持原行为，兼容旧调用方）

    Returns:
        动作感知提示函数，签名 ``(ApprovalRequest) -> Awaitable[ApprovalAction]``
    """

    async def prompt_fn(req: ApprovalRequest) -> ApprovalAction:
        """从 ApprovalRequest 提取信息调 manager.request，映射回 ApprovalAction。"""
        # 工作目录优先级：req.metadata.cwd（运行时显式传） > default_cwd（装配时 thread.cwd 解析后的值）
        req_cwd = (req.metadata or {}).get("cwd", "")
        resolved_cwd = req_cwd if req_cwd else default_cwd
        request_metadata = dict(req.metadata) if req.metadata else {}
        request_metadata.setdefault("run_id", req.run_id)
        request_metadata.setdefault("session_id", req.session_id)
        request_metadata.setdefault("turn", req.turn)
        request_metadata.setdefault("call_id", req.call_id)
        if req.reason:
            request_metadata.setdefault("reason", req.reason)
        decision = await manager.request(
            channel=channel,
            thread_id=thread_id,
            cwd=resolved_cwd,
            tool_name=req.tool_name,
            tool_input=dict(req.arguments),
            metadata=request_metadata,
        )
        return _decision_to_action(
            decision,
            allow_session_action=channel != "cli",
        )

    # 标记为动作感知函数（让 InteractiveApproval 走 ApprovalAction 分支）
    prompt_fn.__action_aware__ = True  # type: ignore[attr-defined]
    return prompt_fn


def _decision_to_action(
    decision: ApprovalDecision,
    *,
    allow_session_action: bool = True,
) -> ApprovalAction:
    """ApprovalDecision → ApprovalAction 二态映射（reviewer 必修 #2）。

    阶段 1 仅二态（ACCEPT_ONCE / REJECT）；ACCEPT_FOR_SESSION 受协议帧 v0.5
    支持后才能在 generic_chat 启用（spec 40-protocol-evolution）。

    Args:
        decision: manager.request 返回的 ApprovalDecision

    Returns:
        对应的 ApprovalAction；未知 outcome 一律映射为 REJECT（失败关闭）
    """
    if decision.outcome == "approved":
        # TODO(stage 5)：协议帧 v0.5 上线后，按 decision.metadata.remember_for_session
        # 返回 ACCEPT_FOR_SESSION（接 GrantStore.put_session）
        if decision.metadata.get("remember_for_session") and allow_session_action:
            return ApprovalAction.ACCEPT_FOR_SESSION
        if decision.metadata.get("remember_persistent"):
            return ApprovalAction.ACCEPT_PERSIST
        return ApprovalAction.ACCEPT_ONCE
    return ApprovalAction.REJECT


def _pending_to_view(
    pending: _PendingApproval,
    *,
    default_timeout_ms: int,
) -> PendingApprovalView:
    """把 manager 内部 pending 状态投影成公开只读视图。"""
    return PendingApprovalView(
        request_id=pending.request_id,
        channel=pending.channel,
        thread_id=pending.thread_id,
        cwd=pending.cwd,
        tool_name=pending.tool_name,
        tool_input=pending.tool_input,
        metadata=pending.metadata,
        severity=pending.severity,
        matched_rule=pending.matched_rule,
        auto_approve_at_ms=pending.auto_approve_at_ms,
        auto_reject_at_ms=pending.auto_reject_at_ms,
        arrived_at_ms=pending.arrived_at_ms,
        timeout_ms=pending.timeout_ms if pending.timeout_ms is not None else default_timeout_ms,
    )


__all__ = [
    "ApprovalEventSink",
    "ApprovalManager",
    "PendingApprovalView",
    "_decision_to_action",
    "get_approval_manager",
    "make_manager_prompt_fn",
    "reset_for_testing",
]
