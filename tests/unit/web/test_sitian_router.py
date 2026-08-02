"""``src/web/routers/sitian.py`` REST 路由单测（sitian-web-report-dialog #2）。

覆盖：

1. ``GET /api/sitian/report`` — 200 happy path（报告存在 → 返回 dict）
2. ``GET /api/sitian/report`` — 404（报告不存在 → ``ErrorResponseDTO(details.reason="no_report")``）
3. ``GET /api/sitian/report`` — 500（JSON 损坏 → ``ErrorResponseDTO(details.reason="report_corrupted")``）
4. ``GET /api/sitian/report`` — 500（JSON 合法但顶层非 object）
5. ``GET /api/sitian/report`` — 401（未登录 → AuthMiddleware 抢先 401）
6. ``GET /api/sitian/report`` — ``cfg.sitian.output_subdir`` 生效（落子目录）

装配辅助：
    复用 :mod:`tests.unit.web.test_cron_router` 的 ``_install_router_before_catch_all``
    思路：``create_app`` 末尾装了 dev_mode 下的 SPA catch-all，直接 ``include_router``
    会被抢先返 404；本测试模块独立写了一个等价 helper（避免跨 router 测试 coupling）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hosts.web.app import create_app
from hosts.web.routers.sitian import router as sitian_router
from infrastructure.config.models import Config
from sitian.store import SiTianRecordsStore
from tests.unit.test_web_app_lifespan import _seed_password

# ---------------------------------------------------------------------------
# Fake ThreadManager（与 cron router 测试同款最小桩）
# ---------------------------------------------------------------------------


class _FakeTM:
    def __init__(self) -> None:
        self._threads: dict[str, Any] = {}

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

    def list_threads(self) -> list[Any]:
        return []

    def list_cells(self) -> list[Any]:
        return []

    def get_cell(self, thread_id: str) -> Any:
        return None

    def resolve_approval(self, thread_id: str, call_id: str, approved: bool) -> None:
        return None


# ---------------------------------------------------------------------------
# app + client 装配
# ---------------------------------------------------------------------------


def _install_router_before_catch_all(app: Any) -> None:
    """把 :data:`sitian_router` 的所有 routes 插入到 SPA catch-all 之前。

    与 ``tests/unit/web/test_cron_router.py::_install_cron_router_before_catch_all``
    同款逻辑：FastAPI 按 ``app.routes`` 顺序匹配，``create_app`` 末尾装的
    ``GET /{full_path:path}`` SPA fallback 会把后注册的 router 抢先返 404。
    """
    before_count = len(app.router.routes)
    app.include_router(sitian_router)
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


def _make_cfg(*, output_subdir: str | None = None) -> Config:
    return Config.model_validate(
        {
            "model": {"preset_id": "fake-model"},
            "web": {
                "enabled": True,
                "dev_mode": True,
            },
            "scheduler": {
                "enabled": False,
            },
            "sitian": ({"output_subdir": output_subdir} if output_subdir is not None else {}),
        }
    )


def _login_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    output_subdir: str | None = None,
) -> TestClient:
    """构造已登录的 TestClient；让 ``resolve_sitian_root()`` 落到 tmp_path/sitian。

    - ``monkeypatch.setenv("KONGMING_HOME", str(tmp_path))``：sitian root 隔离
    - ``_seed_password(tmp_path, "pwd")``：让 AuthMiddleware 能通过
    - 登录拿 cookie（同 cron router 测试模式）
    """
    monkeypatch.setenv("KONGMING_HOME", str(tmp_path))

    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg(output_subdir=output_subdir)
    tm = _FakeTM()
    app = create_app(cfg, tm, home_dir=tmp_path)
    _install_router_before_catch_all(app)
    client = TestClient(app)
    client.__enter__()

    # CSRF：GET 不需要 CSRF header，且 login POST 在其它 router 测试里证实
    # 不需要单独发 csrf header。直接登录即可。
    from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE

    resp = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers={CSRF_HEADER_NAME: CSRF_HEADER_VALUE},
    )
    assert resp.status_code == 200, resp.text
    return client


def _make_store(tmp_path: Path, *, output_subdir: str | None = None) -> SiTianRecordsStore:
    """与 router 内部解析路径完全一致的 store。

    路径：
        - ``output_subdir is None`` → ``tmp_path/sitian/claude``（路由 fallback 默认 channel）
        - 否则 → ``tmp_path/sitian/<output_subdir>``

    与 ``src/web/routers/sitian.py::_DEFAULT_CHANNEL`` 保持同步。
    """
    base = tmp_path / "sitian"
    return SiTianRecordsStore(base / (output_subdir or "claude"))


# ---------------------------------------------------------------------------
# 测试：GET /api/sitian/report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_sitian_report_happy_returns_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """报告存在 → 200，返回原始 JSON dict。"""
    client = _login_client(tmp_path, monkeypatch)
    try:
        store = _make_store(tmp_path)
        payload: dict[str, object] = {
            "reportId": "rep-1",
            "generatedAt": "2026-05-16T12:00:00Z",
            "modelName": "fake",
            "summary": "all good",
            "errors": [],
            "topAlerts": [
                {
                    "alertId": "a-1",
                    "priority": "P0",
                    "severity": "high",
                    "title": "x",
                }
            ],
            "projects": [],
        }
        await store.save_sitian_report(payload)

        resp = client.get("/api/sitian/report")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == payload
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_get_sitian_report_not_found_returns_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """报告文件不存在 → 404 + ``ErrorResponseDTO(details.reason=no_report)``。"""
    client = _login_client(tmp_path, monkeypatch)
    try:
        # 完全不创建报告文件
        resp = client.get("/api/sitian/report")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error_code"] == "internal"
        assert body["details"] == {"reason": "no_report"}
        assert "not generated" in body["message"]
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_get_sitian_report_corrupted_returns_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON 损坏 → 500 + ``ErrorResponseDTO(details.reason=report_corrupted)``。"""
    client = _login_client(tmp_path, monkeypatch)
    try:
        store = _make_store(tmp_path)
        await store.ensure_layout()
        # 手动写损坏内容
        report_path = store.root_dir / "sitian_report.json"
        report_path.write_text("{not valid json", encoding="utf-8")

        resp = client.get("/api/sitian/report")
        assert resp.status_code == 500
        body = resp.json()
        assert body["error_code"] == "internal"
        assert body["details"] == {"reason": "report_corrupted"}
        assert "corrupted" in body["message"]
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_get_sitian_report_top_level_not_object_returns_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON 合法但顶层是 list / scalar → 500 report_corrupted。"""
    client = _login_client(tmp_path, monkeypatch)
    try:
        store = _make_store(tmp_path)
        await store.ensure_layout()
        report_path = store.root_dir / "sitian_report.json"
        report_path.write_text(json.dumps(["not", "object"]), encoding="utf-8")

        resp = client.get("/api/sitian/report")
        assert resp.status_code == 500
        body = resp.json()
        assert body["error_code"] == "internal"
        assert body["details"] == {"reason": "report_corrupted"}
    finally:
        client.__exit__(None, None, None)


def test_get_sitian_report_requires_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未登录 client → 401（AuthMiddleware）。

    不调 ``/api/auth/login`` 拿 cookie，直接发请求。
    """
    monkeypatch.setenv("KONGMING_HOME", str(tmp_path))
    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    tm = _FakeTM()
    app = create_app(cfg, tm, home_dir=tmp_path)
    _install_router_before_catch_all(app)
    client = TestClient(app)
    client.__enter__()
    try:
        resp = client.get("/api/sitian/report")
        assert resp.status_code == 401
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_get_sitian_report_respects_output_subdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cfg.sitian.output_subdir="claude"`` → router 从 ``<root>/claude/`` 读取。

    防止字段漂移：以后改 cfg 字段名时，本测试会失败提醒同步前端 / 路由。
    """
    client = _login_client(tmp_path, monkeypatch, output_subdir="claude")
    try:
        # 报告必须落到 sitian/claude/ 下才能被读到
        store = _make_store(tmp_path, output_subdir="claude")
        payload: dict[str, object] = {
            "reportId": "rep-claude",
            "summary": "channel claude",
            "errors": [],
            "topAlerts": [],
            "projects": [],
        }
        await store.save_sitian_report(payload)

        resp = client.get("/api/sitian/report")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["reportId"] == "rep-claude"

        # 验证隔离：落到 sitian/ 根（无 channel 子目录）的另一份不应该被读到
        other_store = SiTianRecordsStore(tmp_path / "sitian")  # 显式根，绕开 _make_store fallback
        other_payload: dict[str, object] = {"reportId": "rep-root"}
        await other_store.save_sitian_report(other_payload)

        resp2 = client.get("/api/sitian/report")
        assert resp2.status_code == 200
        # 仍然读 claude/ 的，不会被根的覆盖
        assert resp2.json()["reportId"] == "rep-claude"
    finally:
        client.__exit__(None, None, None)
