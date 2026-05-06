"""ScheduleTool：Agent 通过自然语言/cron 表达式管理定时任务。

v0.2 提供六个 action：

- ``create``：创建任务（自动解析自然语言/cron schedule 表达式）
- ``list``：列出所有任务（默认仅 enabled）
- ``pause`` / ``resume``：切换任务 enabled+state
- ``run_now``：立即触发一次（启动 fresh agent run）
- ``remove``：删除任务

任务通过后台 ticker 触发，每次触发会启动一个 fresh agent run；fresh run 内本工具
被装配层裁掉（防递归创建任务），裁剪规则在
:mod:`scheduler.execution_bridge` 的 ``_FilteredToolLookup``。

约束：
- 不直接 import :mod:`safety.*`（import-linter Contract 5）
- 错误格式（``ValueError``）→ ``ToolResult(ok=False)``
- ``run_now`` 走调用方注入的 ``runtime_factory_fn``，未注入时直接报错
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from typing import Any

from core.contracts import ToolContext, ToolResult
from scheduler.domain import (
    DEFAULT_INACTIVITY_TIMEOUT,
    ConcurrencyPolicy,
    DueTaskReservation,
    MisfirePolicy,
    ScheduledTask,
    SessionMode,
    TaskExecutionPolicy,
    TaskOrigin,
    TaskState,
    TaskTarget,
)
from scheduler.schedule_parser import parse_schedule
from scheduler.store import Store, TaskNotFoundError
from scheduler.timing import compute_first_run_at, to_iso, utc_now
from tools.base import BaseBuiltinTool

_THREAD_ID_RE = re.compile(r"^thread-[a-f0-9]{12}$")

# RuntimeFactoryFn: (store) -> (runtime, bridge)
# 留 Any 避免循环 import：scheduler.runtime_factory 装配 NativeRuntime 时
# 会反向触发 executors 导入链，本工具只在 run_now 路径需要。
RuntimeFactoryFn = Callable[[Store], tuple[Any, Any]]


class ScheduleTool(BaseBuiltinTool):
    """cron 定时任务管理工具。

    用户在对话中说"每天 9 点提醒我喝水"等需求时，agent 用本工具创建 / 查看 /
    暂停 / 删除任务。任务通过后台 ticker 触发，每次触发会启动一个 fresh agent
    run；fresh run 内本工具被装配层裁掉（防递归创建任务）。

    支持的 schedule 表达式：

    - ``every 10s`` / ``every 30m`` / ``every 2h`` / ``every 1d``：周期
    - ``10s`` / ``30m`` / ``2h`` / ``1d``：N 时间后一次性
    - ``0 9 * * *``：5 字段 cron
    - ``*/30 * * * * *``：6 字段 cron（首字段秒）
    - ``2026-05-03T09:00:00+08:00``：ISO8601 一次性
    """

    name = "schedule"
    description = (
        "创建、查看、暂停、恢复、立即触发、删除定时任务。"
        "schedule 字段支持自然语言（如 'every 30s' / '2h' / '0 9 * * *'）和 ISO8601 时间戳。"
        "create 时必须给 name / schedule / agent / input；list 不需要参数；"
        "pause / resume / run_now / remove 需要 task_id。"
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "pause", "resume", "run_now", "remove"],
                "description": "操作类型",
            },
            "name": {"type": "string", "description": "任务展示名（create 时必给）"},
            "schedule": {
                "type": "string",
                "description": (
                    "调度表达式：'every Ns/m/h/d' / 'Ns/m/h/d' / 5/6 字段 cron / ISO8601；"
                    "create 时必给"
                ),
            },
            "agent": {
                "type": "string",
                "description": "目标 agent 名（create 时必给，默认 'default'）",
            },
            "input": {"type": "string", "description": "给 agent 的 prompt（create 时必给）"},
            "task_id": {
                "type": "string",
                "description": "task_id（pause/resume/run_now/remove 必给）",
            },
            "concurrency": {
                "type": "string",
                "enum": ["forbid", "allow", "replace"],
                "default": "forbid",
                "description": "并发策略，默认 forbid",
            },
            "timezone": {"type": "string", "default": "UTC"},
            "preset": {
                "type": "string",
                "description": "LLM preset id（匹配 cfg.web.llm_presets[].id）；空串用全局默认",
            },
            "include_disabled": {
                "type": "boolean",
                "default": False,
                "description": "list 时是否包含 disabled 任务",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        store: Store,
        *,
        runtime_factory_fn: RuntimeFactoryFn | None = None,
        default_timezone: str = "UTC",
        default_delivery_channel: str = "web",
        default_preset_id: str = "",
    ) -> None:
        """v0.3 新增 ``default_timezone`` / ``default_delivery_channel`` 参数：

        - 用户在 ``cfg.scheduler.default_timezone`` 配置当前时区（如
          ``"Asia/Shanghai"``）；LLM 创建任务时不传 timezone 就用此默认。
          **不让 LLM 自己猜时区**（实战发现 LLM 常误填 UTC 导致 cron 时间偏 8h）。
        - ``default_delivery_channel`` 让 task 默认带 ``delivery=ScheduleDelivery
          (channel=...)``，否则 dispatcher 会因 ``task.delivery is None`` 直接
          SKIPPED（M3 路由分支 1）→ 整套投递链路在 LLM 创建任务路径下永远不通。

        v0.4 新增 ``default_preset_id``：LLM 创建任务时默认 preset（来自
        调用方配置）。空串表示用全局默认 model。
        """
        super().__init__()
        self._store = store
        self._runtime_factory = runtime_factory_fn
        self._default_timezone = default_timezone
        self._default_delivery_channel = default_delivery_channel
        self._default_preset_id = default_preset_id

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """主入口：按 action 分派。

        覆盖 :meth:`BaseBuiltinTool.execute` 以承载更细的错误结构（同时携带
        ``content`` 解释文本和 ``error_message`` 机器可读字段）。
        """
        if not isinstance(args, dict):
            return ToolResult(
                ok=False,
                content="invalid args: expected dict",
                error_message="invalid args type",
            )
        action = args.get("action")
        if not action:
            return ToolResult(
                ok=False,
                content="missing required 'action' field",
                error_message="missing action",
            )
        try:
            if action == "create":
                return await self._do_create(args, ctx)
            if action == "list":
                return self._do_list(args)
            if action == "pause":
                return self._do_pause(args, ctx)
            if action == "resume":
                return self._do_resume(args, ctx)
            if action == "run_now":
                return await self._do_run_now(args, ctx)
            if action == "remove":
                return self._do_remove(args, ctx)
            return ToolResult(
                ok=False,
                content=f"unknown action: {action}",
                error_message="invalid action",
            )
        except ValueError as exc:
            return ToolResult(
                ok=False,
                content=f"invalid argument: {exc}",
                error_message=str(exc),
            )
        except TaskNotFoundError as exc:
            return ToolResult(
                ok=False,
                content=f"task not found: {exc}",
                error_message="task_not_found",
            )
        except Exception as exc:  # pragma: no cover - 防御式
            return ToolResult(
                ok=False,
                content=f"schedule tool error: {exc}",
                error_message=str(exc),
            )

    # ------------------------------------------------------------------
    # action 分支
    # ------------------------------------------------------------------

    async def _do_create(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = self._require(args, "name")
        schedule_expr = self._require(args, "schedule")
        input_text = self._require(args, "input")
        agent_name = (args.get("agent") or "default").strip() or "default"
        # v0.4：从 ctx.session_id 推导 delivery target（thread 来源的任务自动
        # 回投到该 thread）。
        delivery_target = (
            f"thread:{ctx.session_id}" if _THREAD_ID_RE.match(ctx.session_id) else None
        )
        preset_id = args.get("preset", "") or self._default_preset_id
        # v0.3：timezone 走配置默认（cfg.scheduler.default_timezone），
        # 不让 LLM 凭空填 UTC 导致 cron 表达式时间偏移。
        timezone = args.get("timezone") or self._default_timezone
        concurrency_str = args.get("concurrency", "forbid")

        try:
            concurrency_policy = ConcurrencyPolicy(concurrency_str)
        except ValueError as exc:
            raise ValueError(
                f"invalid concurrency {concurrency_str!r}: must be forbid/allow/replace"
            ) from exc

        trigger = parse_schedule(schedule_expr, default_tz=timezone)

        # next_run_at：所有 trigger_type 都立即算出第一次触发时刻。
        # 历史实现把 recurring（INTERVAL/CRON/SECONDS）留空，导致
        # store.reserve_due_tasks 看到 next_run_at 为空时 continue，任务
        # 永不触发（v0.2.1 P0 bug）。改为统一走 compute_first_run_at，
        # ONCE 路径仍等价于直接返回 trigger.expr。
        try:
            next_run_at = compute_first_run_at(trigger)
        except ValueError as exc:
            return ToolResult(
                ok=False,
                content=f"无法计算第一次触发时刻：{exc}",
                error_message=str(exc),
            )

        task_id = f"task-{uuid.uuid4().hex[:12]}"
        # v0.3：默认填 delivery（dispatcher 看到 None 会 SKIPPED 整条投递链路）
        from scheduler.domain import DeliveryChannel, ScheduleDelivery

        try:
            channel = DeliveryChannel(self._default_delivery_channel)
        except ValueError:
            channel = DeliveryChannel.WEB
        delivery = ScheduleDelivery(channel=channel, target=delivery_target)

        now = to_iso(utc_now())
        task = ScheduledTask(
            task_id=task_id,
            name=name,
            enabled=True,
            state=TaskState.SCHEDULED,
            origin=TaskOrigin.TOOL,
            trigger=trigger,
            policy=TaskExecutionPolicy(
                session_mode=SessionMode.FRESH_SESSION,
                concurrency_policy=concurrency_policy,
                misfire_policy=MisfirePolicy.SKIP,
                max_turns=None,
                inactivity_timeout_seconds=DEFAULT_INACTIVITY_TIMEOUT,
                wall_timeout_seconds=None,
                retry_limit=0,
                silent_marker_enabled=True,
            ),
            target=TaskTarget(agent_name=agent_name, input_text=input_text, metadata={}),
            next_run_at=next_run_at,
            last_run_at=None,
            created_by=f"agent:{ctx.session_id}",
            created_at=now,
            updated_at=now,
            delivery=delivery,
            preset_id=preset_id,
        )
        self._store.create_task(task)
        self._store.append_audit(
            action="create",
            task_id=task_id,
            actor=f"agent:{ctx.session_id}",
            payload={
                "source": "schedule_tool",
                "name": name,
                "schedule": schedule_expr,
                "trigger_type": trigger.trigger_type.value,
                "agent": agent_name,
            },
        )
        content = (
            f"created task {task_id}\n"
            f"  name: {name}\n"
            f"  schedule: {schedule_expr} -> {trigger.trigger_type.value} {trigger.expr}\n"
            f"  agent: {agent_name}\n"
            f"  next_run: {next_run_at}\n"
        )
        return ToolResult(
            ok=True,
            content=content,
            data={
                "task_id": task_id,
                "trigger_type": trigger.trigger_type.value,
                "expr": trigger.expr,
                "next_run_at": next_run_at,
            },
        )

    def _do_list(self, args: dict[str, Any]) -> ToolResult:
        include_disabled = bool(args.get("include_disabled", False))
        tasks = self._store.list_tasks(include_disabled=include_disabled)
        if not tasks:
            return ToolResult(
                ok=True,
                content="(no tasks)",
                data={"count": 0, "tasks": []},
            )

        lines = []
        for t in tasks:
            marker = "[on]" if t.enabled else "[off]"
            name_disp = t.name if len(t.name) <= 40 else t.name[:37] + "..."
            expr_disp = t.trigger.expr if len(t.trigger.expr) <= 25 else t.trigger.expr[:22] + "..."
            lines.append(
                f"{marker} {t.task_id[:16]:<16}  {name_disp:<40}  "
                f"{t.trigger.trigger_type.value:<8} {expr_disp:<25} "
                f"next={t.next_run_at or '-'}"
            )

        return ToolResult(
            ok=True,
            content="\n".join(lines),
            data={
                "count": len(tasks),
                "tasks": [
                    {
                        "task_id": t.task_id,
                        "name": t.name,
                        "enabled": t.enabled,
                        "state": t.state.value,
                        "trigger_type": t.trigger.trigger_type.value,
                        "expr": t.trigger.expr,
                        "next_run_at": t.next_run_at,
                    }
                    for t in tasks
                ],
            },
        )

    def _do_pause(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        task_id = self._require(args, "task_id")
        task = self._store.get_task(task_id)
        if task is None:
            return ToolResult(
                ok=False,
                content=f"task {task_id} not found",
                error_message="task_not_found",
            )
        if not task.enabled and task.state is TaskState.PAUSED:
            return ToolResult(
                ok=True,
                content=f"task {task_id} already paused",
                data={"task_id": task_id, "enabled": False, "state": TaskState.PAUSED.value},
            )
        updated = self._store.update_task(
            task_id,
            enabled=False,
            state=TaskState.PAUSED,
        )
        self._store.append_audit(
            action="pause",
            task_id=task_id,
            actor=f"agent:{ctx.session_id}",
            payload={"source": "schedule_tool"},
        )
        return ToolResult(
            ok=True,
            content=f"paused task {task_id}",
            data={
                "task_id": task_id,
                "enabled": updated.enabled,
                "state": updated.state.value,
            },
        )

    def _do_resume(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        task_id = self._require(args, "task_id")
        task = self._store.get_task(task_id)
        if task is None:
            return ToolResult(
                ok=False,
                content=f"task {task_id} not found",
                error_message="task_not_found",
            )
        if task.enabled and task.state is TaskState.SCHEDULED:
            return ToolResult(
                ok=True,
                content=f"task {task_id} already running",
                data={"task_id": task_id, "enabled": True, "state": TaskState.SCHEDULED.value},
            )
        updated = self._store.update_task(
            task_id,
            enabled=True,
            state=TaskState.SCHEDULED,
        )
        self._store.append_audit(
            action="resume",
            task_id=task_id,
            actor=f"agent:{ctx.session_id}",
            payload={"source": "schedule_tool"},
        )
        return ToolResult(
            ok=True,
            content=f"resumed task {task_id}",
            data={
                "task_id": task_id,
                "enabled": updated.enabled,
                "state": updated.state.value,
            },
        )

    def _do_remove(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        task_id = self._require(args, "task_id")
        removed = self._store.delete_task(task_id)
        if not removed:
            return ToolResult(
                ok=False,
                content=f"task {task_id} not found",
                error_message="task_not_found",
            )
        self._store.append_audit(
            action="remove",
            task_id=task_id,
            actor=f"agent:{ctx.session_id}",
            payload={"source": "schedule_tool"},
        )
        return ToolResult(
            ok=True,
            content=f"removed task {task_id}",
            data={"task_id": task_id, "removed": True},
        )

    async def _do_run_now(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        task_id = self._require(args, "task_id")
        task = self._store.get_task(task_id)
        if task is None:
            return ToolResult(
                ok=False,
                content=f"task {task_id} not found",
                error_message="task_not_found",
            )

        if self._runtime_factory is None:
            return ToolResult(
                ok=False,
                content=(
                    "run_now 不可用：调用方未注入 runtime_factory（通常意味着 cli/web 装配遗漏）"
                ),
                error_message="runtime_factory_missing",
            )

        now_iso = to_iso(utc_now())
        reservation = DueTaskReservation(
            task=task,
            scheduled_for=task.next_run_at or now_iso,
            reserved_at=now_iso,
        )
        self._store.append_audit(
            action="run_now",
            task_id=task_id,
            actor=f"agent:{ctx.session_id}",
            payload={"source": "schedule_tool", "scheduled_for": reservation.scheduled_for},
        )
        runtime, bridge = self._runtime_factory(self._store)
        try:
            run = await bridge.execute(reservation)
        finally:
            aclose = getattr(runtime, "aclose", None)
            if aclose is not None:
                await aclose()

        return ToolResult(
            ok=True,
            content=f"run finished: {run.run_id} status={run.status.value}",
            data={
                "run_id": run.run_id,
                "status": run.status.value,
                "task_id": task_id,
            },
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _require(args: dict[str, Any], key: str) -> str:
        """要求 ``args[key]`` 是非空字符串，否则抛 ``ValueError``。"""
        value = args.get(key)
        if value is None:
            raise ValueError(f"missing required argument: {key!r}")
        if not isinstance(value, str):
            raise ValueError(f"argument {key!r} must be string, got {type(value).__name__}")
        if not value.strip():
            raise ValueError(f"argument {key!r} must be non-empty")
        return value


def build_schedule_tool(
    store: Store,
    *,
    runtime_factory_fn: RuntimeFactoryFn | None = None,
    default_timezone: str = "UTC",
    default_delivery_channel: str = "web",
    default_preset_id: str = "",
) -> ScheduleTool:
    """工厂：装配期由 ``register_schedule_tool_if_enabled`` 调用。

    Args:
        store: cron 模块的文件 :class:`Store`。
        runtime_factory_fn: 可选 callable，签名 ``(store) -> (runtime, bridge)``；
            ``run_now`` action 用。``None`` 时 ``run_now`` 直接报错。
        default_timezone: v0.3 新增。LLM 创建任务时默认 timezone（来自
            ``cfg.scheduler.default_timezone``）。**避免 LLM 凭空填 UTC**。
        default_delivery_channel: v0.3 新增。task 默认 delivery channel
            （来自 ``cfg.scheduler.default_delivery_channel``）。**避免 task
            带 ``delivery=None`` 让 dispatcher SKIPPED**。
        default_preset_id: v0.4 新增。task 默认 preset_id（来自调用方配置）。
            空串表示用全局默认 model。

    Returns:
        配置好的 :class:`ScheduleTool`。
    """
    return ScheduleTool(
        store=store,
        runtime_factory_fn=runtime_factory_fn,
        default_timezone=default_timezone,
        default_delivery_channel=default_delivery_channel,
        default_preset_id=default_preset_id,
    )


__all__ = ["ScheduleTool", "build_schedule_tool"]
