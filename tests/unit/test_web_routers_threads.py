"""routers/threads.py 单测（v0.1.5）。

覆盖：

1. GET /api/threads happy → 列表
2. POST /api/threads 创建 → 201 + DTO
3. PATCH 重命名 → 200 + DTO
4. DELETE 不存在 → 404
5. DELETE 存在 → 204
6. thread_id 不匹配正则 → 422 InvalidThreadIdError
7. 缺 cookie → 401（middleware 拦）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi.testclient import TestClient

from config_loader.models import Config
from tests.unit.test_web_app_lifespan import _seed_password
from web.app import create_app
from web.auth import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from web.thread_metadata import ThreadMetadata

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


class FakeTM:
    """支持 thread CRUD 的 FakeThreadManager。"""

    def __init__(self) -> None:
        self._threads: dict[str, ThreadMetadata] = {}
        self.delete_calls: list[str] = []
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

    async def create_thread(
        self,
        name: str,
        preset_id: str = "",
        *,
        backend_kind: Literal["generic_chat", "claude_code"] = "generic_chat",
        cwd: str = "",
    ) -> ThreadMetadata:
        # 用确定 ID 便于断言
        idx = len(self._threads)
        thread_id = f"thread-{'a' * 11}{idx}"
        meta = ThreadMetadata(
            id=thread_id,
            name=name,
            preset_id=preset_id,
            backend_kind=backend_kind,
            cwd=cwd,
            created_at=1.0,
            updated_at=2.0,
            message_count=0,
        )
        self._threads[thread_id] = meta
        return meta

    async def rename_thread(self, thread_id: str, new_name: str) -> ThreadMetadata:
        if thread_id not in self._threads:
            raise KeyError(thread_id)
        old = self._threads[thread_id]
        new_meta = ThreadMetadata(
            id=old.id,
            name=new_name,
            preset_id=old.preset_id,
            created_at=old.created_at,
            updated_at=old.updated_at + 1.0,
            message_count=old.message_count,
        )
        self._threads[thread_id] = new_meta
        return new_meta

    async def delete_thread(self, thread_id: str, *, keep_history: bool = False) -> None:
        del keep_history
        self.delete_calls.append(thread_id)
        self._threads.pop(thread_id, None)

    async def boot_or_attach(self, thread_id: str) -> Any:
        raise NotImplementedError

    async def evict_cell(
        self,
        thread_id: str,
        reason: str,
        message: str | None = None,
        *,
        notify_ws: bool = True,
    ) -> None:
        del thread_id, reason, message, notify_ws
        return None

    def list_threads(self) -> list[ThreadMetadata]:
        return list(self._threads.values())

    def list_cells(self) -> list[Any]:
        return []

    def get_cell(self, thread_id: str) -> Any:
        return None

    def find_thread_by_sdk_session_id(self, sdk_session_id: str) -> Any:
        return None  # 默认无命中；测试可 monkeypatch

    async def bind_sdk_session(
        self,
        thread_id: str,
        sdk_session_id: str,
        cwd: str,
    ) -> Any:
        raise NotImplementedError  # 默认；测试 mock 时按需 override

    def resolve_approval(self, thread_id: str, call_id: str, approved: bool) -> None:
        del thread_id, call_id, approved
        return None


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
        }
    )


def _login_client(tmp_path: Path, tm: FakeTM) -> TestClient:
    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    app = create_app(cfg, tm, home_dir=tmp_path)
    client = TestClient(app)
    client.__enter__()  # 进入 lifespan
    resp = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert resp.status_code == 200
    return client


def test_list_threads_empty(tmp_path: Path) -> None:
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        resp = client.get("/api/threads")
        assert resp.status_code == 200
        assert resp.json() == []
    finally:
        client.__exit__(None, None, None)


def test_create_thread_returns_201(tmp_path: Path) -> None:
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        resp = client.post(
            "/api/threads",
            json={"name": "t1", "preset_id": "p1"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "t1"
        assert body["preset_id"] == "p1"
        assert body["id"].startswith("thread-")
    finally:
        client.__exit__(None, None, None)


def test_rename_thread(tmp_path: Path) -> None:
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        # 先创建
        cresp = client.post(
            "/api/threads",
            json={"name": "old", "preset_id": "p1"},
            headers=CSRF_HEADERS,
        )
        thread_id = cresp.json()["id"]

        # 重命名
        resp = client.patch(
            f"/api/threads/{thread_id}",
            json={"name": "new-name"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "new-name"
    finally:
        client.__exit__(None, None, None)


def test_rename_nonexistent_returns_404(tmp_path: Path) -> None:
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        resp = client.patch(
            "/api/threads/thread-aaaaaaaaaaaa",
            json={"name": "x"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_delete_thread(tmp_path: Path) -> None:
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        cresp = client.post(
            "/api/threads",
            json={"name": "t", "preset_id": "p1"},
            headers=CSRF_HEADERS,
        )
        thread_id = cresp.json()["id"]

        resp = client.delete(
            f"/api/threads/{thread_id}",
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 204
        assert thread_id in tm.delete_calls
    finally:
        client.__exit__(None, None, None)


def test_delete_nonexistent_returns_404(tmp_path: Path) -> None:
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        resp = client.delete(
            "/api/threads/thread-aaaaaaaaaaaa",
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_invalid_thread_id_returns_422(tmp_path: Path) -> None:
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        resp = client.delete(
            "/api/threads/not-a-thread-id",
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_unauthenticated_returns_401(tmp_path: Path) -> None:
    tm = FakeTM()
    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    app = create_app(cfg, tm, home_dir=tmp_path)
    with TestClient(app) as client:
        # 不登录
        resp = client.get("/api/threads")
        assert resp.status_code == 401
