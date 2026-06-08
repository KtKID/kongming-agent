"""scheduler 创建接口 e2e 诊断测试。

目标：诊断 POST /api/cron/tasks 在生产配置下返回 405 的问题。

与 unit 测试的区别：
  - scheduler.enabled=True（生产配置），让 create_app 走完整 lifespan
  - 不手动 _install_cron_router，依赖 create_app 内部的 include_router
  - 不手动挂 store，依赖 lifespan 里的 scheduler_store 挂载

适用：``uv run pytest tests/e2e/test_cron_create_e2e.py -v``
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from config_loader.models import Config
from scheduler.timing import to_iso
from tests.unit.test_web_app_lifespan import _seed_password
from web.app import create_app
from web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from web.threads.metadata import ThreadMetadata

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


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

    def list_threads(self):
        return list(self._threads.values())

    def list_cells(self):
        return []

    def get_cell(self, thread_id: str):
        return None

    def resolve_approval(self, thread_id: str, call_id: str, approved: bool) -> None:
        return None


def _future_iso(minutes: int = 5) -> str:
    return to_iso(datetime.now(UTC) + timedelta(minutes=minutes))


def _make_prod_cfg(tmp_path: Path) -> Config:
    """模拟生产配置：scheduler.enabled=True + dev_mode=True。"""
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
                "enabled": True,
            },
        }
    )


def _make_dev_cfg() -> Config:
    """开发配置：scheduler.enabled=False（不启动 ticker）。"""
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
                "enabled": False,
            },
        }
    )


# ---------------------------------------------------------------------------
# 诊断 1: scheduler.enabled=True（生产配置）
# ---------------------------------------------------------------------------


def test_create_with_scheduler_enabled(tmp_path: Path) -> None:
    """scheduler.enabled=True 时，POST /api/cron/tasks 应返回 201。

    诊断目标：验证 create_app 内部的 include_router(cron_router) 在
    scheduler.enabled=True 时正确注册了 POST /api/cron/tasks。
    """
    _seed_password(tmp_path, "pwd")
    cfg = _make_prod_cfg(tmp_path)
    tm = _FakeTM()
    app = create_app(cfg, tm, home_dir=tmp_path)

    # 不手动 _install_cron_router —— 依赖 create_app 内部的注册
    client = TestClient(app)
    client.__enter__()

    # 等待 lifespan 完成（scheduler ticker 会启动）

    # 登录
    resp = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert resp.status_code == 200, f"login failed: {resp.text}"

    # GET /api/cron/tasks — 先确认能拉到
    resp = client.get("/api/cron/tasks")
    assert resp.status_code == 200, f"GET failed: {resp.text}"

    # POST /api/cron/tasks — 创建每天任务
    resp = client.post(
        "/api/cron/tasks",
        json={
            "name": "每天测试",
            "agent_name": "default",
            "input_text": "创建 scheduler-test-daily.txt",
            "schedule_type": "cron",
            "cron_expr": "0 9 * * *",
            "timezone": "Asia/Shanghai",
        },
        headers=CSRF_HEADERS,
    )
    assert resp.status_code == 201, f"POST failed: {resp.status_code} {resp.text}"

    body = resp.json()
    assert body["name"] == "每天测试"
    assert body["trigger_type"] == "cron"
    assert body["trigger_expr"] == "0 9 * * *"

    client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 诊断 2: scheduler.enabled=False（开发配置，不启动 ticker）
# ---------------------------------------------------------------------------


def test_create_with_scheduler_disabled(tmp_path: Path) -> None:
    """scheduler.enabled=False 时，POST /api/cron/tasks 应返回 503（store 未挂）。

    如果返回 405 说明路由注册有问题。
    """
    _seed_password(tmp_path, "pwd")
    cfg = _make_dev_cfg()
    tm = _FakeTM()
    app = create_app(cfg, tm, home_dir=tmp_path)

    client = TestClient(app)
    client.__enter__()

    # 登录
    resp = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert resp.status_code == 200, f"login failed: {resp.text}"

    # POST /api/cron/tasks — store 未挂，应 503
    resp = client.post(
        "/api/cron/tasks",
        json={
            "name": "测试",
            "agent_name": "default",
            "input_text": "hello",
            "schedule_type": "cron",
            "cron_expr": "0 9 * * *",
            "timezone": "UTC",
        },
        headers=CSRF_HEADERS,
    )
    # 503 = store 未挂（正确）；405 = 路由没注册（bug）
    assert resp.status_code in (201, 503), (
        f"Expected 201 or 503, got {resp.status_code}: {resp.text}"
    )

    client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 诊断 3: 路由注册检查
# ---------------------------------------------------------------------------


def test_cron_routes_registered_in_app(tmp_path: Path) -> None:
    """检查 create_app 注册的 cron 路由是否包含 POST /api/cron/tasks。"""
    _seed_password(tmp_path, "pwd")
    cfg = _make_dev_cfg()
    tm = _FakeTM()
    app = create_app(cfg, tm, home_dir=tmp_path)

    cron_routes = []
    for r in app.routes:
        path = getattr(r, "path", getattr(r, "path_format", ""))
        methods = getattr(r, "methods", None)
        if "/api/cron" in str(path):
            cron_routes.append((str(methods), str(path)))

    # 至少要有 GET 和 POST /api/cron/tasks
    paths = [p for _, p in cron_routes]
    assert "/api/cron/tasks" in paths, f"/api/cron/tasks not found in: {paths}"

    # 检查 POST 方法存在
    post_found = any("POST" in m and p == "/api/cron/tasks" for m, p in cron_routes)
    assert post_found, f"POST /api/cron/tasks not found. Routes: {cron_routes}"
