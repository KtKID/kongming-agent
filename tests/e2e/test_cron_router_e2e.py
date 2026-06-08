"""``src/web/routers/cron.py`` 端到端串测（web-cron-router-v0.1 #10）。

与 ``tests/unit/web/test_cron_router.py`` 的区别：

- 单测按 endpoint 拆分独立用例；本 e2e 走**完整生命周期**串接：
  ``create → list → pause → resume → run_now → list_runs → delete → list``
- 一次 ``TestClient`` 实例，验证多次调用之间的状态在 store 持久化层正确流转
- 不连真实模型（sleep-dev 4 不准）：cron 不实际触发，仅验证 router → store 的状态推进

适用：``make test-e2e``。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from hosts.web.app import create_app
from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from hosts.web.routers.cron import router as cron_router
from hosts.web.threads.metadata import ThreadMetadata
from infrastructure.config.models import Config
from scheduler.domain import (
    ConcurrencyPolicy,
    MisfirePolicy,
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

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


class _FakeTM:
    """空 ThreadManager 桩；本 e2e 不触碰 thread 路径。"""

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


def _t(seconds_offset: int = 0) -> str:
    base = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)
    return to_iso(base + timedelta(seconds=seconds_offset))


def _make_task(
    task_id: str,
    *,
    enabled: bool = True,
    state: TaskState = TaskState.SCHEDULED,
    next_run_at: str | None = None,
) -> ScheduledTask:
    return ScheduledTask(
        task_id=task_id,
        name=f"task-{task_id}",
        enabled=enabled,
        state=state,
        origin=TaskOrigin.WEB,
        trigger=ScheduleTrigger(trigger_type=TriggerType.INTERVAL, expr="10", timezone="UTC"),
        policy=TaskExecutionPolicy(
            session_mode=SessionMode.FRESH_SESSION,
            concurrency_policy=ConcurrencyPolicy.FORBID,
            misfire_policy=MisfirePolicy.SKIP,
        ),
        target=TaskTarget(agent_name="default", input_text="ping", metadata={}),
        next_run_at=next_run_at or _t(60 * 60),  # 默认 1 小时后
        last_run_at=None,
        created_by="tester",
        created_at=_t(0),
        updated_at=_t(0),
        preset_id="preset-default",
    )


def _install_cron_router_before_catch_all(app: Any) -> None:
    """与单测同款：把 cron router 插到 SPA catch-all 之前。"""
    before_count = len(app.router.routes)
    app.include_router(cron_router)
    new_routes = app.router.routes[before_count:]
    del app.router.routes[before_count:]
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
            "web": {"enabled": True, "dev_mode": True},
            "scheduler": {"enabled": False},
        }
    )


def _bootstrap(tmp_path: Path) -> tuple[TestClient, Store]:
    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    tm = _FakeTM()
    app = create_app(cfg, tm, home_dir=tmp_path)
    _install_cron_router_before_catch_all(app)
    client = TestClient(app)
    client.__enter__()
    store = Store(tmp_path / "cron")
    app.state.scheduler_store = store
    resp = client.post("/api/auth/login", json={"password": "pwd"}, headers=CSRF_HEADERS)
    assert resp.status_code == 200, resp.text
    return client, store


# ---------------------------------------------------------------------------
# e2e: 完整生命周期串测
# ---------------------------------------------------------------------------


def test_cron_router_full_lifecycle(tmp_path: Path) -> None:
    """走完 cron task 一次完整生命周期，验证多 endpoint 之间的状态流转。

    流程：

    1. 初始 list → 空
    2. store.create_task 落两条任务（一条 enabled、一条 disabled）
    3. GET /tasks → 拿到 2 条
    4. POST pause → 第一条进入 PAUSED
    5. POST resume → 重新 SCHEDULED + next_run_at 重算
    6. POST run_now → 202 + next_run_at 被推前到 ~now
    7. GET /tasks/{id}/runs → 此时仍空（ticker 没跑）
    8. DELETE → 第一条消失；剩余 1 条（disabled）
    9. 删除已删的 → 404
    10. 整个流程中 audit log 累计 ≥ 4 条（pause + resume + run_now + delete）
    """
    client, store = _bootstrap(tmp_path)
    try:
        # 1. 初始 list 应该空
        r = client.get("/api/cron/tasks")
        assert r.status_code == 200, r.text
        assert r.json() == []

        # 2. 落两条 task：一条 enabled、一条 disabled
        active = _make_task("active")
        paused = _make_task("paused", enabled=False, state=TaskState.PAUSED, next_run_at=None)
        store.create_task(active)
        store.create_task(paused)

        # 3. 列表（include_disabled=True）应有 2 条
        r = client.get("/api/cron/tasks")
        assert r.status_code == 200
        body = r.json()
        ids = sorted(t["task_id"] for t in body)
        assert ids == ["active", "paused"]
        # disabled 也在列表里
        disabled_dto = next(t for t in body if t["task_id"] == "paused")
        assert disabled_dto["enabled"] is False
        assert disabled_dto["state"] == TaskState.PAUSED.value

        # 4. pause active task
        r = client.post("/api/cron/tasks/active/pause", headers=CSRF_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is False
        assert r.json()["state"] == TaskState.PAUSED.value
        # store 物理状态确认
        t_after_pause = store.get_task("active")
        assert t_after_pause is not None
        assert t_after_pause.enabled is False

        # 5. resume → SCHEDULED + 重算 next_run_at
        r = client.post("/api/cron/tasks/active/resume", headers=CSRF_HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] is True
        assert body["state"] == TaskState.SCHEDULED.value
        # next_run_at 重算后必非空
        assert body["next_run_at"] is not None
        assert "T" in body["next_run_at"]  # ISO8601

        # 6. run_now → 202 + next_run_at 被推前
        original_next = store.get_task("active").next_run_at  # type: ignore[union-attr]
        r = client.post("/api/cron/tasks/active/run_now", headers=CSRF_HEADERS)
        assert r.status_code == 202, r.text
        body = r.json()
        # status 字段存在；run_id 是占位
        assert body["status"] in ("PENDING", "pending")
        # next_run_at 被推前
        new_next = store.get_task("active").next_run_at  # type: ignore[union-attr]
        assert new_next is not None
        assert new_next != original_next  # 被推前了

        # 7. 拿 /tasks/{id}/runs（ticker 没跑，应该空）
        r = client.get("/api/cron/tasks/active/runs")
        assert r.status_code == 200
        assert r.json() == []

        # 8. DELETE active → 204
        r = client.delete("/api/cron/tasks/active", headers=CSRF_HEADERS)
        assert r.status_code == 204
        # 列表只剩 paused
        r = client.get("/api/cron/tasks")
        assert r.status_code == 200
        assert [t["task_id"] for t in r.json()] == ["paused"]

        # 9. 删除已删的 → 404
        r = client.delete("/api/cron/tasks/active", headers=CSRF_HEADERS)
        assert r.status_code == 404

        # 10. audit log 累积 ≥ 4 条
        audit_lines = store.list_audits()
        actions = [entry.get("action") for entry in audit_lines]
        assert actions.count("pause") >= 1
        assert actions.count("resume") >= 1
        assert actions.count("run_now") >= 1
        assert actions.count("delete") >= 1
    finally:
        client.__exit__(None, None, None)


def test_cron_router_global_runs_pagination(tmp_path: Path) -> None:
    """跨任务 GET /api/cron/runs cursor 分页串测。

    1. 落 3 个 task，各 4 条 run（不同 started_at）→ 共 12 条
    2. limit=5 拿第一页 → 5 条 + 非空 cursor
    3. limit=5 + cursor → 拿第二页 → 5 条
    4. limit=5 + cursor2 → 拿第三页 → 2 条 + cursor=null
    5. 三页合并 12 条无重复 + 倒序正确
    """
    from scheduler.domain import DeliveryStatus, RunStatus, ScheduledRun

    client, store = _bootstrap(tmp_path)
    try:
        # 准备 3 task × 4 run = 12 条
        for i in range(3):
            tid = f"t{i}"
            store.create_task(_make_task(tid))
            for j in range(4):
                offset = i * 100 + j  # 保证 12 条 started_at 全不同
                run = ScheduledRun(
                    run_id=f"r-{tid}-{j}",
                    task_id=tid,
                    status=RunStatus.COMPLETED,
                    scheduled_for=_t(offset),
                    started_at=_t(offset),
                    finished_at=_t(offset + 1),
                    session_id=f"s-{tid}-{j}",
                    result_status="ok",
                    final_message_excerpt=f"msg-{tid}-{j}",
                    error_message=None,
                    failure_reason=None,
                    delivery_error=None,
                    silent_suppressed=False,
                    delivery_status=DeliveryStatus.DELIVERED,
                )
                store.append_run(run)

        # 第一页
        r = client.get("/api/cron/runs?limit=5")
        assert r.status_code == 200
        page1 = r.json()
        assert len(page1["runs"]) == 5
        assert page1["next_cursor"] is not None

        # 第二页
        r = client.get(f"/api/cron/runs?limit=5&cursor={page1['next_cursor']}")
        assert r.status_code == 200
        page2 = r.json()
        assert len(page2["runs"]) == 5
        assert page2["next_cursor"] is not None

        # 第三页（剩 2 条）
        r = client.get(f"/api/cron/runs?limit=5&cursor={page2['next_cursor']}")
        assert r.status_code == 200
        page3 = r.json()
        assert len(page3["runs"]) == 2
        # 三页合计 12 条 + 无重复
        all_run_ids = (
            [run["run_id"] for run in page1["runs"]]
            + [run["run_id"] for run in page2["runs"]]
            + [run["run_id"] for run in page3["runs"]]
        )
        assert len(all_run_ids) == 12
        assert len(set(all_run_ids)) == 12

        # 验证倒序：started_at 递减
        all_started = (
            [run["started_at"] for run in page1["runs"]]
            + [run["started_at"] for run in page2["runs"]]
            + [run["started_at"] for run in page3["runs"]]
        )
        assert all_started == sorted(all_started, reverse=True)
    finally:
        client.__exit__(None, None, None)
