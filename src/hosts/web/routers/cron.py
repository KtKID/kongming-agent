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
- 全局 :class:`web.auth.middleware.AuthMiddleware` 已挡掉未带合法 cookie 的请求；本路由不再
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
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

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
    TriggerType,
)
from scheduler.manager import SchedulerManager
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
    thread_id: str
    preset_id: str
    created_by: str
    # v0.5.4: 前端编辑弹窗需要回填这两个字段，否则用户改 schedule 后整条
    # task 会失去 input_text / agent_name（GET /tasks 不返回 → 提交时丢空）。
    input_text: str
    agent_name: str


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
    抢到 reservation 后由 :class:`application.scheduled_runs.execution_bridge.ExecutionBridge`
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


class UpdateCronTaskRequest(BaseModel):
    """``PATCH /api/cron/tasks/{task_id}`` 编辑请求体（v0.5.3 cron-task-edit）。

    所有字段 optional：前端只传想改的部分；``None`` 表示"不改"，原值原样保留。

    - ``name`` / ``preset_id`` / ``enabled``：直接覆盖
    - ``agent`` / ``input_text``：映射到嵌套 :class:`TaskTarget`；store 不支持
      nested partial update，router 必须先读出 task → 构造新 ``TaskTarget`` → 传
      整个 ``target=...``
    - ``schedule``：自由文本（"every 30s"、"0 9 * * *"、ISO timestamp 等），
      复用 :func:`parse_schedule` 解析后重写 ``trigger`` + 重算 ``next_run_at``。
      ``default_tz`` 沿用当前 task ``trigger.timezone``，调用方想改时区需先改
      schedule 表达式（v0.5.3 不暴露独立 timezone 字段，避免双源歧义）
    - ``concurrency_policy``：映射到嵌套 :class:`TaskExecutionPolicy`，同
      ``target`` 一样需先读出 ``policy`` 再 ``replace``
    - ``enabled=False`` → ``state`` 同步 :attr:`TaskState.DISABLED`；``True`` →
      :attr:`TaskState.SCHEDULED`。domain ``__post_init__`` 强制 ``enabled=False``
      时 state ∈ {paused, disabled, completed, deleted}，本端点直接选 DISABLED
      避免误把 PAUSED（pause 端点的语义位）抢走。
    """

    name: str | None = None
    schedule: str | None = None
    agent: str | None = None
    input_text: str | None = None
    preset_id: str | None = None
    enabled: bool | None = None
    concurrency_policy: Literal["forbid", "allow", "replace"] | None = None


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
        thread_id=task.thread_id,
        preset_id=task.preset_id,
        created_by=task.created_by,
        # v0.5.4: 透出 target 嵌套字段供前端编辑回填
        input_text=task.target.input_text,
        agent_name=task.target.agent_name,
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


def _require_thread_manager(request: Request) -> Any:
    manager = getattr(request.app.state, "thread_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail="thread manager not configured",
        )
    return manager


def _require_config(request: Request) -> Any:
    cfg = getattr(request.app.state, "config", None)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail="app config not configured",
        )
    return cfg


def _require_task(store: Store, task_id: str) -> ScheduledTask:
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return task


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
    presets = list(getattr(cfg.web, "llm_presets", []) or [])
    if presets:
        return str(presets[0].id)
    raise HTTPException(status_code=422, detail="preset_id required for scheduled task thread")


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
    task_preset_id = (body.preset_id or "").strip()
    thread_preset_id = _resolve_thread_preset_id(request, task_preset_id)

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
            preset_id=task_preset_id,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        manager = SchedulerManager(
            store,
            thread_provisioner=_require_thread_manager(request),
        )
        created = await manager.create_task_with_thread(
            task,
            thread_preset_id=thread_preset_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    store.append_audit(
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
    return _task_to_dto(created)


# ---------------------------------------------------------------------------
# PATCH: edit task（v0.5.3 cron-task-edit）
# ---------------------------------------------------------------------------


@router.patch("/tasks/{task_id}", status_code=200)
async def update_cron_task(
    request: Request, task_id: str, body: UpdateCronTaskRequest
) -> CronTaskDTO:
    """编辑现有 cron task。

    只允许改 "可变字段"（name / schedule / agent / input_text / preset_id /
    enabled / concurrency_policy）。其他字段（origin / created_by / created_at /
    trigger.timezone 等）不在本端点暴露。

    边界：
    - task 不存在 → 404
    - 入参全部为 None → 200 直接回当前 task（语义：无操作，不算错误）
    - schedule 解析失败 → 422
    - enabled / state 组合非法（实际上由路由侧固定映射所以不会触发）→ 422

    实现：先 ``get_task`` 拿现状，按映射构造 ``fields_to_update`` dict 传给
    :meth:`Store.update_task`。嵌套 dataclass（``target`` / ``policy``）走
    :func:`dataclasses.replace` 局部覆盖，整体回写；store 层 ``replace(t, **payload)``
    会消费整体字段。
    """
    store = _require_store(request)
    task = _require_task(store, task_id)

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

    # enabled：同步 state 防止 dataclass __post_init__ 报错
    if body.enabled is not None:
        fields_to_update["enabled"] = body.enabled
        # 当 enabled 翻转时同步 state。enabled=False 必须挑 DISABLED 避免抢
        # PAUSED 的语义位（PAUSED 是 pause 端点的专属态）。
        # enabled=True 强制 SCHEDULED；如果之前是 COMPLETED 等态，schedule 不变
        # 时直接 SCHEDULED 可能让 ticker 立即重跑历史 next_run_at —— 但本端点
        # 没改 next_run_at 时不主动重算（用户应当显式给 schedule）。
        fields_to_update["state"] = TaskState.SCHEDULED if body.enabled else TaskState.DISABLED
        changed.append("enabled")

    # v0.5.4: ONCE 已完成任务改 schedule 时自动复活
    # 根因：ONCE 跑完后 enabled=False + last_run_at!=None + state=COMPLETED，
    # ticker.reserve_due_tasks 双重过滤（enabled=True 且 last_run_at=None）会跳过；
    # 用户改 schedule 想再跑一次也救不回。这里在用户改 schedule 且原任务是已完成
    # ONCE 时主动翻转三个字段，避免与 pause / disabled 抢语义位（recurring 任务
    # 自己有 next_run_at 节拍，不在此复活）。
    if (
        "trigger" in fields_to_update
        and fields_to_update["trigger"].trigger_type == TriggerType.ONCE
        and task.last_run_at is not None
    ):
        fields_to_update["enabled"] = True
        fields_to_update["last_run_at"] = None
        fields_to_update["state"] = TaskState.SCHEDULED
        if "revived_once" not in changed:
            changed.append("revived_once")

    if not fields_to_update:
        # 入参全空：幂等返回当前 task，不写 audit
        return _task_to_dto(task)

    try:
        updated = store.update_task(task_id, **fields_to_update)
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}") from None
    except (ValueError, TypeError) as exc:
        # dataclass __post_init__ 触发的不变量违例
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    store.append_audit(
        action="update",
        task_id=task_id,
        actor="web",
        payload={
            "source": "cron_router",
            "changed_fields": changed,
            **({"new_next_run_at": new_next_run_at} if new_next_run_at is not None else {}),
        },
    )
    return _task_to_dto(updated)


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
    "UpdateCronTaskRequest",
    "router",
]
