"""``PATCH /api/cron/tasks/{task_id}`` 编辑链路单测（v0.5.3 cron-task-edit）。

覆盖：
  T1. 改 name → 返回 task 含新 name；其他字段不变
  T2. 改 schedule（"every 30s" → "0 9 * * *"）→ trigger.expr 更新 + next_run_at 重算
  T3. 改 preset_id → task.preset_id 更新；返回 DTO 含新值
  T4. 改 lifecycle → task.lifecycle 同步
  T5a. 404：task 不存在
  T5b. 422：schedule 非法
  T6. 入参全 None → 200 幂等返回当前 task（不写 audit）
  T7. 改 agent + input_text（嵌套 target 字段）
  T8. 改 concurrency_policy（嵌套 policy 字段）

测试策略：
  - 走真实 Store（tmp_path），不 mock
  - 走真实 TestClient，全链路 AuthMiddleware + CSRFMiddleware
  - 复用 test_cron_router.py 的装配函数和 fixture builder
"""

from __future__ import annotations

from pathlib import Path

from scheduler.domain import ConcurrencyPolicy, TaskLifecycleState, TriggerType
from tests.unit.web.test_cron_router import (
    CSRF_HEADERS,
    _login_client_with_store,
    _make_task,
    _trigger_interval,
    _trigger_once,
)


def _seed_one_task(
    store,
    *,
    task_id: str = "edit-1",
    name: str = "原任务名",
    preset_id: str = "preset-old",
) -> None:
    """种一条任务到 store，用于编辑测试的 baseline。"""
    task = _make_task(
        task_id=task_id,
        name=name,
        trigger=_trigger_interval(30),
        next_run_at="2026-05-09T13:00:00+00:00",
        preset_id=preset_id,
    )
    store.create_task(task)


# ---------------------------------------------------------------------------
# T1. 改 name
# ---------------------------------------------------------------------------


def test_patch_name_only(tmp_path: Path) -> None:
    """改 name → 新 name 落库，其他字段不变。"""
    client, store = _login_client_with_store(tmp_path)
    try:
        _seed_one_task(store, task_id="edit-1", name="原任务名")
        before = store.get_task("edit-1")
        assert before is not None
        before_trigger_expr = before.trigger.expr
        before_preset = before.preset_id
        before_agent = before.target.agent_name

        resp = client.patch(
            "/api/cron/tasks/edit-1",
            json={"name": "新任务名"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["task_id"] == "edit-1"
        assert body["name"] == "新任务名"
        # 其他字段保持
        assert body["trigger_expr"] == before_trigger_expr
        assert body["preset_id"] == before_preset

        # store 侧
        after = store.get_task("edit-1")
        assert after is not None
        assert after.name == "新任务名"
        assert after.target.agent_name == before_agent
        assert after.preset_id == before_preset
        assert after.trigger.expr == before_trigger_expr

        # audit
        audits = store.list_audits(task_id="edit-1")
        update_audits = [a for a in audits if a["action"] == "update"]
        assert len(update_audits) == 1
        assert "name" in update_audits[0]["payload"]["changed_fields"]
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T2. 改 schedule
# ---------------------------------------------------------------------------


def test_patch_schedule_updates_trigger_and_next_run_at(tmp_path: Path) -> None:
    """改 schedule → trigger.expr / trigger_type 更新 + next_run_at 重算。"""
    client, store = _login_client_with_store(tmp_path)
    try:
        _seed_one_task(store, task_id="edit-2")
        before = store.get_task("edit-2")
        assert before is not None
        assert before.trigger.trigger_type is TriggerType.INTERVAL
        before_next = before.next_run_at

        resp = client.patch(
            "/api/cron/tasks/edit-2",
            json={"schedule": "0 9 * * *"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["trigger_type"] == "cron"
        assert body["trigger_expr"] == "0 9 * * *"
        # next_run_at 必须重算（cron 触发时刻不同于 INTERVAL）
        assert body["next_run_at"] is not None
        assert body["next_run_at"] != before_next

        # store 侧
        after = store.get_task("edit-2")
        assert after is not None
        assert after.trigger.trigger_type is TriggerType.CRON
        assert after.trigger.expr == "0 9 * * *"
        # timezone 沿用原 task（INTERVAL 默认 UTC，本测试 _trigger_interval 用 UTC）
        assert after.trigger.timezone == before.trigger.timezone
        assert after.next_run_at != before_next
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T3. 改 preset_id
# ---------------------------------------------------------------------------


def test_patch_preset_id(tmp_path: Path) -> None:
    """改 preset_id → 落库 + DTO 反映新值。"""
    client, store = _login_client_with_store(tmp_path)
    try:
        _seed_one_task(store, task_id="edit-3", preset_id="preset-old")

        resp = client.patch(
            "/api/cron/tasks/edit-3",
            json={"preset_id": "preset-new"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["preset_id"] == "preset-new"

        after = store.get_task("edit-3")
        assert after is not None
        assert after.preset_id == "preset-new"
    finally:
        client.__exit__(None, None, None)


def test_patch_preset_id_empty_string(tmp_path: Path) -> None:
    """改 preset_id="" → 清空 preset；表达"用全局默认 preset"。"""
    client, store = _login_client_with_store(tmp_path)
    try:
        _seed_one_task(store, task_id="edit-3b", preset_id="preset-old")

        resp = client.patch(
            "/api/cron/tasks/edit-3b",
            json={"preset_id": ""},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["preset_id"] == ""

        after = store.get_task("edit-3b")
        assert after is not None
        assert after.preset_id == ""
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T4. 改 lifecycle
# ---------------------------------------------------------------------------


def test_patch_lifecycle_disabled(tmp_path: Path) -> None:
    """改 lifecycle=disabled 后单一真源同步到 DTO 与 Store。"""
    client, store = _login_client_with_store(tmp_path)
    try:
        _seed_one_task(store, task_id="edit-4")
        before = store.get_task("edit-4")
        assert before is not None
        assert before.lifecycle is TaskLifecycleState.SCHEDULED

        resp = client.patch(
            "/api/cron/tasks/edit-4",
            json={"lifecycle": "disabled"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["lifecycle"] == "disabled"

        after = store.get_task("edit-4")
        assert after is not None
        assert after.lifecycle is TaskLifecycleState.DISABLED
    finally:
        client.__exit__(None, None, None)


def test_patch_lifecycle_scheduled_resumes_disabled_task(tmp_path: Path) -> None:
    """改 lifecycle=scheduled 后 disabled 任务恢复调度。"""
    client, store = _login_client_with_store(tmp_path)
    try:
        # 先种一个 disabled 任务
        task = _make_task(
            task_id="edit-4b",
            lifecycle=TaskLifecycleState.DISABLED,
            trigger=_trigger_interval(30),
            next_run_at="2026-05-09T13:00:00+00:00",
        )
        store.create_task(task)

        resp = client.patch(
            "/api/cron/tasks/edit-4b",
            json={"lifecycle": "scheduled"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["lifecycle"] == "scheduled"

        after = store.get_task("edit-4b")
        assert after is not None
        assert after.lifecycle is TaskLifecycleState.SCHEDULED
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T5a. 404 task 不存在
# ---------------------------------------------------------------------------


def test_patch_404_task_not_found(tmp_path: Path) -> None:
    client, _ = _login_client_with_store(tmp_path)
    try:
        resp = client.patch(
            "/api/cron/tasks/nonexistent",
            json={"name": "any"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 404, resp.text
        assert "not found" in resp.json()["detail"].lower()
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T5b. 422 schedule 非法
# ---------------------------------------------------------------------------


def test_patch_422_invalid_schedule(tmp_path: Path) -> None:
    client, store = _login_client_with_store(tmp_path)
    try:
        _seed_one_task(store, task_id="edit-5b")

        resp = client.patch(
            "/api/cron/tasks/edit-5b",
            json={"schedule": "this is not a schedule"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422, resp.text

        # store 侧应保持原状（解析失败时不写盘）
        after = store.get_task("edit-5b")
        assert after is not None
        assert after.trigger.trigger_type is TriggerType.INTERVAL
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T6. 入参全 None
# ---------------------------------------------------------------------------


def test_patch_empty_body_returns_current(tmp_path: Path) -> None:
    """入参全空（body={}）→ 200 + 当前 task 原样返回 + 不写 audit。"""
    client, store = _login_client_with_store(tmp_path)
    try:
        _seed_one_task(store, task_id="edit-6", name="原始名")
        before_audits = store.list_audits(task_id="edit-6")
        before_update_count = sum(1 for a in before_audits if a["action"] == "update")

        resp = client.patch(
            "/api/cron/tasks/edit-6",
            json={},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "原始名"

        # 不写 audit
        after_audits = store.list_audits(task_id="edit-6")
        after_update_count = sum(1 for a in after_audits if a["action"] == "update")
        assert after_update_count == before_update_count
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T7. 改 agent + input_text（嵌套 target）
# ---------------------------------------------------------------------------


def test_patch_agent_and_input_text(tmp_path: Path) -> None:
    """同时改 agent + input_text → 嵌套 target 局部覆盖。"""
    client, store = _login_client_with_store(tmp_path)
    try:
        _seed_one_task(store, task_id="edit-7")
        before = store.get_task("edit-7")
        assert before is not None
        before_metadata = dict(before.target.metadata)

        resp = client.patch(
            "/api/cron/tasks/edit-7",
            json={"agent": "researcher", "input_text": "新的提示词"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200, resp.text

        after = store.get_task("edit-7")
        assert after is not None
        assert after.target.agent_name == "researcher"
        assert after.target.input_text == "新的提示词"
        # metadata 不应被清空
        assert dict(after.target.metadata) == before_metadata
    finally:
        client.__exit__(None, None, None)


def test_patch_only_input_text_keeps_agent(tmp_path: Path) -> None:
    """只改 input_text → agent_name 不变。"""
    client, store = _login_client_with_store(tmp_path)
    try:
        _seed_one_task(store, task_id="edit-7b")
        before = store.get_task("edit-7b")
        assert before is not None
        before_agent = before.target.agent_name

        resp = client.patch(
            "/api/cron/tasks/edit-7b",
            json={"input_text": "仅改输入"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200, resp.text

        after = store.get_task("edit-7b")
        assert after is not None
        assert after.target.agent_name == before_agent
        assert after.target.input_text == "仅改输入"
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T8. 改 concurrency_policy（嵌套 policy）
# ---------------------------------------------------------------------------


def test_patch_concurrency_policy(tmp_path: Path) -> None:
    client, store = _login_client_with_store(tmp_path)
    try:
        _seed_one_task(store, task_id="edit-8")
        before = store.get_task("edit-8")
        assert before is not None
        assert before.policy.concurrency_policy is ConcurrencyPolicy.FORBID

        resp = client.patch(
            "/api/cron/tasks/edit-8",
            json={"concurrency_policy": "replace"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200, resp.text

        after = store.get_task("edit-8")
        assert after is not None
        assert after.policy.concurrency_policy is ConcurrencyPolicy.REPLACE
        # 其它 policy 字段不变
        assert after.policy.session_mode == before.policy.session_mode
        assert after.policy.misfire_policy == before.policy.misfire_policy
        assert after.policy.retry_limit == before.policy.retry_limit
    finally:
        client.__exit__(None, None, None)


def test_patch_invalid_concurrency_policy(tmp_path: Path) -> None:
    """Literal 校验在 pydantic 层挡掉，返回 422（FastAPI 自动）。"""
    client, store = _login_client_with_store(tmp_path)
    try:
        _seed_one_task(store, task_id="edit-8b")
        resp = client.patch(
            "/api/cron/tasks/edit-8b",
            json={"concurrency_policy": "bogus"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T9. v0.5.4 DTO 含 input_text + agent_name
# ---------------------------------------------------------------------------


def test_dto_includes_input_text_and_agent_name(tmp_path: Path) -> None:
    """v0.5.4: GET / PATCH 返回的 CronTaskDTO 必须含 input_text + agent_name。

    前端编辑弹窗根据这两个字段回填表单；缺失会导致用户改 schedule 提交时
    误把 input_text 清空。
    """
    client, store = _login_client_with_store(tmp_path)
    try:
        _seed_one_task(store, task_id="edit-9")
        # 验证 store 侧 baseline（_make_task 默认 target=("default", "ping")）
        before = store.get_task("edit-9")
        assert before is not None
        assert before.target.agent_name == "default"
        assert before.target.input_text == "ping"

        # GET /tasks
        resp = client.get("/api/cron/tasks")
        assert resp.status_code == 200, resp.text
        items = resp.json()
        edited = next(t for t in items if t["task_id"] == "edit-9")
        assert edited["agent_name"] == "default"
        assert edited["input_text"] == "ping"

        # GET /tasks/{id}
        resp = client.get("/api/cron/tasks/edit-9")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["agent_name"] == "default"
        assert body["input_text"] == "ping"

        # PATCH 改 input_text 后 DTO 必须反映新值
        resp = client.patch(
            "/api/cron/tasks/edit-9",
            json={"input_text": "新输入"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["agent_name"] == "default"
        assert body["input_text"] == "新输入"
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T10. v0.5.4 ONCE 已完成任务改 schedule 自动复活
# ---------------------------------------------------------------------------


def test_patch_revives_exhausted_once_task_when_schedule_changes(tmp_path: Path) -> None:
    """EXHAUSTED one-shot 改 schedule 时开启新的 SCHEDULED 生命周期。"""
    client, store = _login_client_with_store(tmp_path)
    try:
        # 1. 创建 SCHEDULED one-shot
        task = _make_task(
            task_id="revive-1",
            trigger=_trigger_once("2026-05-09T13:00:00+00:00"),
            next_run_at="2026-05-09T13:00:00+00:00",
        )
        store.create_task(task)

        # 2. 模拟已领取并完成
        store.update_task(
            "revive-1",
            lifecycle=TaskLifecycleState.EXHAUSTED,
            last_run_at="2026-05-09T13:00:05+00:00",
        )
        before = store.get_task("revive-1")
        assert before is not None
        assert before.lifecycle is TaskLifecycleState.EXHAUSTED
        assert before.last_run_at is not None

        # 3. PATCH 改 schedule（新 ONCE 时间）
        resp = client.patch(
            "/api/cron/tasks/revive-1",
            json={"schedule": "2026-06-01T09:00:00+00:00"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # 4. DTO 反映复活
        assert body["lifecycle"] == "scheduled"
        assert body["last_run_at"] is None
        # next_run_at 被重算为新 schedule 的时间（不是 2026-05-09）
        assert body["next_run_at"] is not None
        assert body["next_run_at"].startswith("2026-06-01")
        assert body["trigger_expr"] == "2026-06-01T09:00:00+00:00"

        # 5. store 侧同步
        after = store.get_task("revive-1")
        assert after is not None
        assert after.lifecycle is TaskLifecycleState.SCHEDULED
        assert after.last_run_at is None
        assert after.trigger.trigger_type is TriggerType.ONCE
    finally:
        client.__exit__(None, None, None)


def test_patch_does_not_revive_when_schedule_not_changed(tmp_path: Path) -> None:
    """仅改 name 时 EXHAUSTED 生命周期保持原状。

    这是复活逻辑的边界条件：复活只在用户明确表达"我要让它再跑"（改 schedule）
    时才发生；改个名字不该意外重启已完成任务。
    """
    client, store = _login_client_with_store(tmp_path)
    try:
        task = _make_task(
            task_id="revive-2",
            trigger=_trigger_once("2026-05-09T13:00:00+00:00"),
            next_run_at="2026-05-09T13:00:00+00:00",
        )
        store.create_task(task)
        store.update_task(
            "revive-2",
            lifecycle=TaskLifecycleState.EXHAUSTED,
            last_run_at="2026-05-09T13:00:05+00:00",
        )

        resp = client.patch(
            "/api/cron/tasks/revive-2",
            json={"name": "改个名"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "改个名"
        assert body["lifecycle"] == "exhausted"
        assert body["last_run_at"] == "2026-05-09T13:00:05+00:00"

        after = store.get_task("revive-2")
        assert after is not None
        assert after.lifecycle is TaskLifecycleState.EXHAUSTED
        assert after.last_run_at == "2026-05-09T13:00:05+00:00"
    finally:
        client.__exit__(None, None, None)


def test_patch_does_not_revive_recurring_task(tmp_path: Path) -> None:
    """v0.5.4: INTERVAL（recurring）任务改 schedule 不走复活分支。

    recurring 有自己的 next_run_at 节拍：trigger 重算 + next_run_at 重算就够了；
    lifecycle 应保持 PAUSED。
    """
    client, store = _login_client_with_store(tmp_path)
    try:
        # 种一个手动暂停的 INTERVAL 任务
        task = _make_task(
            task_id="revive-3",
            lifecycle=TaskLifecycleState.PAUSED,
            trigger=_trigger_interval(30),
            next_run_at="2026-05-09T13:00:00+00:00",
        )
        store.create_task(task)

        resp = client.patch(
            "/api/cron/tasks/revive-3",
            json={"schedule": "every 2m"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # trigger / next_run_at 更新
        assert body["trigger_type"] == "interval"
        assert body["next_run_at"] is not None
        assert body["lifecycle"] == "paused"

        after = store.get_task("revive-3")
        assert after is not None
        assert after.lifecycle is TaskLifecycleState.PAUSED
    finally:
        client.__exit__(None, None, None)
