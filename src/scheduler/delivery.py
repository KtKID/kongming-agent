"""scheduler v0.3 — cron 投递层（M3）。

cron run 跑完后，按 ``ScheduledTask.delivery.channel`` 把 ``final_message``
路由到对应渠道（M4 ``WebDeliverySink`` / M5 ``CliDeliverySink``）；
并把投递结果写回 :class:`scheduler.domain.ScheduledRun` 的
``delivered_at`` / ``delivery_status`` / ``delivery_error`` 三个 v0.3 字段。

设计决策（与 ``docs/cron-delivery-v0.3/`` 锁定）：

- **不用 Protocol**：硬编码 web / cli 两 channel 的 if/elif 路由，避免过度抽象。
  扩展新 channel（远端 push）时再考虑 Protocol；当前两个就 ``DeliverySink``
  ABC 足够。
- **silent_marker 重定义**：``ScheduledTask.policy.silent_marker_enabled=True``
  且 ``final_message.startswith('[SILENT]')`` → 跳过 sink 调用，状态填
  ``DeliveryStatus.SKIPPED``，``skip_reason='silent_marker'``。
- **投递失败不污染 run.status**：sink 抛异常 → catch 住记录 ``FAILED`` +
  ``delivery_error``，run.status 保持 agent 完成时的状态。

边界：

- 不直连 web / cli 模块；只通过 ``DeliverySink`` 抽象解耦
- 不写 jsonl / 不调 store —— 投递结果通过 :class:`DeliveryResult` 返回，
  由调用方（``ExecutionBridge``）合并到 ``ScheduledRun`` 后再 supersede 落盘
- 不做重试 / 退避：M3 简化范围；失败仅记录字段，重试留给 v0.4
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from scheduler.domain import (
    SILENT_MARKER,
    DeliveryChannel,
    DeliveryStatus,
    ScheduledRun,
    ScheduledTask,
)
from scheduler.timing import to_iso, utc_now


@dataclass(frozen=True)
class DeliveryResult:
    """单次投递的结果摘要；调用方据此更新 :class:`ScheduledRun` 字段。

    不变量（v0.3 起由 ``__post_init__`` 强制）：

    - ``status == DELIVERED`` → ``delivered_at`` 必非空
    - ``status == FAILED`` → ``error_message`` 必非空
    - ``status == SKIPPED`` → ``skip_reason`` 必非空（``silent_marker`` /
      ``no_sink_registered`` / ``no_delivery_config``）
    - ``status == PENDING`` → 不应作为投递返回值出现（仅用于
      :class:`scheduler.domain.ScheduledRun.delivery_status` 初始字段）
    """

    status: DeliveryStatus
    delivered_at: str | None = None
    error_message: str | None = None
    skip_reason: str | None = None

    def __post_init__(self) -> None:
        """v0.3 cron-delivery R2 加固：结构层强制不变量。

        防止 M4/M5 sink 实现者构造非法 DeliveryResult（如 DELIVERED 但
        delivered_at=None），从而把诡异状态写入 ScheduledRun 字段。
        """
        if not isinstance(self.status, DeliveryStatus):
            raise TypeError(f"status must be DeliveryStatus, got {type(self.status).__name__}")
        if self.status is DeliveryStatus.DELIVERED:
            if self.delivered_at is None:
                raise ValueError("DeliveryResult(status=DELIVERED) requires delivered_at")
        elif self.status is DeliveryStatus.FAILED:
            if not self.error_message:
                raise ValueError("DeliveryResult(status=FAILED) requires non-empty error_message")
        elif self.status is DeliveryStatus.SKIPPED:
            if not self.skip_reason:
                raise ValueError("DeliveryResult(status=SKIPPED) requires non-empty skip_reason")
        elif self.status is DeliveryStatus.PENDING:
            # PENDING 不应出现在投递返回值里；用于 ScheduledRun 初始默认值
            raise ValueError(
                "DeliveryResult(status=PENDING) is not a valid delivery outcome; "
                "PENDING is reserved for ScheduledRun.delivery_status default"
            )

    @classmethod
    def delivered(cls, *, at: str | None = None) -> DeliveryResult:
        """构造 DELIVERED 结果；``at`` 缺省取 ``utc_now`` 的 ISO 文本。"""
        return cls(
            status=DeliveryStatus.DELIVERED,
            delivered_at=at or to_iso(utc_now()),
        )

    @classmethod
    def failed(cls, error: str) -> DeliveryResult:
        return cls(status=DeliveryStatus.FAILED, error_message=error)

    @classmethod
    def skipped(cls, reason: str) -> DeliveryResult:
        return cls(status=DeliveryStatus.SKIPPED, skip_reason=reason)


class DeliverySink(ABC):
    """v0.3 cron 投递目标抽象。

    每个 channel 一个具体实现：

    - ``WebDeliverySink``（M4 in src/web/）：通过 WS 广播 + RunRecord 落盘
    - ``CliDeliverySink``（M5 in src/cli/）：buffer + REPL 提示符前 flush

    不用 :class:`typing.Protocol` 因为 ``DeliveryDispatcher`` 当前硬编码两
    channel 路由（spec 决策 2）；ABC 让"忘记实现 deliver"在装配时即抛错。
    """

    @abstractmethod
    async def deliver(
        self,
        task: ScheduledTask,
        run: ScheduledRun,
        final_message: str,
    ) -> DeliveryResult:
        """把 ``final_message`` 投递到本 sink 关联的 channel。

        实现要求：

        - 内部异常应**抛出**而非吞掉；``DeliveryDispatcher`` 会捕获并转 FAILED
        - 不要在本方法里写 ``ScheduledRun.delivery_*`` 字段（由 dispatcher /
          ExecutionBridge 统一合并）
        - 投递语义可异步（非阻塞主流程）；返回 DeliveryResult 时投递不必已完成
          物理动作（如 WS 广播可 fire-and-forget），但要保证 sink 内已经把
          数据交给底层运输层（store / WS broker / CLI buffer）
        """
        raise NotImplementedError


@runtime_checkable
class TargetDeliverySink(Protocol):
    """v0.4 cron-thread-preset：target-specific 投递协议。

    ``DeliverySink`` 做 channel 级广播（web / cli）；本 Protocol 做
    target 级定向投递（如把 cron 结果推进特定 thread）。

    ``deliver_to_target`` 返回 ``True`` 表示投递成功，``False`` 表示
    target 不可达 / 已关闭等非致命失败。异常则由 dispatcher 捕获
    记入 ``delivery_error``。
    """

    async def deliver_to_target(self, target: str, task_name: str, message: str) -> bool: ...


class DeliveryDispatcher:
    """v0.3 cron 投递路由器。

    装配阶段持有 ``web_sink`` / ``cli_sink``；运行阶段每次 cron run 完成
    调一次 :meth:`deliver`，按 ``task.delivery.channel`` 选 sink + 处理
    silent_marker / 无 sink / sink 异常等分支。

    调用约定（与 :class:`application.scheduled_runs.execution_bridge.ExecutionBridge` 协作）：

    1. 仅在 ``run.status`` 进入终态后调用（COMPLETED / FAILED / SILENT 等）
    2. 返回的 :class:`DeliveryResult` 由 bridge 合并到 ``ScheduledRun``，
       再走 ``supersede_and_append_run`` 落盘
    3. 不修改 task / run / store；纯投递路由
    """

    def __init__(
        self,
        *,
        web_sink: DeliverySink | None = None,
        cli_sink: DeliverySink | None = None,
        target_sink: TargetDeliverySink | None = None,
    ) -> None:
        self._web_sink = web_sink
        self._cli_sink = cli_sink
        self._target_sink = target_sink

    async def deliver(
        self,
        task: ScheduledTask,
        run: ScheduledRun,
        final_message: str,
    ) -> DeliveryResult:
        """按 task.delivery.channel 路由 + 处理边界情况。

        分支顺序（先到的优先返回，避免后续分支误判）：

        1. ``task.delivery is None`` → SKIPPED(reason=no_delivery_config)
           兼容 v0.2 任务（没显式配置 delivery）
        2. ``run.status is SILENT`` → SKIPPED(reason=silent_marker)
           **生产路径主判定**：cron 经 ExecutionBridge.execute → _classify_result
           已把 [SILENT] 前缀剥离写入 ``run.final_message_excerpt``；bridge 把
           **excerpt** 当 final_message 传给 dispatcher，所以 dispatcher 看到的
           final_message **没有** [SILENT] 前缀。silent 状态唯一可靠来源是
           ``run.status``。
        3. ``policy.silent_marker_enabled and final_message.startswith([SILENT])``
           → SKIPPED(reason=silent_marker)
           **fallback 路径**：仅在 dispatcher 被绕过 bridge 直接调用（如未来
           重用本路由器到非 cron 场景）时才命中；ExecutionBridge 链路下永远不命中
           （因前缀已剥离）。保留是为了让 dispatcher 自身契约一致——任何含
           [SILENT] 前缀的消息都不该被投递。
        4. 路由 channel：
           - WEB → web_sink；不存在 → SKIPPED(reason=no_sink_registered)
           - CLI → cli_sink；不存在 → SKIPPED(reason=no_sink_registered)
        5. 调 sink.deliver，捕获 ``Exception`` → FAILED(error)。
           **故意不接 BaseException**：``CancelledError`` / ``KeyboardInterrupt``
           应继续向上传播，让 ticker / REPL 层做正常 cancel 处理。
        """
        # 1) 未配置 delivery
        if task.delivery is None:
            return DeliveryResult.skipped("no_delivery_config")

        # 2 / 3) silent_marker 命中
        from scheduler.domain import RunStatus

        is_silent_by_status = run.status is RunStatus.SILENT
        is_silent_by_marker = task.policy.silent_marker_enabled and final_message.startswith(
            SILENT_MARKER
        )
        if is_silent_by_status or is_silent_by_marker:
            return DeliveryResult.skipped("silent_marker")

        # 4) 路由 channel
        sink = self._pick_sink(task.delivery.channel)
        if sink is None:
            return DeliveryResult.skipped("no_sink_registered")

        # 5) 调 sink；只接 Exception，BaseException（含 CancelledError）继续传播
        try:
            result = await sink.deliver(task, run, final_message)
        except Exception as exc:
            # type name 始终非空，DeliveryResult.failed 不会因 error_message 为空抛
            result = DeliveryResult.failed(f"{type(exc).__name__}: {exc}")

        # 6) v0.4 target 定向投递（附加，不覆盖 channel sink 结果）
        if (
            task.delivery.target  # None 和空串都跳过
            and self._target_sink is not None
        ):
            try:
                ok = await self._target_sink.deliver_to_target(
                    task.delivery.target, task.name, final_message
                )
                if not ok and result.status is not DeliveryStatus.FAILED:
                    # target 不可达但 channel sink 已成功：记 delivery_error 但不降级
                    result = DeliveryResult(
                        status=result.status,
                        delivered_at=result.delivered_at,
                        error_message=f"target_unreachable: {task.delivery.target}",
                        skip_reason=result.skip_reason,
                    )
            except Exception as exc:
                # target sink 异常不覆盖已有 channel 投递结果；仅追加 error
                if result.status is not DeliveryStatus.FAILED:
                    result = DeliveryResult(
                        status=result.status,
                        delivered_at=result.delivered_at,
                        error_message=f"target_{type(exc).__name__}: {exc}",
                        skip_reason=result.skip_reason,
                    )

        return result

    def _pick_sink(self, channel: DeliveryChannel) -> DeliverySink | None:
        if channel is DeliveryChannel.WEB:
            return self._web_sink
        if channel is DeliveryChannel.CLI:
            return self._cli_sink
        # DeliveryChannel 是 closed enum；理论上不会到这里。保留分支让 mypy
        # / 后续新增 channel 时 fail loud（spec 决策 2 阻止默默扩展）。
        return None


__all__ = [
    "DeliveryDispatcher",
    "DeliveryResult",
    "DeliverySink",
    "TargetDeliverySink",
]
