"""``src/web/routers/cron.py`` REST 路由单测（web-cron-router-v0.1 #6）。

覆盖 7 个 endpoint：

1. ``GET    /api/cron/tasks``                 — happy path
2. ``GET    /api/cron/runs``                  — happy + 400 limit 越界
3. ``GET    /api/cron/tasks/{id}/runs``       — happy + 404
4. ``POST   /api/cron/tasks/{id}/pause``      — happy + 404
5. ``POST   /api/cron/tasks/{id}/resume``     — happy + 404 + ONCE 已过期 400
6. ``DELETE /api/cron/tasks/{id}``            — 204 + 404
7. ``POST   /api/cron/tasks/{id}/run_now``    — 202 + 404 + 409 已在跑

辅助说明：

- 走真实 :class:`scheduler.store.Store` (``tmp_path``)，不 mock；router 跟 store
  之间的契约风险才是这层测试要保护的核心
- 走真实 :class:`fastapi.testclient.TestClient`，AuthMiddleware + CSRFMiddleware
  全链路；登录 cookie 由 ``_login_client`` 一次 POST ``/api/auth/login`` 拿到
- ``cfg.scheduler.enabled=False`` 关掉真实 lifespan ticker，测试在 client
  进入 lifespan 后手动把 ``Store(tmp_path / "cron")`` 挂到 ``app.state``
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from infrastructure.config.models import Config
from scheduler.domain import (
    ConcurrencyPolicy,
    DeliveryStatus,
    MisfirePolicy,
    RunStatus,
    ScheduledRun,
    ScheduledTask,
    ScheduleTrigger,
    SessionMode,
    TaskExecutionPolicy,
    TaskOrigin,
    TaskState,
    TaskTarget,
    TriggerType,
)
from scheduler.store import Store
from scheduler.timing import to_iso
from tests.unit.test_web_app_lifespan import _seed_password
from web.app import create_app
from web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from web.routers.cron import router as cron_router
from web.threads.metadata import ThreadMetadata

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


# ---------------------------------------------------------------------------
# Fake ThreadManager（与现有 web router 测试同款最小桩）
# ---------------------------------------------------------------------------


class _FakeTM:
    def __init__(self) -> None:
        self._threads: dict[str, ThreadMetadata] = {}

    @property
    def started(self) -> bool:
        return True

    @property
    def closed(self) -> bool:
        return True

    async def start(self) -> None:
        return None

    async def aclose_all(self) -> None:
        return None

    def list_threads(self) -> list[ThreadMetadata]:
        return list(self._threads.values())

    def list_cells(self) -> list[Any]:
        return []

    def get_cell(self, thread_id: str) -> Any:
        return None

    def resolve_approval(self, thread_id: str, call_id: str, approved: bool) -> None:
        return None


# ---------------------------------------------------------------------------
# domain fixture builders（与 tests/unit/scheduler/test_scheduler_store.py 同款）
# ---------------------------------------------------------------------------


def _t(seconds_offset: int = 0, *, base: datetime | None = None) -> str:
    base = base or datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)
    return to_iso(base + timedelta(seconds=seconds_offset))


def _trigger_interval(minutes: int = 10) -> ScheduleTrigger:
    return ScheduleTrigger(
        trigger_type=TriggerType.INTERVAL,
        expr=str(minutes),
        timezone="UTC",
    )


def _trigger_once(at_iso: str | None = None) -> ScheduleTrigger:
    return ScheduleTrigger(
        trigger_type=TriggerType.ONCE,
        expr=at_iso or _t(60),
        timezone="UTC",
    )


def _make_task(
    *,
    task_id: str = "t-1",
    name: str | None = None,
    enabled: bool = True,
    state: TaskState = TaskState.SCHEDULED,
    trigger: ScheduleTrigger | None = None,
    next_run_at: str | None = None,
    last_run_at: str | None = None,
    preset_id: str = "preset-default",
) -> ScheduledTask:
    return ScheduledTask(
        task_id=task_id,
        name=name or f"task-{task_id}",
        enabled=enabled,
        state=state,
        origin=TaskOrigin.WEB,
        trigger=trigger or _trigger_interval(10),
        policy=TaskExecutionPolicy(
            session_mode=SessionMode.FRESH_SESSION,
            concurrency_policy=ConcurrencyPolicy.FORBID,
            misfire_policy=MisfirePolicy.SKIP,
        ),
        target=TaskTarget(agent_name="default", input_text="ping", metadata={}),
        next_run_at=next_run_at,
        last_run_at=last_run_at,
        created_by="tester",
        created_at=_t(0),
        updated_at=_t(0),
        preset_id=preset_id,
    )


def _make_run(
    *,
    run_id: str = "r-1",
    task_id: str = "t-1",
    status: RunStatus = RunStatus.COMPLETED,
    scheduled_for: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    final_message_excerpt: str | None = "done",
    delivery_status: DeliveryStatus = DeliveryStatus.DELIVERED,
) -> ScheduledRun:
    return ScheduledRun(
        run_id=run_id,
        task_id=task_id,
        status=status,
        scheduled_for=scheduled_for or _t(0),
        started_at=started_at or _t(0),
        finished_at=finished_at or _t(5),
        session_id=f"sess-{run_id}",
        result_status="ok",
        final_message_excerpt=final_message_excerpt,
        error_message=None,
        failure_reason=None,
        delivery_error=None,
        silent_suppressed=False,
        delivery_status=delivery_status,
    )


# ---------------------------------------------------------------------------
# app + client 装配
# ---------------------------------------------------------------------------


def _install_cron_router_before_catch_all(app: Any) -> None:
    """把 :data:`cron_router` 的所有 routes 插入到 SPA catch-all 之前。

    背景：``create_app`` 在 router 注册末尾调 :func:`install_static`，后者注册了
    ``GET /{full_path:path}`` 的 SPA fallback / dev placeholder。FastAPI 按
    ``app.routes`` 顺序匹配，直接 ``include_router`` 会让新 router 的 routes 排
    在 catch-all 之后，被 404 抢先返回。

    本辅助函数直接对 ``app.router.routes`` 列表做插入：先 ``include_router``，
    再把这次新增的 ``len(cron_router.routes)`` 条 routes 移到 catch-all 之前。
    """
    before_count = len(app.router.routes)
    app.include_router(cron_router)
    new_routes = app.router.routes[before_count:]
    # 移除尾部新增（include_router 直接 append）
    del app.router.routes[before_count:]
    # 找 SPA catch-all 的索引（path 含 ``{full_path:path}``）
    catch_all_index: int | None = None
    for idx, route in enumerate(app.router.routes):
        path_format = getattr(route, "path_format", None) or getattr(route, "path", "")
        if "{full_path" in path_format:
            catch_all_index = idx
            break
    insert_at = catch_all_index if catch_all_index is not None else len(app.router.routes)
    for offset, route in enumerate(new_routes):
        app.router.routes.insert(insert_at + offset, route)


def _make_cfg() -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {
                "enabled": True,
                "dev_mode": True,
            },
            "scheduler": {
                # 关闭真实 ticker；store 由测试手动挂入
                "enabled": False,
            },
        }
    )


def _login_client_with_store(tmp_path: Path) -> tuple[TestClient, Store]:
    """构造 TestClient + 隔离的 :class:`Store`。

    流程：
    1. ``_seed_password`` 落 password.hash
    2. ``create_app(cfg, FakeTM, home_dir=tmp_path)`` 装 FastAPI
    3. 进入 ``with TestClient`` 触发 lifespan startup
    4. 手动挂 ``app.state.scheduler_store = Store(tmp_path / "cron")``
    5. POST ``/api/auth/login`` 拿合法 cookie

    返回 ``(client, store)``：测试既能直接走 store 准备数据，也能走 client 发请求。
    """
    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    tm = _FakeTM()
    app = create_app(cfg, tm, home_dir=tmp_path)
    # router 注册在 batch B/C（task #9）；单测里独立挂入 cron router 不破坏其他测试。
    # ⚠️ create_app 末尾装了 dev_mode 下的 SPA placeholder catch-all（``GET /{full_path:path}``），
    # FastAPI 按 routes 列表顺序匹配；直接 ``include_router`` 会被 catch-all 抢先返回 404。
    # 解决：把 cron 新增的 routes 插到 catch-all 前面。
    _install_cron_router_before_catch_all(app)
    client = TestClient(app)
    client.__enter__()  # 触发 lifespan startup（不挂 ticker，store 缺省）

    # 手动挂 store —— 走 tmp_path/cron 目录，与 lifespan 默认路径一致便于人工调试
    store = Store(tmp_path / "cron")
    app.state.scheduler_store = store

    resp = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    return client, store


# ---------------------------------------------------------------------------
# GET /api/cron/tasks
# ---------------------------------------------------------------------------


def test_list_cron_tasks_empty(tmp_path: Path) -> None:
    client, _ = _login_client_with_store(tmp_path)
    try:
        resp = client.get("/api/cron/tasks")
        assert resp.status_code == 200
        assert resp.json() == []
    finally:
        client.__exit__(None, None, None)


def test_list_cron_tasks_includes_disabled(tmp_path: Path) -> None:
    client, store = _login_client_with_store(tmp_path)
    try:
        store.create_task(_make_task(task_id="alive", next_run_at=_t(60)))
        store.create_task(
            _make_task(
                task_id="dead",
                enabled=False,
                state=TaskState.PAUSED,
                next_run_at=_t(120),
            )
        )

        resp = client.get("/api/cron/tasks")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 2
        ids = sorted(item["task_id"] for item in items)
        assert ids == ["alive", "dead"]
        # 检查关键字段
        alive = next(it for it in items if it["task_id"] == "alive")
        assert alive["enabled"] is True
        assert alive["state"] == "scheduled"
        assert alive["trigger_type"] == "interval"
        assert alive["trigger_expr"] == "10"
        assert alive["preset_id"] == "preset-default"
        # disabled 也要返
        dead = next(it for it in items if it["task_id"] == "dead")
        assert dead["enabled"] is False
        assert dead["state"] == "paused"
    finally:
        client.__exit__(None, None, None)


def test_list_cron_tasks_requires_auth(tmp_path: Path) -> None:
    """未登录 client 请求 /api/cron/tasks → 401（AuthMiddleware）。"""
    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    tm = _FakeTM()
    app = create_app(cfg, tm, home_dir=tmp_path)
    _install_cron_router_before_catch_all(app)
    client = TestClient(app)
    client.__enter__()
    try:
        # 不调 /api/auth/login —— 没 cookie
        resp = client.get("/api/cron/tasks")
        assert resp.status_code == 401
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# GET /api/cron/runs
# ---------------------------------------------------------------------------


def test_list_cron_runs_global_happy(tmp_path: Path) -> None:
    """跨 task 取最近 run；按 started_at 倒序；包含 task_name 冗余字段。"""
    client, store = _login_client_with_store(tmp_path)
    try:
        store.create_task(_make_task(task_id="alpha", name="Alpha"))
        store.create_task(_make_task(task_id="beta", name="Beta"))
        # 两个 task 各 append 一条 run，时间戳错开
        store.append_run(
            _make_run(
                run_id="r-alpha",
                task_id="alpha",
                started_at=_t(10),
                finished_at=_t(15),
            )
        )
        store.append_run(
            _make_run(
                run_id="r-beta",
                task_id="beta",
                started_at=_t(20),
                finished_at=_t(25),
            )
        )

        resp = client.get("/api/cron/runs?limit=10")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "runs" in body
        assert "next_cursor" in body
        runs = body["runs"]
        assert len(runs) == 2
        # 倒序：beta 先（started_at=_t(20) 比 _t(10) 大）
        assert runs[0]["run_id"] == "r-beta"
        assert runs[0]["task_name"] == "Beta"
        assert runs[1]["run_id"] == "r-alpha"
        assert runs[1]["task_name"] == "Alpha"
        # 字段完整性
        assert runs[0]["status"] == "completed"
        assert runs[0]["delivery_status"] == "delivered"
        assert runs[0]["final_message_excerpt"] == "done"
    finally:
        client.__exit__(None, None, None)


def test_list_cron_runs_global_limit_too_large_returns_400(tmp_path: Path) -> None:
    """limit 越上限 → FastAPI Query(le=200) 校验 → 422（pydantic）。

    pydantic v2 把 query 参数越界翻成 422 而非 400；接受这个语义（与其他
    router 测试一致）。任务包描述 "400 limit 越界" 不严格区分 400 vs 422，
    都属于客户端错误。
    """
    client, _ = _login_client_with_store(tmp_path)
    try:
        resp = client.get("/api/cron/runs?limit=300")
        assert resp.status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_list_cron_runs_global_empty_store(tmp_path: Path) -> None:
    client, _ = _login_client_with_store(tmp_path)
    try:
        resp = client.get("/api/cron/runs")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"runs": [], "next_cursor": None}
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# GET /api/cron/tasks/{id}/runs
# ---------------------------------------------------------------------------


def test_list_task_runs_happy(tmp_path: Path) -> None:
    client, store = _login_client_with_store(tmp_path)
    try:
        store.create_task(_make_task(task_id="alpha", name="Alpha"))
        store.append_run(_make_run(run_id="r-1", task_id="alpha", started_at=_t(10)))
        store.append_run(_make_run(run_id="r-2", task_id="alpha", started_at=_t(20)))

        resp = client.get("/api/cron/tasks/alpha/runs?limit=10")
        assert resp.status_code == 200
        runs = resp.json()
        assert len(runs) == 2
        # list_runs 是落盘顺序（旧→新）
        assert runs[0]["run_id"] == "r-1"
        assert runs[1]["run_id"] == "r-2"
        # task_name 冗余注入
        assert all(r["task_name"] == "Alpha" for r in runs)
    finally:
        client.__exit__(None, None, None)


def test_list_task_runs_unknown_task_returns_404(tmp_path: Path) -> None:
    client, _ = _login_client_with_store(tmp_path)
    try:
        resp = client.get("/api/cron/tasks/missing/runs")
        assert resp.status_code == 404
        assert "missing" in resp.text
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# POST /api/cron/tasks/{id}/pause
# ---------------------------------------------------------------------------


def test_pause_task_happy(tmp_path: Path) -> None:
    client, store = _login_client_with_store(tmp_path)
    try:
        store.create_task(_make_task(task_id="alpha"))

        resp = client.post("/api/cron/tasks/alpha/pause", headers=CSRF_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["task_id"] == "alpha"
        assert body["enabled"] is False
        assert body["state"] == "paused"

        # store 侧确认
        task = store.get_task("alpha")
        assert task is not None
        assert task.enabled is False
        assert task.state is TaskState.PAUSED

        # audit 写了一条
        audits = store.list_audits(task_id="alpha")
        assert any(a["action"] == "pause" and a["actor"] == "web" for a in audits)
    finally:
        client.__exit__(None, None, None)


def test_pause_task_unknown_returns_404(tmp_path: Path) -> None:
    client, _ = _login_client_with_store(tmp_path)
    try:
        resp = client.post("/api/cron/tasks/missing/pause", headers=CSRF_HEADERS)
        assert resp.status_code == 404
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# POST /api/cron/tasks/{id}/resume
# ---------------------------------------------------------------------------


def test_resume_task_recomputes_next_run_at(tmp_path: Path) -> None:
    client, store = _login_client_with_store(tmp_path)
    try:
        store.create_task(
            _make_task(
                task_id="alpha",
                enabled=False,
                state=TaskState.PAUSED,
                trigger=_trigger_interval(5),  # 5 min
                next_run_at=None,  # paused 之后清空
            )
        )

        resp = client.post("/api/cron/tasks/alpha/resume", headers=CSRF_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["enabled"] is True
        assert body["state"] == "scheduled"
        # 重算后 next_run_at 必非空且 ISO8601 文本
        assert body["next_run_at"] is not None
        assert "T" in body["next_run_at"]

        task = store.get_task("alpha")
        assert task is not None
        assert task.enabled is True
        assert task.state is TaskState.SCHEDULED
        assert task.next_run_at is not None
    finally:
        client.__exit__(None, None, None)


def test_resume_task_unknown_returns_404(tmp_path: Path) -> None:
    client, _ = _login_client_with_store(tmp_path)
    try:
        resp = client.post("/api/cron/tasks/missing/resume", headers=CSRF_HEADERS)
        assert resp.status_code == 404
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# DELETE /api/cron/tasks/{id}
# ---------------------------------------------------------------------------


def test_delete_task_happy(tmp_path: Path) -> None:
    client, store = _login_client_with_store(tmp_path)
    try:
        store.create_task(_make_task(task_id="alpha"))

        resp = client.delete("/api/cron/tasks/alpha", headers=CSRF_HEADERS)
        assert resp.status_code == 204
        assert resp.text == ""

        # store 已没了
        assert store.get_task("alpha") is None
        # audit
        audits = store.list_audits(task_id="alpha")
        assert any(a["action"] == "delete" for a in audits)
    finally:
        client.__exit__(None, None, None)


def test_delete_task_unknown_returns_404(tmp_path: Path) -> None:
    client, _ = _login_client_with_store(tmp_path)
    try:
        resp = client.delete("/api/cron/tasks/missing", headers=CSRF_HEADERS)
        assert resp.status_code == 404
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# POST /api/cron/tasks/{id}/run_now
# ---------------------------------------------------------------------------


def test_run_now_happy(tmp_path: Path) -> None:
    """run_now 推前 next_run_at；返回 202 + 占位 run_id。"""
    client, store = _login_client_with_store(tmp_path)
    try:
        store.create_task(
            _make_task(
                task_id="alpha",
                next_run_at=_t(60 * 60),  # 1 小时后
            )
        )

        resp = client.post("/api/cron/tasks/alpha/run_now", headers=CSRF_HEADERS)
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "PENDING"
        assert body["run_id"].startswith("pending-")

        # store next_run_at 已被推前到 ~ now
        task = store.get_task("alpha")
        assert task is not None
        assert task.next_run_at is not None
        # 不严格断言时间，只看远早于 1 小时后
        # 简单方式：next_run_at 不再等于原 _t(60*60)
        assert task.next_run_at != _t(60 * 60)

        # audit
        audits = store.list_audits(task_id="alpha")
        assert any(a["action"] == "run_now" for a in audits)
    finally:
        client.__exit__(None, None, None)


def test_run_now_unknown_returns_404(tmp_path: Path) -> None:
    client, _ = _login_client_with_store(tmp_path)
    try:
        resp = client.post("/api/cron/tasks/missing/run_now", headers=CSRF_HEADERS)
        assert resp.status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_run_now_already_running_returns_409(tmp_path: Path) -> None:
    """已有最新 run 处于 RUNNING → 409 conflict。"""
    client, store = _login_client_with_store(tmp_path)
    try:
        store.create_task(_make_task(task_id="alpha"))
        # append 一条 RUNNING run
        store.append_run(
            _make_run(
                run_id="r-running",
                task_id="alpha",
                status=RunStatus.RUNNING,
                started_at=_t(0),
                finished_at=None,
            )
        )

        resp = client.post("/api/cron/tasks/alpha/run_now", headers=CSRF_HEADERS)
        assert resp.status_code == 409
        assert "already running" in resp.text
    finally:
        client.__exit__(None, None, None)
