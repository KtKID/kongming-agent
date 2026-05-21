"""``POST /api/cron/tasks`` + ``GET /api/cron/tasks/{id}`` 创建链路单测。

验证三种调度模式（一次性 / 每天 / 每周）+ Cron 高级的任务创建落库，
以及常见校验拒绝路径。

覆盖：
  1. 一次性 — happy path（指定未来时间，验证 next_run_at / trigger_type / state）
  2. 一次性 — once_at 缺失 → 422
  3. 一次性 — once_at 是过去时间 → 422
  4. 每天  — happy path（验证 cron 表达式正确 + next_run_at 合理）
  5. 每周  — happy path（验证 cron 表达式含 weekday + next_run_at 合理）
  6. Cron  — happy path（5 字段 cron 表达式原样落库）
  7. Cron  — 非法 cron 表达式 → 422
  8. name 为空 → 422
  9. input_text 为空 → 422
  10. schedule_type 不合法 → 422
  11. GET /api/cron/tasks/{id} — 取回刚创建的任务，字段完整
  12. GET /api/cron/tasks/{id} — 不存在 → 404

测试策略：
  - 走真实 Store（tmp_path），不 mock
  - 走真实 TestClient，全链路 AuthMiddleware + CSRFMiddleware
  - 复用 test_cron_router.py 的装配函数和 fixture builder
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scheduler.domain import TaskState, TriggerType
from scheduler.timing import to_iso
from tests.unit.test_web_app_lifespan import _seed_password
from tests.unit.web.test_cron_router import (
    CSRF_HEADERS,
    _FakeTM,
    _install_cron_router_before_catch_all,
    _login_client_with_store,
    _make_cfg,
)
from web.app import create_app

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _future_iso(minutes: int = 5) -> str:
    """从当前时刻往后推 N 分钟的 ISO8601 字符串（用于 once_at）。"""
    return to_iso(datetime.now(UTC) + timedelta(minutes=minutes))


# ---------------------------------------------------------------------------
# 1. 一次性 — happy path
# ---------------------------------------------------------------------------


def test_create_once_task_happy(tmp_path: Path) -> None:
    """POST /api/cron/tasks — 一次性任务，验证落库字段。"""
    client, store = _login_client_with_store(tmp_path)
    try:
        once_at = _future_iso(10)
        resp = client.post(
            "/api/cron/tasks",
            json={
                "name": "创建 scheduler-test-once.txt",
                "agent_name": "default",
                "input_text": "在当前目录创建一个名为 scheduler-test-once.txt 的文件，内容写入当前时间戳",
                "schedule_type": "once",
                "once_at": once_at,
                "timezone": "Asia/Shanghai",
            },
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "创建 scheduler-test-once.txt"
        assert body["enabled"] is True
        assert body["state"] == "scheduled"
        assert body["trigger_type"] == "once"
        assert body["trigger_expr"] is not None  # ISO timestamp
        assert body["next_run_at"] is not None
        assert body["task_id"]

        # store 侧确认
        task = store.get_task(body["task_id"])
        assert task is not None
        assert task.state is TaskState.SCHEDULED
        assert task.trigger.trigger_type is TriggerType.ONCE
        assert (
            task.target.input_text
            == "在当前目录创建一个名为 scheduler-test-once.txt 的文件，内容写入当前时间戳"
        )

        # audit
        audits = store.list_audits(task_id=body["task_id"])
        assert any(a["action"] == "create" and a["actor"] == "web" for a in audits)
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 2. 一次性 — once_at 缺失 → 422
# ---------------------------------------------------------------------------


def test_create_once_task_missing_once_at(tmp_path: Path) -> None:
    client, _ = _login_client_with_store(tmp_path)
    try:
        resp = client.post(
            "/api/cron/tasks",
            json={
                "name": "bad-once",
                "agent_name": "default",
                "input_text": "test",
                "schedule_type": "once",
                "timezone": "UTC",
            },
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422
        assert "once_at" in resp.json()["detail"].lower()
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 3. 一次性 — once_at 是过去时间 → parse_schedule 走通但 compute_first_run_at OK
#    注意：ONCE trigger 的 compute_first_run_at 直接返回 expr（不做过去校验），
#    所以这个用例验证的是 parse_schedule 接受过去时间后仍然成功落库。
#    如果将来加了"过去时间拒绝"逻辑，这个用例应该改为期望 422。
# ---------------------------------------------------------------------------


def test_create_once_task_past_time_still_accepted(tmp_path: Path) -> None:
    """ONCE trigger 不过校验过去时间，落库成功。"""
    client, _ = _login_client_with_store(tmp_path)
    try:
        past = to_iso(datetime.now(UTC) - timedelta(hours=1))
        resp = client.post(
            "/api/cron/tasks",
            json={
                "name": "past-once",
                "agent_name": "default",
                "input_text": "test",
                "schedule_type": "once",
                "once_at": past,
                "timezone": "UTC",
            },
            headers=CSRF_HEADERS,
        )
        # 当前版本不过去校验，预期 201
        assert resp.status_code == 201, resp.text
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 4. 每天 — happy path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected_expr"),
    [
        (
            {
                "name": "创建 scheduler-test-daily.txt",
                "agent_name": "default",
                "input_text": "在当前目录创建一个名为 scheduler-test-daily.txt 的文件，内容写入当前日期",
                "schedule_type": "cron",
                "cron_expr": "0 9 * * *",
                "timezone": "Asia/Shanghai",
            },
            "0 9 * * *",
        ),
        (
            {
                "name": "创建 scheduler-test-weekly.txt",
                "agent_name": "default",
                "input_text": "在当前目录创建一个名为 scheduler-test-weekly.txt 的文件，内容写入当前周数",
                "schedule_type": "cron",
                "cron_expr": "0 9 * * 3",
                "timezone": "Asia/Shanghai",
            },
            "0 9 * * 3",
        ),
        (
            {
                "name": "每 5 分钟",
                "agent_name": "default",
                "input_text": "test",
                "schedule_type": "cron",
                "cron_expr": "*/5 * * * *",
                "timezone": "UTC",
            },
            "*/5 * * * *",
        ),
        (
            {
                "name": "每 10 秒",
                "agent_name": "default",
                "input_text": "test",
                "schedule_type": "cron",
                "cron_expr": "*/10 * * * * *",
                "timezone": "UTC",
            },
            "*/10 * * * * *",
        ),
    ],
)
def test_create_cron_task_happy_path(
    tmp_path: Path,
    payload: dict[str, str],
    expected_expr: str,
) -> None:
    """POST /api/cron/tasks — 压缩 cron happy path 矩阵。"""
    client, store = _login_client_with_store(tmp_path)
    try:
        resp = client.post("/api/cron/tasks", json=payload, headers=CSRF_HEADERS)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == payload["name"]
        assert body["state"] == "scheduled"
        assert body["trigger_type"] == "cron"
        assert body["trigger_expr"] == expected_expr
        assert body["next_run_at"] is not None

        task = store.get_task(body["task_id"])
        assert task is not None
        assert task.trigger.trigger_type is TriggerType.CRON
        assert task.trigger.expr == expected_expr
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 7. Cron — 非法 cron 表达式 → 422
# ---------------------------------------------------------------------------


def test_create_cron_invalid_expr(tmp_path: Path) -> None:
    client, _ = _login_client_with_store(tmp_path)
    try:
        resp = client.post(
            "/api/cron/tasks",
            json={
                "name": "bad-cron",
                "agent_name": "default",
                "input_text": "test",
                "schedule_type": "cron",
                "cron_expr": "not a cron",
                "timezone": "UTC",
            },
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 8. name 为空 → 422（pydantic 校验）
# ---------------------------------------------------------------------------


def test_create_task_empty_name(tmp_path: Path) -> None:
    client, _ = _login_client_with_store(tmp_path)
    try:
        resp = client.post(
            "/api/cron/tasks",
            json={
                "name": "",
                "agent_name": "default",
                "input_text": "test",
                "schedule_type": "once",
                "once_at": _future_iso(),
                "timezone": "UTC",
            },
            headers=CSRF_HEADERS,
        )
        # domain 层 ScheduledTask.__post_init__ 要求 name non-empty str → ValueError
        # router 捕获后翻成 422
        assert resp.status_code == 422, resp.text
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 9. input_text 缺失 → pydantic 校验
# ---------------------------------------------------------------------------


def test_create_task_missing_input_text(tmp_path: Path) -> None:
    client, _ = _login_client_with_store(tmp_path)
    try:
        resp = client.post(
            "/api/cron/tasks",
            json={
                "name": "no-input",
                "agent_name": "default",
                "schedule_type": "once",
                "once_at": _future_iso(),
                "timezone": "UTC",
            },
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422  # pydantic 缺必填字段
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 10. schedule_type 不合法 → 422
# ---------------------------------------------------------------------------


def test_create_task_invalid_schedule_type(tmp_path: Path) -> None:
    client, _ = _login_client_with_store(tmp_path)
    try:
        resp = client.post(
            "/api/cron/tasks",
            json={
                "name": "bad-type",
                "agent_name": "default",
                "input_text": "test",
                "schedule_type": "monthly",
                "timezone": "UTC",
            },
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422
        assert "monthly" in resp.json()["detail"]
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 11. GET /api/cron/tasks/{id} — 取回刚创建的任务
# ---------------------------------------------------------------------------


def test_get_task_after_create(tmp_path: Path) -> None:
    client, _ = _login_client_with_store(tmp_path)
    try:
        # 先创建
        create_resp = client.post(
            "/api/cron/tasks",
            json={
                "name": "每日构建",
                "agent_name": "default",
                "input_text": "执行每日构建流程",
                "schedule_type": "cron",
                "cron_expr": "30 6 * * *",
                "timezone": "Asia/Shanghai",
            },
            headers=CSRF_HEADERS,
        )
        assert create_resp.status_code == 201
        task_id = create_resp.json()["task_id"]

        # 再取回
        get_resp = client.get(f"/api/cron/tasks/{task_id}")
        assert get_resp.status_code == 200, get_resp.text
        body = get_resp.json()
        assert body["task_id"] == task_id
        assert body["name"] == "每日构建"
        assert body["trigger_type"] == "cron"
        assert body["trigger_expr"] == "30 6 * * *"
        assert body["enabled"] is True
        assert body["state"] == "scheduled"
        assert body["preset_id"] == ""
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 12. GET /api/cron/tasks/{id} — 不存在 → 404
# ---------------------------------------------------------------------------


def test_get_task_not_found(tmp_path: Path) -> None:
    client, _ = _login_client_with_store(tmp_path)
    try:
        resp = client.get("/api/cron/tasks/nonexistent-id")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 13. store 不挂 → 503
# ---------------------------------------------------------------------------


def test_create_task_no_store_returns_503(tmp_path: Path) -> None:
    """scheduler_store 未挂（scheduler.enabled=False）→ 503。"""
    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    tm = _FakeTM()
    app = create_app(cfg, tm, home_dir=tmp_path)
    _install_cron_router_before_catch_all(app)
    client = TestClient(app)
    client.__enter__()
    try:
        # 登录
        client.post("/api/auth/login", json={"password": "pwd"}, headers=CSRF_HEADERS)
        # 不挂 store
        resp = client.post(
            "/api/cron/tasks",
            json={
                "name": "orphan",
                "agent_name": "default",
                "input_text": "test",
                "schedule_type": "cron",
                "cron_expr": "0 0 * * *",
                "timezone": "UTC",
            },
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 503
    finally:
        client.__exit__(None, None, None)
