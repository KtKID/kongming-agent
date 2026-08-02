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

from hosts.web.app import create_app
from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from hosts.web.threads.errors import ThreadForkConflictError
from hosts.web.threads.metadata import ThreadMetadata
from infrastructure.config.models import Config
from tests.unit.test_web_app_lifespan import _seed_password

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


class _FakeUsageManager:
    """task#3.3 minimal stub：``ThreadManager.usage_manager`` 在 router ``_to_dto``
    里被调 ``get_thread_summary(thread_id)`` 派生 ``usage_summary`` dict。

    本 stub 返回空 ``ThreadUsageSummary``（全零 anthropic），让 router 测试
    无需真实 manager。
    """

    async def get_thread_summary(self, thread_id: str):  # type: ignore[no-untyped-def]
        from hosts.web.usage_token import ThreadUsageSummary

        del thread_id
        return ThreadUsageSummary(channel="anthropic")


class FakeTM:
    """支持 thread CRUD 的 FakeThreadManager。"""

    def __init__(self) -> None:
        self._threads: dict[str, ThreadMetadata] = {}
        self.delete_calls: list[str] = []
        self.fork_calls: list[tuple[str, int | None]] = []
        self.first_message_calls: list[dict[str, Any]] = []
        self.fork_conflict = False
        self.fork_value_error = False
        self._started = False
        self._closed = False
        # task#3.3：router ``_to_dto`` 通过 ``tm.usage_manager.get_thread_summary``
        # 拿 token usage summary
        self.usage_manager = _FakeUsageManager()

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
        backend_kind: Literal["generic_chat", "claude_code", "codex"] = "generic_chat",
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

    async def create_generic_thread_from_first_message(
        self,
        *,
        text: str,
        preset_id: str,
        cwd: str = "",
        reasoning_effort: str | None = None,
    ) -> ThreadMetadata:
        if preset_id == "missing":
            raise ValueError("unknown preset_id: 'missing'")
        self.first_message_calls.append(
            {
                "text": text,
                "preset_id": preset_id,
                "cwd": cwd,
                "reasoning_effort": reasoning_effort,
            }
        )
        idx = len(self._threads)
        thread_id = f"thread-{'b' * 11}{idx}"
        meta = ThreadMetadata(
            id=thread_id,
            name=text.strip()[:40],
            preset_id=preset_id,
            backend_kind="generic_chat",
            cwd=cwd,
            created_at=1.0,
            updated_at=2.0,
            message_count=1,
        )
        self._threads[thread_id] = meta
        return meta

    async def fork_thread(
        self,
        source_thread_id: str,
        *,
        history_index: int | None = None,
    ) -> ThreadMetadata:
        if self.fork_conflict:
            raise ThreadForkConflictError("source thread is active")
        if self.fork_value_error:
            raise ValueError("full fork currently supports generic_chat threads")
        source = self._threads.get(source_thread_id)
        if source is None:
            raise KeyError(source_thread_id)
        self.fork_calls.append((source_thread_id, history_index))
        thread_id = f"thread-{'c' * 11}{len(self._threads)}"
        meta = source.model_copy(
            update={
                "id": thread_id,
                "name": f"{source.name}（分支）",
                "forked_from_id": source.id,
                "forked_from_history_index": history_index,
                "created_at": 3.0,
                "updated_at": 3.0,
                "is_pinned": False,
            }
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

    async def pin_thread(self, thread_id: str, is_pinned: bool) -> ThreadMetadata:
        if thread_id not in self._threads:
            raise KeyError(thread_id)
        old = self._threads[thread_id]
        new_meta = ThreadMetadata(
            id=old.id,
            name=old.name,
            preset_id=old.preset_id,
            created_at=old.created_at,
            updated_at=old.updated_at,
            message_count=old.message_count,
            is_pinned=is_pinned,
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

    def find_thread_by_claude_thread_id(self, claude_thread_id: str) -> Any:
        return None  # 默认无命中；测试可 monkeypatch

    async def bind_claude_thread(
        self,
        thread_id: str,
        claude_thread_id: str,
        cwd: str,
    ) -> Any:
        raise NotImplementedError  # 默认；测试 mock 时按需 override

    async def create_and_bind_claude_thread(
        self,
        *,
        claude_thread_id: str,
        cwd: str,
        name: str,
        preset_id: str = "",
    ) -> Any:
        """默认实现：复用 find / create / bind 三个旧方法，让历史测试 mock 仍然生效。

        历史 test_import_claude_session_endpoint 测试通过 mock
        ``find_thread_by_claude_thread_id`` / ``create_thread`` / ``bind_claude_thread``
        来验证防重复语义；router 改用 ``create_and_bind_claude_thread`` 后需要
        FakeTM 兜底翻译，否则那些测试全部 AttributeError。

        生产 ThreadManager 的实现含 per-ctid asyncio.Lock 串行化；FakeTM 不需要
        并发保护（测试用例顺序调用，不会真并发命中 race）。
        """
        existing = self.find_thread_by_claude_thread_id(claude_thread_id)
        if existing is not None:
            return existing, False
        new_thread = await self.create_thread(
            name,
            preset_id,
            backend_kind="claude_code",
            cwd=cwd,
        )
        bound = await self.bind_claude_thread(new_thread.id, claude_thread_id, cwd)
        return bound, True

    def resolve_approval(self, thread_id: str, call_id: str, approved: bool) -> None:
        del thread_id, call_id, approved
        return None


def _make_cfg() -> Config:
    return Config.model_validate(
        {
            "model": {
                "preset_id": "local-gemma-4-e4b-it",
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


def test_create_generic_thread_from_first_message_returns_thread(tmp_path: Path) -> None:
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        resp = client.post(
            "/api/threads/generic/first-message",
            json={
                "text": "hello",
                "preset_id": "p1",
                "cwd": str(tmp_path),
                "reasoning_effort": "low",
            },
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["thread"]["name"] == "hello"
        assert body["thread"]["preset_id"] == "p1"
        assert body["thread"]["message_count"] == 1
        assert body["thread"]["cwd"] == str(tmp_path)
        assert tm.first_message_calls == [
            {
                "text": "hello",
                "preset_id": "p1",
                "cwd": str(tmp_path),
                "reasoning_effort": "low",
            }
        ]
    finally:
        client.__exit__(None, None, None)


def test_create_generic_thread_from_first_message_manager_value_error_returns_400(
    tmp_path: Path,
) -> None:
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        resp = client.post(
            "/api/threads/generic/first-message",
            json={"text": "hello", "preset_id": "missing", "cwd": str(tmp_path)},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 400
        assert "unknown preset_id" in resp.text
    finally:
        client.__exit__(None, None, None)


def test_fork_thread_returns_201_with_lineage(tmp_path: Path) -> None:
    """REST 入口命中 Manager 关键 source_thread_id 并返回 lineage DTO。"""
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        created = client.post(
            "/api/threads",
            json={"name": "source", "preset_id": "p1"},
            headers=CSRF_HEADERS,
        )
        source_id = created.json()["id"]

        response = client.post(
            f"/api/threads/{source_id}/fork",
            json={"history_index": 3},
            headers=CSRF_HEADERS,
        )

        assert response.status_code == 201
        payload = response.json()
        assert payload["id"] != source_id
        assert payload["name"] == "source（分支）"
        assert payload["forked_from_id"] == source_id
        assert payload["forked_from_history_index"] == 3
        assert tm.fork_calls == [(source_id, 3)]
    finally:
        client.__exit__(None, None, None)


def test_fork_thread_active_source_returns_409(tmp_path: Path) -> None:
    """Manager 的完整快照冲突稳定映射为 HTTP 409。"""
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        created = client.post(
            "/api/threads",
            json={"name": "source", "preset_id": "p1"},
            headers=CSRF_HEADERS,
        )
        tm.fork_conflict = True
        response = client.post(
            f"/api/threads/{created.json()['id']}/fork",
            headers=CSRF_HEADERS,
        )
        assert response.status_code == 409
        assert "source thread is active" in response.text
    finally:
        client.__exit__(None, None, None)


def test_fork_thread_missing_source_returns_404(tmp_path: Path) -> None:
    """缺失 source_thread_id 稳定映射为 HTTP 404。"""
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        response = client.post(
            "/api/threads/thread-ffffffffffff/fork",
            headers=CSRF_HEADERS,
        )
        assert response.status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_fork_thread_invalid_source_id_returns_422_before_manager(
    tmp_path: Path,
) -> None:
    """非法 path 参数在 Manager 前被拒绝，避免创建任何目标 runtime。"""
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        response = client.post(
            "/api/threads/not-a-thread/fork",
            headers=CSRF_HEADERS,
        )
        assert response.status_code == 422
        assert tm.fork_calls == []
    finally:
        client.__exit__(None, None, None)


def test_fork_thread_unsupported_channel_returns_400(tmp_path: Path) -> None:
    """Provider 专属历史边界稳定映射为 HTTP 400。"""
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        created = client.post(
            "/api/threads",
            json={"name": "source", "preset_id": "p1"},
            headers=CSRF_HEADERS,
        )
        tm.fork_value_error = True
        response = client.post(
            f"/api/threads/{created.json()['id']}/fork",
            headers=CSRF_HEADERS,
        )
        assert response.status_code == 400
        assert "generic_chat" in response.text
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


def test_thread_permissions_get_put_and_revision_conflict(tmp_path: Path) -> None:
    """GET/PUT 走真实 PermissionsManager，stale revision 返回 409。"""
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        created = client.post(
            "/api/threads",
            json={"name": "permissions", "preset_id": "p1"},
            headers=CSRF_HEADERS,
        )
        thread_id = created.json()["id"]

        empty = client.get(f"/api/threads/{thread_id}/permissions")
        assert empty.status_code == 200
        assert empty.json() == {
            "schema_version": 2,
            "thread_id": thread_id,
            "revision": 0,
            "allow": [],
            "deny": [],
            "updated_at": None,
            "migration_summary": None,
        }

        saved = client.put(
            f"/api/threads/{thread_id}/permissions",
            json={
                "thread_id": thread_id,
                "revision": 0,
                "allow": [{"expression": "read_file", "scope_cwd": None}],
                "deny": [{"expression": "run_shell(curl:*)", "scope_cwd": None}],
            },
            headers=CSRF_HEADERS,
        )
        assert saved.status_code == 200
        assert saved.json()["revision"] == 1

        conflict = client.put(
            f"/api/threads/{thread_id}/permissions",
            json={
                "thread_id": thread_id,
                "revision": 0,
                "allow": [{"expression": "list_dir", "scope_cwd": None}],
                "deny": [],
            },
            headers=CSRF_HEADERS,
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "permissions_revision_conflict"

        unchanged = client.get(f"/api/threads/{thread_id}/permissions")
        assert unchanged.json()["revision"] == 1
        assert unchanged.json()["allow"] == [{"expression": "read_file", "scope_cwd": None}]
    finally:
        client.__exit__(None, None, None)


def test_thread_permissions_rejects_cross_thread_and_unknown_field(tmp_path: Path) -> None:
    """path/body 身份错位和未知字段均在路由合同层拒绝。"""
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        created = client.post(
            "/api/threads",
            json={"name": "permissions", "preset_id": "p1"},
            headers=CSRF_HEADERS,
        )
        thread_id = created.json()["id"]
        mismatch = client.put(
            f"/api/threads/{thread_id}/permissions",
            json={
                "thread_id": "thread-bbbbbbbbbbbb",
                "revision": 0,
                "allow": [],
                "deny": [],
            },
            headers=CSRF_HEADERS,
        )
        assert mismatch.status_code == 422
        assert mismatch.json()["detail"]["code"] == "thread_id_mismatch"

        unknown = client.put(
            f"/api/threads/{thread_id}/permissions",
            json={
                "thread_id": thread_id,
                "revision": 0,
                "allow": [],
                "deny": [],
                "scope": "global",
            },
            headers=CSRF_HEADERS,
        )
        assert unknown.status_code == 422
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


def test_pin_thread(tmp_path: Path) -> None:
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        cresp = client.post(
            "/api/threads",
            json={"name": "t", "preset_id": "p1"},
            headers=CSRF_HEADERS,
        )
        thread_id = cresp.json()["id"]
        assert cresp.json()["is_pinned"] is False

        resp = client.patch(
            f"/api/threads/{thread_id}",
            json={"is_pinned": True},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["is_pinned"] is True
    finally:
        client.__exit__(None, None, None)


def test_pin_nonexistent_returns_404(tmp_path: Path) -> None:
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        resp = client.patch(
            "/api/threads/thread-aaaaaaaaaaaa",
            json={"is_pinned": True},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 404
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
