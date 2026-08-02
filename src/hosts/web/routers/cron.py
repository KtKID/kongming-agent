"""Cron task / run REST 路由（web-cron-router-v0.1）。

端点：

- ``GET    /api/cron/tasks``                     — 列出所有 cron 任务（含 disabled）
- ``GET    /api/cron/runs?limit=&cursor=``       — 跨任务最近 N 条 run（cursor 分页）
- ``GET    /api/cron/tasks/{task_id}/runs``      — 单任务最近 run
- ``POST   /api/cron/tasks/{task_id}/pause``     — 生命周期转 PAUSED
- ``POST   /api/cron/tasks/{task_id}/resume``    — 生命周期转 SCHEDULED 并重算 next_run_at
- ``DELETE /api/cron/tasks/{task_id}``           — 物理删除
- ``POST   /api/cron/tasks/{task_id}/run_now``   — 异步试运行（写独立请求；ticker 抢 reservation）

依赖注入约定：

- ``app.state.scheduler_manager``：由 :func:`web.app.create_app` 在 lifespan 启动
  ticker 时挂入；任务查询、投影和状态写入统一经过该门户。
- 全局 :class:`web.auth.middleware.AuthMiddleware` 已挡掉未带合法 cookie 的请求；本路由不再
  显式 :func:`Depends`。

设计要点：

- ``run_now`` 通过 ``manual_run_requested_at`` 持久化试运行请求，让 lifespan ticker
  下一秒抢 reservation 并触发；正式 ``next_run_at`` 保持原日程。接口立即返回
  ``202`` + 占位 ``run_id``。前端通过 ``/ws/cron`` 收 ``cron.run.completed``
  拿真实 ``run_id``。
- ``resume`` 通过 :func:`scheduler.timing.compute_first_run_at` 重算 ``next_run_at``；
  ``ONCE`` trigger 已过期会抛 ``ValueError`` → 翻成 ``400``（语义：已完成的一次性
  任务不可恢复）。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request

from hosts.web.app_support.generic_history import normalize_generic_history
from hosts.web.app_support.llm_protocol import NormalizedMessage
from hosts.web.protocol.rest_models import (
    CreateCronTaskRequest,
    CronRunDTO,
    CronRunMessagesResponse,
    CronRunsPage,
    CronTaskDTO,
    RunNowResponse,
    UpdateCronTaskRequest,
)
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from scheduler.domain import (
    ConcurrencyPolicy,
    DeliveryChannel,
    RunStatus,
    ScheduleDelivery,
    ScheduledTask,
    TaskExecutionPolicy,
    TaskLifecycleState,
    TaskOrigin,
    TaskTarget,
    TriggerType,
)
from scheduler.manager import SchedulerManager, SchedulerTaskProjection
from scheduler.schedule_parser import parse_schedule
from scheduler.store import ManualRunPendingError, TaskNotFoundError
from scheduler.timing import compute_first_run_at, to_iso, utc_now

if TYPE_CHECKING:
    from scheduler.domain import ScheduledRun, ScheduledTask
    from scheduler.store import Store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cron", tags=["cron"])


_RUNS_GLOBAL_LIMIT_MAX = 200
_RUNS_PER_TASK_LIMIT_MAX = 200


# ---------------------------------------------------------------------------
# Domain → DTO
# ---------------------------------------------------------------------------


def _projection_to_dto(projection: SchedulerTaskProjection) -> CronTaskDTO:
    """把 SchedulerManager 的正交 task 投影转换为 REST DTO。"""
    task = projection.task
    return CronTaskDTO(
        task_id=task.task_id,
        name=task.name,
        lifecycle=task.lifecycle.value,
        latest_run_status=(
            projection.latest_run_status.value if projection.latest_run_status is not None else None
        ),
        live_runtime_status=projection.live_runtime_status.value,
        trigger_type=task.trigger.trigger_type.value,
        trigger_expr=task.trigger.expr,
        timezone=task.trigger.timezone,
        next_run_at=task.next_run_at,
        last_run_at=task.last_run_at,
        thread_id=task.thread_id,
        preset_id=task.preset_id,
        created_by=task.created_by,
        # v0.5.4: 透出 target 嵌套字段供前端编辑回填
        input_text=task.target.input_text,
        agent_name=task.target.agent_name,
    )


def _task_to_dto(manager: SchedulerManager, task: ScheduledTask) -> CronTaskDTO:
    """经共享 SchedulerManager 投影单个 task。"""
    return _projection_to_dto(manager.project_task(task))


def _run_to_dto(run: ScheduledRun, *, task_name: str, thread_id: str) -> CronRunDTO:
    return CronRunDTO(
        run_id=run.run_id,
        task_id=run.task_id,
        task_name=task_name,
        session_id=run.session_id or "",
        thread_id=run.thread_id or thread_id,
        scheduled_for=run.scheduled_for,
        started_at=run.started_at,
        finished_at=run.finished_at,
        status=run.status.value,
        failure_reason=run.failure_reason.value if run.failure_reason is not None else None,
        final_message_excerpt=run.final_message_excerpt,
        delivery_status=run.delivery_status.value,
        delivery_error=run.delivery_error,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _require_manager(request: Request) -> SchedulerManager:
    """从 ``app.state`` 取共享 :class:`SchedulerManager`；缺失时 503。

    生产路径在 lifespan 装配共享 Manager。测试若只注入 scheduler_store，
    本 helper 会创建一次并缓存到 app.state，后续请求继续复用同一门户。
    """
    manager: SchedulerManager | None = getattr(
        request.app.state,
        "scheduler_manager",
        None,
    )
    if manager is not None:
        return manager
    store: Store | None = getattr(request.app.state, "scheduler_store", None)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="scheduler not configured (scheduler_store missing)",
        )
    manager = SchedulerManager(
        store,
        thread_provisioner=getattr(request.app.state, "thread_manager", None),
    )
    request.app.state.scheduler_manager = manager
    return manager


def _require_config(request: Request) -> Any:
    cfg = getattr(request.app.state, "config", None)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail="app config not configured",
        )
    return cfg


def _require_task(manager: SchedulerManager, task_id: str) -> ScheduledTask:
    task = manager.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return task


def _require_run(
    manager: SchedulerManager,
    task_id: str,
    run_id: str,
) -> ScheduledRun:
    for run in manager.list_runs(task_id, limit=None):
        if run.run_id == run_id:
            return run
    raise HTTPException(status_code=404, detail=f"run not found: {run_id}")


def _resolve_thread_preset_id(request: Request, task_preset_id: str) -> str:
    """解析专属 thread 使用的 preset id。

    cron task 自身的 ``preset_id`` 保持请求语义：调用方未传时为空串，执行时
    继续走默认 provider。专属 generic_chat thread 仍需要一个 preset 才能在
    用户打开历史后继续对话，因此这里仅为 thread 创建解析默认 Web preset。
    """
    candidate = task_preset_id.strip()
    if candidate:
        return candidate
    cfg = _require_config(request)
    raw_catalog_manager = getattr(request.app.state, "model_catalog_manager", None)
    if not isinstance(raw_catalog_manager, ModelCatalogManager):
        raise HTTPException(status_code=503, detail="model catalog not configured")
    return raw_catalog_manager.resolve_runtime(cfg.model).preset_id


# ---------------------------------------------------------------------------
# POST: create task
# ---------------------------------------------------------------------------


@router.post("/tasks", status_code=201)
async def create_cron_task(request: Request, body: CreateCronTaskRequest) -> CronTaskDTO:
    """创建定时任务（结构化 REST 入口）。

    流程：
    1. 校验 schedule_type + 对应字段（once→once_at, cron→cron_expr）
    2. 用 ``parse_schedule`` 解析触发表达式 → ``ScheduleTrigger``
    3. 用 ``compute_first_run_at`` 算首次触发时刻
    4. 构造 ``ScheduledTask`` 并经 ``SchedulerManager`` 创建
    """
    manager = _require_manager(request)

    # 校验 schedule_type + 对应字段
    if body.schedule_type == "once":
        if not body.once_at:
            raise HTTPException(
                status_code=422,
                detail="once_at is required when schedule_type=once",
            )
        schedule_text = body.once_at
    elif body.schedule_type == "cron":
        if not body.cron_expr:
            raise HTTPException(
                status_code=422,
                detail="cron_expr is required when schedule_type=cron",
            )
        schedule_text = body.cron_expr
    else:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported schedule_type: {body.schedule_type!r}",
        )

    # 解析触发表达式
    try:
        trigger = parse_schedule(schedule_text, default_tz=body.timezone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 校验并发策略
    try:
        concurrency = ConcurrencyPolicy(body.concurrency_policy)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"invalid concurrency_policy: {body.concurrency_policy!r}",
        ) from None

    # 计算首次触发时刻
    try:
        next_run_at = compute_first_run_at(trigger)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    now_iso = to_iso(utc_now())
    task_id = uuid.uuid4().hex[:16]
    task_preset_id = (body.preset_id or "").strip()
    thread_preset_id = _resolve_thread_preset_id(request, task_preset_id)

    try:
        task = ScheduledTask(
            task_id=task_id,
            name=body.name,
            lifecycle=TaskLifecycleState.SCHEDULED,
            origin=TaskOrigin.WEB,
            trigger=trigger,
            policy=TaskExecutionPolicy(concurrency_policy=concurrency),
            target=TaskTarget(
                agent_name=body.agent_name,
                input_text=body.input_text,
            ),
            next_run_at=next_run_at,
            last_run_at=None,
            created_by="web",
            created_at=now_iso,
            updated_at=now_iso,
            delivery=ScheduleDelivery(channel=DeliveryChannel.WEB),
            preset_id=task_preset_id,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        created = await manager.create_task_with_thread(
            task,
            thread_preset_id=thread_preset_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    manager.append_audit(
        action="create",
        task_id=created.task_id,
        actor="web",
        payload={
            "source": "cron_router",
            "preset_id": created.preset_id or "",
            "thread_id": created.thread_id,
            "trigger_type": created.trigger.trigger_type.value,
        },
    )
    return _task_to_dto(manager, created)


# ---------------------------------------------------------------------------
# PATCH: edit task（v0.5.3 cron-task-edit）
# ---------------------------------------------------------------------------


@router.patch("/tasks/{task_id}", status_code=200)
async def update_cron_task(
    request: Request, task_id: str, body: UpdateCronTaskRequest
) -> CronTaskDTO:
    """编辑现有 cron task。

    只允许改 "可变字段"（name / schedule / agent / input_text / preset_id /
    lifecycle / concurrency_policy）。其他字段（origin / created_by / created_at /
    trigger.timezone 等）不在本端点暴露。

    边界：
    - task 不存在 → 404
    - 入参全部为 None → 200 直接回当前 task（语义：无操作，不算错误）
    - schedule 解析失败 → 422
    - lifecycle 取值非法由协议层返回 422

    实现：先 ``get_task`` 拿现状，按映射构造 ``fields_to_update`` dict 传给
    :meth:`Store.update_task`。嵌套 dataclass（``target`` / ``policy``）走
    :func:`dataclasses.replace` 局部覆盖，整体回写；store 层 ``replace(t, **payload)``
    会消费整体字段。
    """
    manager = _require_manager(request)
    task = _require_task(manager, task_id)

    fields_to_update: dict[str, Any] = {}
    changed: list[str] = []

    if body.name is not None:
        fields_to_update["name"] = body.name
        changed.append("name")

    if body.preset_id is not None:
        fields_to_update["preset_id"] = body.preset_id
        changed.append("preset_id")

    # target 嵌套字段：agent / input_text 共用同一份 TaskTarget 重建
    if body.agent is not None or body.input_text is not None:
        new_target = replace(
            task.target,
            agent_name=body.agent if body.agent is not None else task.target.agent_name,
            input_text=(body.input_text if body.input_text is not None else task.target.input_text),
        )
        fields_to_update["target"] = new_target
        if body.agent is not None:
            changed.append("agent")
        if body.input_text is not None:
            changed.append("input_text")

    # policy 嵌套字段：concurrency_policy 重建 TaskExecutionPolicy
    if body.concurrency_policy is not None:
        try:
            concurrency = ConcurrencyPolicy(body.concurrency_policy)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"invalid concurrency_policy: {body.concurrency_policy!r}",
            ) from exc
        new_policy = replace(task.policy, concurrency_policy=concurrency)
        fields_to_update["policy"] = new_policy
        changed.append("concurrency_policy")

    # schedule：重算 trigger + next_run_at
    new_next_run_at: str | None = None
    if body.schedule is not None:
        try:
            new_trigger = parse_schedule(body.schedule, default_tz=task.trigger.timezone)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            new_next_run_at = compute_first_run_at(new_trigger)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        fields_to_update["trigger"] = new_trigger
        fields_to_update["next_run_at"] = new_next_run_at
        changed.append("schedule")

    if body.lifecycle is not None:
        fields_to_update["lifecycle"] = TaskLifecycleState(body.lifecycle)
        changed.append("lifecycle")

    # exhausted one-shot 改 schedule 时开启新生命周期；历史完成时间清空，
    # 下一次 terminal run 再写入 last_run_at。
    if (
        "trigger" in fields_to_update
        and fields_to_update["trigger"].trigger_type == TriggerType.ONCE
        and task.lifecycle is TaskLifecycleState.EXHAUSTED
    ):
        fields_to_update["lifecycle"] = TaskLifecycleState.SCHEDULED
        fields_to_update["last_run_at"] = None
        if "revived_once" not in changed:
            changed.append("revived_once")

    if not fields_to_update:
        # 入参全空：幂等返回当前 task，不写 audit
        return _task_to_dto(manager, task)

    try:
        updated = manager.update_task(
            task_id,
            fields_to_update=fields_to_update,
        )
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}") from None
    except (ValueError, TypeError) as exc:
        # dataclass __post_init__ 触发的不变量违例
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    manager.append_audit(
        action="update",
        task_id=task_id,
        actor="web",
        payload={
            "source": "cron_router",
            "changed_fields": changed,
            **({"new_next_run_at": new_next_run_at} if new_next_run_at is not None else {}),
        },
    )
    return _task_to_dto(manager, updated)


# ---------------------------------------------------------------------------
# GET: single task detail
# ---------------------------------------------------------------------------


@router.get("/tasks/{task_id}")
async def get_cron_task(request: Request, task_id: str) -> CronTaskDTO:
    """获取单个 cron 任务详情。"""
    manager = _require_manager(request)
    task = _require_task(manager, task_id)
    return _task_to_dto(manager, task)


# ---------------------------------------------------------------------------
# GET endpoints
# ---------------------------------------------------------------------------


@router.get("/tasks")
async def list_cron_tasks(request: Request) -> list[CronTaskDTO]:
    """列出所有 cron 任务（含 disabled）。"""
    manager = _require_manager(request)
    return [_projection_to_dto(projection) for projection in manager.list_task_projections()]


@router.get("/runs")
async def list_cron_runs_global(
    request: Request,
    limit: int = Query(default=50, ge=1, le=_RUNS_GLOBAL_LIMIT_MAX),
    cursor: str | None = Query(default=None),
) -> CronRunsPage:
    """跨任务最近 N 条 run（``started_at`` 倒序）。

    分页：``cursor`` 为上一页最后一条的 ``started_at``；本页只返 ``started_at``
    严格早于 ``cursor`` 的 run。``next_cursor`` 为本页最后一条的 ``started_at``，
    前端原样回传即可。
    """
    manager = _require_manager(request)

    # cursor 空串归一化（FastAPI 对 ``?cursor=`` 传 "" 而非 None）
    effective_cursor = cursor if cursor else None
    runs = manager.list_recent_runs(limit=limit, cursor=effective_cursor)

    # 注入 task_name（一次性建表，避免 N+1）
    tasks_by_id = {
        projection.task.task_id: projection.task for projection in manager.list_task_projections()
    }
    dto_list = [
        _run_to_dto(
            r,
            task_name=(tasks_by_id[r.task_id].name if r.task_id in tasks_by_id else r.task_id),
            thread_id=(tasks_by_id[r.task_id].thread_id if r.task_id in tasks_by_id else ""),
        )
        for r in runs
    ]

    next_cursor: str | None = None
    if dto_list and len(dto_list) >= limit:
        # 仅当本页装满时才暗示可能还有下一页；语义保守
        last_started = dto_list[-1].started_at
        if last_started:
            next_cursor = last_started

    return CronRunsPage(runs=dto_list, next_cursor=next_cursor)


@router.get("/tasks/{task_id}/runs")
async def list_cron_task_runs(
    request: Request,
    task_id: str,
    limit: int = Query(default=20, ge=1, le=_RUNS_PER_TASK_LIMIT_MAX),
) -> list[CronRunDTO]:
    """单任务最近 N 条 run（按落盘顺序，最旧→最新）。

    实现：直接调 :meth:`Store.list_runs(task_id, limit=N)`，复用其 superseded
    过滤；任务不存在抛 404。
    """
    manager = _require_manager(request)
    task = _require_task(manager, task_id)
    runs = manager.list_runs(task_id, limit=limit)
    return [_run_to_dto(r, task_name=task.name, thread_id=task.thread_id) for r in runs]


@router.get("/tasks/{task_id}/runs/{run_id}/messages")
async def get_cron_run_messages(
    request: Request,
    task_id: str,
    run_id: str,
) -> CronRunMessagesResponse:
    """读取单次 cron run 独立 session 历史。"""
    manager = _require_manager(request)
    task = _require_task(manager, task_id)
    run = _require_run(manager, task_id, run_id)
    if not run.session_id:
        raise HTTPException(status_code=404, detail=f"run session not found: {run_id}")

    cfg = _require_config(request)
    session_root = _resolve_session_root(request, str(cfg.session.file_store_path))
    session_file = session_root / run.session_id / f"{run.session_id}.jsonl"
    manifest_file = session_root / run.session_id / "manifest.json"
    if not session_file.is_file() or not manifest_file.is_file():
        raise HTTPException(status_code=404, detail=f"run session not found: {run.session_id}")

    del task
    history = _read_file_session_history(session_file)
    messages: list[NormalizedMessage] = normalize_generic_history(
        history,
        session_id=run.session_id,
    )
    return CronRunMessagesResponse(messages=messages)


def _read_file_session_history(session_file: Path) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    with open(session_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                message = record["message"]
            except (json.JSONDecodeError, KeyError, TypeError):
                logger.warning("cron run session history skipped malformed line: %s", session_file)
                continue
            if isinstance(message, dict):
                history.append(message)
    return history


def _resolve_session_root(request: Request, raw_path: str) -> Path:
    expanded = Path(raw_path).expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    parts = expanded.parts
    if parts and parts[0] == ".kongming":
        raw_home = getattr(request.app.state, "kongming_home", None)
        home = raw_home if isinstance(raw_home, Path) else Path.home() / ".kongming"
        suffix = Path(*parts[1:]) if len(parts) > 1 else Path()
        return (home / suffix).resolve()
    return expanded.resolve()


# ---------------------------------------------------------------------------
# POST / DELETE endpoints
# ---------------------------------------------------------------------------


@router.post("/tasks/{task_id}/pause")
async def pause_cron_task(request: Request, task_id: str) -> CronTaskDTO:
    """暂停 cron 任务（lifecycle=PAUSED）。

    幂等：已 PAUSED 的任务再 pause 仍返回当前状态（``update_task`` 不报错）。
    """
    manager = _require_manager(request)
    _require_task(manager, task_id)
    try:
        updated = manager.update_task(
            task_id,
            fields_to_update={"lifecycle": TaskLifecycleState.PAUSED},
        )
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}") from None
    manager.append_audit(
        action="pause",
        task_id=task_id,
        actor="web",
        payload={"source": "cron_router"},
    )
    return _task_to_dto(manager, updated)


@router.post("/tasks/{task_id}/resume")
async def resume_cron_task(request: Request, task_id: str) -> CronTaskDTO:
    """恢复 cron 任务。

    流程：
    1. 取 task 反查 trigger
    2. ``compute_first_run_at(trigger)`` 重算 ``next_run_at``
       （ONCE 已过期 → ValueError → 400）
    3. ``update_task`` 设 ``lifecycle=SCHEDULED, next_run_at=...``
    """
    manager = _require_manager(request)
    task = _require_task(manager, task_id)

    try:
        new_next = compute_first_run_at(task.trigger)
    except ValueError as exc:
        # ONCE 已过期 / cron 表达式损坏等 → 不可恢复
        raise HTTPException(
            status_code=400,
            detail=f"cannot recompute next_run_at: {exc}",
        ) from exc

    try:
        updated = manager.update_task(
            task_id,
            fields_to_update={
                "lifecycle": TaskLifecycleState.SCHEDULED,
                "next_run_at": new_next,
            },
        )
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}") from None
    manager.append_audit(
        action="resume",
        task_id=task_id,
        actor="web",
        payload={"source": "cron_router", "next_run_at": new_next},
    )
    return _task_to_dto(manager, updated)


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_cron_task(request: Request, task_id: str) -> None:
    """物理删除 cron 任务（含 ``scheduled_tasks.json`` 中的条目）。

    runs/{task_id}.jsonl 历史记录保留；不在本端点清理（审计需要）。
    """
    manager = _require_manager(request)
    _require_task(manager, task_id)
    deleted = manager.delete_task(task_id)
    if not deleted:
        # 极小竞态窗口：_require_task 之后被并发 delete；按 REST 语义返 404
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    manager.append_audit(
        action="delete",
        task_id=task_id,
        actor="web",
        payload={"source": "cron_router"},
    )


@router.post("/tasks/{task_id}/run_now", status_code=202)
async def run_now_cron_task(request: Request, task_id: str) -> RunNowResponse:
    """异步排入一次手动执行，同时保留任务的正式调度 cursor。

    409：当前最近一条 run 正在运行，或已有待领取的手动执行请求。

    返回占位 ``run_id``（``pending-<uuid>``）；真实 ``run_id`` 由 ticker 触发
    时由 execution bridge 生成，前端通过 ``/ws/cron`` 拿。
    """
    manager = _require_manager(request)
    _require_task(manager, task_id)

    # 409：已在跑；用最近一条 run 的 status 判断
    runs = manager.list_runs(task_id, limit=1)
    if runs and runs[-1].status is RunStatus.RUNNING:
        raise HTTPException(
            status_code=409,
            detail=f"task already running: {task_id}",
        )

    now_iso = to_iso(utc_now())
    try:
        manager.request_manual_run(task_id, requested_at=now_iso)
    except ManualRunPendingError:
        raise HTTPException(
            status_code=409,
            detail=f"manual run already pending: {task_id}",
        ) from None
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}") from None
    manager.append_audit(
        action="run_now",
        task_id=task_id,
        actor="web",
        payload={"source": "cron_router", "manual_run_requested_at": now_iso},
    )
    placeholder_run_id = f"pending-{uuid.uuid4().hex[:12]}"
    return RunNowResponse(run_id=placeholder_run_id, status="PENDING")


__all__ = [
    "CreateCronTaskRequest",
    "CronRunDTO",
    "CronRunMessagesResponse",
    "CronRunsPage",
    "CronTaskDTO",
    "RunNowResponse",
    "UpdateCronTaskRequest",
    "router",
]
