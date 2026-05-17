"""Cron task / run REST 路由（web-cron-router-v0.1）。

端点：

- ``GET    /api/cron/tasks``                     — 列出所有 cron 任务（含 disabled）
- ``GET    /api/cron/runs?limit=&cursor=``       — 跨任务最近 N 条 run（cursor 分页）
- ``GET    /api/cron/tasks/{task_id}/runs``      — 单任务最近 run
- ``POST   /api/cron/tasks/{task_id}/pause``     — 暂停（enabled=False, state=PAUSED）
- ``POST   /api/cron/tasks/{task_id}/resume``    — 恢复（enabled=True, state=SCHEDULED, 重算 next_run_at）
- ``DELETE /api/cron/tasks/{task_id}``           — 物理删除
- ``POST   /api/cron/tasks/{task_id}/run_now``   — 异步触发（推前 next_run_at；ticker 抢 reservation）

依赖注入约定：

- ``app.state.scheduler_store``：由 :func:`web.app.create_app` 在 lifespan 启动 ticker
  时挂入；测试可在创建 app 后直接覆写为 :class:`scheduler.store.Store(tmp_path)`。
- 全局 :class:`web.auth.AuthMiddleware` 已挡掉未带合法 cookie 的请求；本路由不再
  显式 :func:`Depends`。

设计要点：

- ``run_now`` **不**在 web 进程内同步执行 cron（cron run 可能跑数十秒~数分钟）；
  采用"推前 ``next_run_at = now`` + 写 audit"模式，让 lifespan ticker 下一秒抢
  reservation 并触发；立即返回 ``202`` + 占位 ``run_id``。前端通过 ``/ws/cron``
  收 ``cron.run.completed`` 拿真实 ``run_id``。
- ``resume`` 通过 :func:`scheduler.timing.compute_first_run_at` 重算 ``next_run_at``；
  ``ONCE`` trigger 已过期会抛 ``ValueError`` → 翻成 ``400``（语义：已完成的一次性
  任务不可恢复）。
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from scheduler.domain import (
    ConcurrencyPolicy,
    DeliveryChannel,
    RunStatus,
    ScheduleDelivery,
    ScheduledTask,
    TaskExecutionPolicy,
    TaskOrigin,
    TaskState,
    TaskTarget,
)
from scheduler.schedule_parser import parse_schedule
from scheduler.store import TaskNotFoundError
from scheduler.timing import compute_first_run_at, to_iso, utc_now

if TYPE_CHECKING:
    from scheduler.domain import ScheduledRun, ScheduledTask
    from scheduler.store import Store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cron", tags=["cron"])


_RUNS_GLOBAL_LIMIT_MAX = 200
_RUNS_PER_TASK_LIMIT_MAX = 200


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------


class CronTaskDTO(BaseModel):
    """cron 任务展示态（v0.1）。

    枚举字段（``state`` / ``trigger_type``）以 :class:`enum.StrEnum` 的小写
    ``.value`` 序列化，与 :mod:`scheduler.domain` 真源对齐。前端如需大写常量
    需自行映射。
    """

    task_id: str
    name: str
    enabled: bool
    state: str
    trigger_type: str
    trigger_expr: str
    next_run_at: str | None
    last_run_at: str | None
    preset_id: str
    created_by: str


class CronRunDTO(BaseModel):
    """cron 单次 run 展示态。

    ``task_name`` 冗余字段：避免前端日志区为每条 run 单独 fetch task 名称
    （N+1 查询）；router 在转换时一次性 :func:`get_task` 注入。
    """

    run_id: str
    task_id: str
    task_name: str
    scheduled_for: str
    started_at: str | None
    finished_at: str | None
    status: str
    final_message_excerpt: str | None
    delivery_status: str
    delivery_error: str | None


class CronRunsPage(BaseModel):
    """``GET /api/cron/runs`` 的分页响应。

    ``next_cursor`` = 当前页最后一条 run 的 ``started_at``；前端把它当作
    下一次请求的 ``cursor`` 传入。无更多数据时为 ``None``。
    """

    runs: list[CronRunDTO]
    next_cursor: str | None


class RunNowResponse(BaseModel):
    """``POST /api/cron/tasks/{id}/run_now`` 的 202 响应。

    ``run_id`` 为占位字符串（``"pending-<uuid>"``），实际 run_id 由 ticker
    抢到 reservation 后由 :class:`scheduler.execution_bridge.ExecutionBridge`
    生成。前端通过 ``/ws/cron`` 等 ``cron.run.completed`` 帧获取真实
    ``run_id``。
    """

    run_id: str
    status: str = Field(default="PENDING")


class CreateCronTaskRequest(BaseModel):
    """``POST /api/cron/tasks`` 创建请求体。

    结构化 DTO：前端不直接耦合 schedule_tool 内部参数形态。

    - ``schedule_type``: once 时需填 ``once_at``；cron 时需填 ``cron_expr``
    - ``timezone``: IANA 时区字符串
    - ``concurrency_policy``: 可选，默认 forbid
    - ``preset_id``: 可选，LLM preset 选择
    """

    name: str
    agent_name: str
    input_text: str
    schedule_type: str = Field(description="once | cron")
    once_at: str | None = None
    cron_expr: str | None = None
    timezone: str = Field(default="UTC")
    concurrency_policy: str = Field(default="forbid")
    preset_id: str | None = None


# ---------------------------------------------------------------------------
# Domain → DTO
# ---------------------------------------------------------------------------


def _task_to_dto(task: ScheduledTask) -> CronTaskDTO:
    return CronTaskDTO(
        task_id=task.task_id,
        name=task.name,
        enabled=task.enabled,
        state=task.state.value,
        trigger_type=task.trigger.trigger_type.value,
        trigger_expr=task.trigger.expr,
        next_run_at=task.next_run_at,
        last_run_at=task.last_run_at,
        preset_id=task.preset_id,
        created_by=task.created_by,
    )


def _run_to_dto(run: ScheduledRun, *, task_name: str) -> CronRunDTO:
    return CronRunDTO(
        run_id=run.run_id,
        task_id=run.task_id,
        task_name=task_name,
        scheduled_for=run.scheduled_for,
        started_at=run.started_at,
        finished_at=run.finished_at,
        status=run.status.value,
        final_message_excerpt=run.final_message_excerpt,
        delivery_status=run.delivery_status.value,
        delivery_error=run.delivery_error,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _require_store(request: Request) -> Store:
    """从 ``app.state`` 取 :class:`scheduler.store.Store`；缺失时 503。

    生产路径：``cfg.scheduler.enabled=True`` 时由 :func:`web.app.create_app`
    在 lifespan 注入。``enabled=False`` 时 cron 模块本就不应被使用，前端不
    应该到这一步——返回 503 比 500 更准确（service unavailable）。
    """
    store: Store | None = getattr(request.app.state, "scheduler_store", None)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="scheduler not configured (scheduler_store missing)",
        )
    return store


def _require_task(store: Store, task_id: str) -> ScheduledTask:
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return task


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
    4. 构造 ``ScheduledTask`` 并 ``store.create_task``
    """
    store = _require_store(request)

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

    try:
        task = ScheduledTask(
            task_id=task_id,
            name=body.name,
            enabled=True,
            state=TaskState.SCHEDULED,
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
            preset_id=body.preset_id or "",
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        created = store.create_task(task)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    store.append_audit(
        action="create",
        task_id=created.task_id,
        actor="web",
        payload={
            "source": "cron_router",
            "preset_id": created.preset_id or "",
            "trigger_type": created.trigger.trigger_type.value,
        },
    )
    return _task_to_dto(created)


# ---------------------------------------------------------------------------
# GET: single task detail
# ---------------------------------------------------------------------------


@router.get("/tasks/{task_id}")
async def get_cron_task(request: Request, task_id: str) -> CronTaskDTO:
    """获取单个 cron 任务详情。"""
    store = _require_store(request)
    task = _require_task(store, task_id)
    return _task_to_dto(task)


# ---------------------------------------------------------------------------
# GET endpoints
# ---------------------------------------------------------------------------


@router.get("/tasks")
async def list_cron_tasks(request: Request) -> list[CronTaskDTO]:
    """列出所有 cron 任务（含 disabled）。"""
    store = _require_store(request)
    tasks = store.list_tasks(include_disabled=True)
    return [_task_to_dto(t) for t in tasks]


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
    store = _require_store(request)

    # cursor 空串归一化（FastAPI 对 ``?cursor=`` 传 "" 而非 None）
    effective_cursor = cursor if cursor else None
    runs = store.list_recent_runs(limit=limit, cursor=effective_cursor)

    # 注入 task_name（一次性建表，避免 N+1）
    name_by_task = {t.task_id: t.name for t in store.list_tasks(include_disabled=True)}
    dto_list = [_run_to_dto(r, task_name=name_by_task.get(r.task_id, r.task_id)) for r in runs]

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
    store = _require_store(request)
    task = _require_task(store, task_id)
    runs = store.list_runs(task_id, limit=limit)
    return [_run_to_dto(r, task_name=task.name) for r in runs]


# ---------------------------------------------------------------------------
# POST / DELETE endpoints
# ---------------------------------------------------------------------------


@router.post("/tasks/{task_id}/pause")
async def pause_cron_task(request: Request, task_id: str) -> CronTaskDTO:
    """暂停 cron 任务（enabled=False, state=PAUSED）。

    幂等：已 PAUSED 的任务再 pause 仍返回当前状态（``update_task`` 不报错）。
    """
    store = _require_store(request)
    _require_task(store, task_id)
    try:
        updated = store.update_task(
            task_id,
            enabled=False,
            state=TaskState.PAUSED,
        )
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}") from None
    store.append_audit(
        action="pause",
        task_id=task_id,
        actor="web",
        payload={"source": "cron_router"},
    )
    return _task_to_dto(updated)


@router.post("/tasks/{task_id}/resume")
async def resume_cron_task(request: Request, task_id: str) -> CronTaskDTO:
    """恢复 cron 任务。

    流程：
    1. 取 task 反查 trigger
    2. ``compute_first_run_at(trigger)`` 重算 ``next_run_at``
       （ONCE 已过期 → ValueError → 400）
    3. ``update_task`` 设 ``enabled=True, state=SCHEDULED, next_run_at=...``
    """
    store = _require_store(request)
    task = _require_task(store, task_id)

    try:
        new_next = compute_first_run_at(task.trigger)
    except ValueError as exc:
        # ONCE 已过期 / cron 表达式损坏等 → 不可恢复
        raise HTTPException(
            status_code=400,
            detail=f"cannot recompute next_run_at: {exc}",
        ) from exc

    try:
        updated = store.update_task(
            task_id,
            enabled=True,
            state=TaskState.SCHEDULED,
            next_run_at=new_next,
        )
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}") from None
    store.append_audit(
        action="resume",
        task_id=task_id,
        actor="web",
        payload={"source": "cron_router", "next_run_at": new_next},
    )
    return _task_to_dto(updated)


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_cron_task(request: Request, task_id: str) -> None:
    """物理删除 cron 任务（含 ``scheduled_tasks.json`` 中的条目）。

    runs/{task_id}.jsonl 历史记录保留；不在本端点清理（审计需要）。
    """
    store = _require_store(request)
    _require_task(store, task_id)
    deleted = store.delete_task(task_id)
    if not deleted:
        # 极小竞态窗口：_require_task 之后被并发 delete；按 REST 语义返 404
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    store.append_audit(
        action="delete",
        task_id=task_id,
        actor="web",
        payload={"source": "cron_router"},
    )


@router.post("/tasks/{task_id}/run_now", status_code=202)
async def run_now_cron_task(request: Request, task_id: str) -> RunNowResponse:
    """异步触发 cron 任务（``next_run_at = now``，让 ticker 下一秒抢）。

    409：当前最近一条 run 处于 :attr:`RunStatus.RUNNING`。

    返回占位 ``run_id``（``pending-<uuid>``）；真实 ``run_id`` 由 ticker 触发
    时由 execution bridge 生成，前端通过 ``/ws/cron`` 拿。
    """
    store = _require_store(request)
    _require_task(store, task_id)

    # 409：已在跑；用最近一条 run 的 status 判断
    runs = store.list_runs(task_id, limit=1)
    if runs and runs[-1].status is RunStatus.RUNNING:
        raise HTTPException(
            status_code=409,
            detail=f"task already running: {task_id}",
        )

    now_iso = to_iso(utc_now())
    try:
        store.update_task(task_id, next_run_at=now_iso)
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}") from None
    store.append_audit(
        action="run_now",
        task_id=task_id,
        actor="web",
        payload={"source": "cron_router", "scheduled_for": now_iso},
    )
    placeholder_run_id = f"pending-{uuid.uuid4().hex[:12]}"
    return RunNowResponse(run_id=placeholder_run_id, status="PENDING")


__all__ = [
    "CreateCronTaskRequest",
    "CronRunDTO",
    "CronRunsPage",
    "CronTaskDTO",
    "RunNowResponse",
    "router",
]
