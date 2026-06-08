"""routers/manage.py 单测（v0.1.5）。

覆盖：

1. GET /api/manage/cells happy
2. POST /api/manage/cells/{id}/stop 存在 → 204 + tm.evict_cell 被调
3. stop 不存在 → 404
4. stop 非法 thread_id → 422
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from infrastructure.config.models import Config
from tests.unit.test_web_app_lifespan import _seed_password
from web.app import create_app
from web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from web.protocol import CellSummaryDTO

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


class FakeCell:
    def __init__(self, thread_id: str, *, chat_ws_connections: int = 0) -> None:
        self.thread_id = thread_id
        self.metadata = type(
            "Meta",
            (),
            {
                "backend_kind": "generic_chat",
                "cwd": "/tmp/demo",
            },
        )()
        self.adapter = type(
            "Adapter",
            (),
            {
                "_ws": type("Fanout", (), {"client_count": chat_ws_connections})(),
            },
        )()


class ManageFakeTM:
    def __init__(self) -> None:
        self._cells: dict[str, FakeCell] = {}
        self.evict_calls: list[tuple[str, str]] = []
        self._started = False
        self._closed = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        self._started = True

    async def aclose_all(self) -> None:
        self._closed = True

    async def create_thread(self, name: str, preset_id: str) -> Any:
        del name, preset_id
        raise NotImplementedError

    async def rename_thread(self, thread_id: str, new_name: str) -> Any:
        del thread_id, new_name
        raise NotImplementedError

    async def delete_thread(self, thread_id: str, *, keep_history: bool = False) -> None:
        del thread_id, keep_history
        return None

    async def boot_or_attach(self, thread_id: str) -> Any:
        del thread_id
        raise NotImplementedError

    async def evict_cell(
        self,
        thread_id: str,
        reason: str,
        message: str | None = None,
        *,
        notify_ws: bool = True,
    ) -> None:
        del message, notify_ws
        self.evict_calls.append((thread_id, reason))
        self._cells.pop(thread_id, None)

    def list_threads(self) -> list[Any]:
        return []

    def list_cells(self) -> list[CellSummaryDTO]:
        return [
            CellSummaryDTO(
                thread_id=c.thread_id,
                thread_name="t",
                preset_id="p1",
                created_at=1.0,
                last_active_at=2.0,
                pending_approval_count=0,
                status="idle",
            )
            for c in self._cells.values()
        ]

    def get_cell(self, thread_id: str) -> Any:
        return self._cells.get(thread_id)

    def resolve_approval(self, thread_id: str, call_id: str, approved: bool) -> None:
        del thread_id, call_id, approved
        return None

    def add_cell(self, thread_id: str, *, chat_ws_connections: int = 0) -> None:
        self._cells[thread_id] = FakeCell(
            thread_id,
            chat_ws_connections=chat_ws_connections,
        )


def _make_cfg() -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {"enabled": True, "dev_mode": True},
        }
    )


def _login(tmp_path: Path, tm: ManageFakeTM) -> TestClient:
    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    app = create_app(cfg, tm, home_dir=tmp_path)
    client = TestClient(app)
    client.__enter__()
    r = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert r.status_code == 200
    return client


def test_list_cells_empty(tmp_path: Path) -> None:
    tm = ManageFakeTM()
    client = _login(tmp_path, tm)
    try:
        resp = client.get("/api/manage/cells")
        assert resp.status_code == 200
        assert resp.json() == []
    finally:
        client.__exit__(None, None, None)


def test_list_cells_returns_dto(tmp_path: Path) -> None:
    tm = ManageFakeTM()
    tm.add_cell("thread-aaaaaaaaaaaa")
    client = _login(tmp_path, tm)
    try:
        resp = client.get("/api/manage/cells")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["thread_id"] == "thread-aaaaaaaaaaaa"
        assert body[0]["status"] == "idle"
    finally:
        client.__exit__(None, None, None)


def test_stop_cell_existing_returns_204(tmp_path: Path) -> None:
    tm = ManageFakeTM()
    tm.add_cell("thread-bbbbbbbbbbbb")
    client = _login(tmp_path, tm)
    try:
        resp = client.post(
            "/api/manage/cells/thread-bbbbbbbbbbbb/stop",
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 204
        assert ("thread-bbbbbbbbbbbb", "manual_stop") in tm.evict_calls
    finally:
        client.__exit__(None, None, None)


def test_runtime_status_returns_unified_snapshot(tmp_path: Path) -> None:
    tm = ManageFakeTM()
    tm.add_cell("thread-cccccccccccc", chat_ws_connections=2)
    client = _login(tmp_path, tm)
    try:
        resp = client.get("/api/manage/runtime-status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["polling"]["interval_seconds"] == 5
        assert body["cells_total"] == 1
        assert body["chat_ws_connections_total"] == 2
        assert body["global_ws"]["thread_status_connections"] == 0
        assert body["provider_sessions"]["claude_active_sessions"] == 0
        assert body["cells"][0]["thread_id"] == "thread-cccccccccccc"
        assert body["cells"][0]["chat_ws_connections"] == 2
        assert body["cells"][0]["backend_kind"] == "generic_chat"
    finally:
        client.__exit__(None, None, None)


def test_stop_cell_nonexistent_returns_404(tmp_path: Path) -> None:
    tm = ManageFakeTM()
    client = _login(tmp_path, tm)
    try:
        resp = client.post(
            "/api/manage/cells/thread-aaaaaaaaaaaa/stop",
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_stop_cell_invalid_id_returns_422(tmp_path: Path) -> None:
    tm = ManageFakeTM()
    client = _login(tmp_path, tm)
    try:
        resp = client.post(
            "/api/manage/cells/not-a-thread/stop",
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_unauthenticated_blocked(tmp_path: Path) -> None:
    tm = ManageFakeTM()
    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    app = create_app(cfg, tm, home_dir=tmp_path)
    with TestClient(app) as client:
        resp = client.get("/api/manage/cells")
        assert resp.status_code == 401
