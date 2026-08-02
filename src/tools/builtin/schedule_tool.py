"""ScheduleTool：Agent 通过自然语言/cron 表达式管理定时任务。

v0.2 提供六个 action：

- ``create``：创建任务（自动解析自然语言/cron schedule 表达式）
- ``list``：列出所有任务（默认仅 enabled）
- ``pause`` / ``resume``：切换任务 enabled+state
- ``run_now``：立即触发一次（启动 fresh agent run）
- ``remove``：删除任务

任务通过后台 ticker 触发，每次触发会启动一个 fresh agent run；fresh run 内本工具
被装配层裁掉（防递归创建任务），裁剪规则在
:mod:`application.scheduled_runs.execution_bridge` 的 ``_FilteredToolLookup``。

约束：
- 不直接 import :mod:`safety.*`（import-linter Contract 5）
- 错误格式（``ValueError``）→ ``ToolResult(ok=False)``
- ``run_now`` 走调用方注入的 ``runtime_factory_fn``，未注入时直接报错
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from core.contracts import PreparedToolCall, ToolContext, ToolResult
from scheduler.domain import (
    DEFAULT_INACTIVITY_TIMEOUT,
    ApprovalMode,
    ConcurrencyPolicy,
    DeliveryChannel,
    DueTaskReservation,
    MisfirePolicy,
    ScheduleDelivery,
    ScheduledTask,
    ScheduleTrigger,
    SessionMode,
    TaskExecutionPolicy,
    TaskLifecycleState,
    TaskOrigin,
    TaskTarget,
    TriggerType,
)
from scheduler.manager import SchedulerManager, ScheduleThreadProvisioner
from scheduler.run_portal import ScheduledRunPortal
from scheduler.schedule_parser import parse_schedule
from scheduler.store import Store, TaskNotFoundError
from scheduler.timing import (
    compute_first_run_at,
    is_within_oneshot_grace,
    parse_iso,
    to_iso,
    utc_now,
)
from tools.runtime.base import BaseBuiltinTool

# RuntimeFactoryFn: (store) -> 进程内共享 scheduled_run_manager。
RuntimeFactoryFn = Callable[[Store], ScheduledRunPortal]


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
        "支持创建、查看、暂停、恢复、立即触发、删除的定时任务工具手册。"
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
                "description": "LLM preset id（匹配 model catalog）；空串用全局默认",
            },
            "approval_mode": {
                "type": "string",
                "enum": ["fail_closed", "trust"],
                "description": (
                    "任务级审批模式（v0.5）：fail_closed=审批必拒（除 write_file 白名单）；"
                    "trust=cron 信任模式自动放行 explicit_consent（hard_block 仍拒绝）。"
                    "缺省时沿用全局 cfg.scheduler.approval.mode（v0.5 默认 trust）。"
                ),
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
        thread_provisioner: ScheduleThreadProvisioner | None = None,
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
        self._thread_provisioner = thread_provisioner

    def prepare(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> PreparedToolCall:
        """审批前冻结 action 专属校验、默认值、trigger 与首次执行时间。"""
        del context
        self._validate_args(arguments)
        allowed = set(self.input_schema["properties"])
        unknown = sorted(set(arguments) - allowed)
        if unknown:
            raise ValueError(f"unknown arguments: {unknown}")
        action = arguments.get("action")
        if action not in {"create", "list", "pause", "resume", "run_now", "remove"}:
            raise ValueError("invalid action")
        if action == "list":
            include_disabled = arguments.get("include_disabled", False)
            if not isinstance(include_disabled, bool):
                raise ValueError("include_disabled must be a boolean")
            return PreparedToolCall(
                arguments={"action": action, "include_disabled": include_disabled}
            )
        if action != "create":
            return PreparedToolCall(
                arguments={
                    "action": action,
                    "task_id": self._require(arguments, "task_id").strip(),
                }
            )

        name = self._require(arguments, "name").strip()
        schedule_expr = self._require(arguments, "schedule").strip()
        input_text = self._require(arguments, "input")
        raw_agent = arguments.get("agent", "default")
        if raw_agent is None:
            raw_agent = "default"
        if not isinstance(raw_agent, str):
            raise ValueError("argument 'agent' must be string")
        agent_name = raw_agent.strip() or "default"
        raw_preset = arguments.get("preset", self._default_preset_id)
        if raw_preset is None or raw_preset == "":
            raw_preset = self._default_preset_id
        if not isinstance(raw_preset, str):
            raise ValueError("argument 'preset' must be string")
        raw_timezone = arguments.get("timezone", self._default_timezone)
        if raw_timezone is None or raw_timezone == "":
            raw_timezone = self._default_timezone
        if not isinstance(raw_timezone, str):
            raise ValueError("argument 'timezone' must be string")
        concurrency = arguments.get("concurrency", "forbid")
        if not isinstance(concurrency, str):
            raise ValueError("argument 'concurrency' must be string")
        try:
            concurrency_policy = ConcurrencyPolicy(concurrency)
        except ValueError as exc:
            raise ValueError(
                f"invalid concurrency {concurrency!r}: must be forbid/allow/replace"
            ) from exc
        raw_approval_mode = arguments.get("approval_mode")
        approval_mode: str | None = None
        if raw_approval_mode is not None:
            if not isinstance(raw_approval_mode, str):
                raise ValueError("argument 'approval_mode' must be string")
            try:
                approval_mode = ApprovalMode(raw_approval_mode).value
            except ValueError as exc:
                raise ValueError(
                    f"invalid approval_mode {raw_approval_mode!r}: must be fail_closed/trust"
                ) from exc
        trigger = parse_schedule(schedule_expr, default_tz=raw_timezone)
        next_run_at = compute_first_run_at(trigger)
        try:
            delivery_channel = DeliveryChannel(self._default_delivery_channel).value
        except ValueError:
            delivery_channel = DeliveryChannel.WEB.value
        return PreparedToolCall(
            arguments={
                "action": action,
                "name": name,
                "schedule": schedule_expr,
                "input": input_text,
                "agent": agent_name,
                "preset": raw_preset,
                "timezone": raw_timezone,
                "concurrency": concurrency_policy.value,
                "approval_mode": approval_mode,
                "trigger_type": trigger.trigger_type.value,
                "trigger_expr": trigger.expr,
                "trigger_timezone": trigger.timezone,
                "next_run_at": next_run_at,
                "delivery_channel": delivery_channel,
            }
        )

    async def execute(
        self,
        prepared: PreparedToolCall,
        ctx: ToolContext,
    ) -> ToolResult:
        """主入口：按 action 分派。

        覆盖 :meth:`BaseBuiltinTool.execute` 以承载更细的错误结构（同时携带
        ``content`` 解释文本和 ``error_message`` 机器可读字段）。
        """
        args = dict(prepared.arguments)
        action = args["action"]
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
        name = args["name"]
        schedule_expr = args["schedule"]
        input_text = args["input"]
        agent_name = args["agent"]
        preset_id = args["preset"]
        concurrency_policy = ConcurrencyPolicy(args["concurrency"])
        raw_approval_mode = args["approval_mode"]
        approval_mode = (
            ApprovalMode(raw_approval_mode) if isinstance(raw_approval_mode, str) else None
        )
        trigger = ScheduleTrigger(
            trigger_type=TriggerType(args["trigger_type"]),
            expr=args["trigger_expr"],
            timezone=args["trigger_timezone"],
        )
        next_run_at = args["next_run_at"]

        if trigger.trigger_type is TriggerType.ONCE:
            now_dt = utc_now()
            scheduled_for = parse_iso(next_run_at)
            if not is_within_oneshot_grace(scheduled_for, now_dt):
                return ToolResult(
                    ok=False,
                    content=f"schedule is in the past: {next_run_at} < {to_iso(now_dt)}",
                    error_message="schedule_in_past",
                )

        task_id = f"task-{uuid.uuid4().hex[:12]}"
        # v0.3：默认填 delivery（dispatcher 看到 None 会 SKIPPED 整条投递链路）
        channel = DeliveryChannel(args["delivery_channel"])
        delivery = ScheduleDelivery(channel=channel)

        now = to_iso(utc_now())
        task = ScheduledTask(
            task_id=task_id,
            name=name,
            lifecycle=TaskLifecycleState.SCHEDULED,
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
                approval_mode=approval_mode,  # v0.5：None 走全局
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
        if self._thread_provisioner is None:
            return ToolResult(
                ok=False,
                content="schedule create requires a scheduled-task thread provisioner",
                error_message="thread_provisioner_required",
            )
        manager = SchedulerManager(self._store, thread_provisioner=self._thread_provisioner)
        created = await manager.create_task_with_thread(task)
        self._store.append_audit(
            action="create",
            task_id=created.task_id,
            actor=f"agent:{ctx.session_id}",
            payload={
                "source": "schedule_tool",
                "name": name,
                "schedule": schedule_expr,
                "trigger_type": trigger.trigger_type.value,
                "agent": agent_name,
                "approval_mode": approval_mode.value if approval_mode else None,
                "preset_id": created.preset_id,
                "thread_id": created.thread_id,
            },
        )
        content = (
            f"created task {created.task_id}\n"
            f"  name: {name}\n"
            f"  schedule: {schedule_expr} -> {trigger.trigger_type.value} {trigger.expr}\n"
            f"  agent: {agent_name}\n"
            f"  next_run: {next_run_at}\n"
        )
        if created.thread_id:
            content += f"  thread_id: {created.thread_id}\n"
        if approval_mode is not None:
            content += f"  approval_mode: {approval_mode.value}\n"
        return ToolResult(
            ok=True,
            content=content,
            data={
                "task_id": created.task_id,
                "thread_id": created.thread_id,
                "trigger_type": trigger.trigger_type.value,
                "expr": trigger.expr,
                "next_run_at": next_run_at,
                "approval_mode": approval_mode.value if approval_mode else None,
            },
        )

    def _do_list(self, args: dict[str, Any]) -> ToolResult:
        include_disabled = args["include_disabled"]
        tasks = self._store.list_tasks(include_disabled=include_disabled)
        if not tasks:
            return ToolResult(
                ok=True,
                content="(no tasks)",
                data={"count": 0, "tasks": []},
            )

        lines = []
        for t in tasks:
            marker = f"[{t.lifecycle.value}]"
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
                        "lifecycle": t.lifecycle.value,
                        "trigger_type": t.trigger.trigger_type.value,
                        "expr": t.trigger.expr,
                        "next_run_at": t.next_run_at,
                        "approval_mode": (
                            t.policy.approval_mode.value if t.policy.approval_mode else None
                        ),
                    }
                    for t in tasks
                ],
            },
        )

    def _do_pause(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        task_id = args["task_id"]
        task = self._store.get_task(task_id)
        if task is None:
            return ToolResult(
                ok=False,
                content=f"task {task_id} not found",
                error_message="task_not_found",
            )
        if task.lifecycle is TaskLifecycleState.PAUSED:
            return ToolResult(
                ok=True,
                content=f"task {task_id} already paused",
                data={
                    "task_id": task_id,
                    "lifecycle": TaskLifecycleState.PAUSED.value,
                },
            )
        updated = self._store.update_task(
            task_id,
            lifecycle=TaskLifecycleState.PAUSED,
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
                "lifecycle": updated.lifecycle.value,
            },
        )

    def _do_resume(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        task_id = args["task_id"]
        task = self._store.get_task(task_id)
        if task is None:
            return ToolResult(
                ok=False,
                content=f"task {task_id} not found",
                error_message="task_not_found",
            )
        if task.lifecycle is TaskLifecycleState.SCHEDULED:
            return ToolResult(
                ok=True,
                content=f"task {task_id} already running",
                data={
                    "task_id": task_id,
                    "lifecycle": TaskLifecycleState.SCHEDULED.value,
                },
            )
        updated = self._store.update_task(
            task_id,
            lifecycle=TaskLifecycleState.SCHEDULED,
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
                "lifecycle": updated.lifecycle.value,
            },
        )

    def _do_remove(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        task_id = args["task_id"]
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
        task_id = args["task_id"]
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
        scheduled_run_manager = self._runtime_factory(self._store)
        receipt = await scheduled_run_manager.submit_scheduled_run(reservation)
        run = await scheduled_run_manager.wait_for_run(receipt.run_id)

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
    thread_provisioner: ScheduleThreadProvisioner | None = None,
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
        thread_provisioner: scheduled-task-thread 新增。存在时 create action 先
            provision 专属 thread，再写 task。

    Returns:
        配置好的 :class:`ScheduleTool`。
    """
    return ScheduleTool(
        store=store,
        runtime_factory_fn=runtime_factory_fn,
        default_timezone=default_timezone,
        default_delivery_channel=default_delivery_channel,
        default_preset_id=default_preset_id,
        thread_provisioner=thread_provisioner,
    )


__all__ = ["ScheduleTool", "build_schedule_tool"]
