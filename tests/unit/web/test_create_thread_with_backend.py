"""POST /api/threads + ThreadManager.create_thread 加 backend_kind 字段单测（v0.1.6）。

覆盖：

1. ThreadManager.create_thread 默认 backend_kind="generic_chat"
2. ThreadManager.create_thread backend_kind="claude_code" 允许空 preset_id
3. ThreadManager.create_thread backend_kind="generic_chat" 空 preset_id → ValueError
4. POST /api/threads 默认 → 创建 generic_chat
5. POST /api/threads backend_kind="claude_code" + 空 preset_id → 201
6. POST /api/threads backend_kind="generic_chat" + 空 preset_id → 400
7. GET /api/threads 返回 backend_kind 字段
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from config_loader.models import Config
from tests.unit.test_web_app_lifespan import _seed_password
from web.app import create_app
from web.auth import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from web.thread_metadata import ThreadMetadata

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


class FakeTM:
    """支持 thread CRUD 的 FakeThreadManager（v0.1.6 加 backend_kind 字段）。"""

    def __init__(self) -> None:
        self._threads: dict[str, ThreadMetadata] = {}
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
        backend_kind: str = "generic_chat",
        cwd: str = "",
    ) -> ThreadMetadata:
        # 这里复刻真实 ThreadManager 的校验逻辑，让单测能验证路由层 + manager 都校验
        if not name or not name.strip():
            raise ValueError("thread name must not be empty")
        if backend_kind == "generic_chat" and (not preset_id or not preset_id.strip()):
            raise ValueError("preset_id required for generic_chat backend")
        idx = len(self._threads)
        # 12 位 hex；用 idx 占位，前 11 位填 0
        thread_id = f"thread-{idx:012x}"
        meta = ThreadMetadata(
            id=thread_id,
            name=name,
            preset_id=preset_id,
            backend_kind=backend_kind,  # type: ignore[arg-type]
            cwd=cwd,
            created_at=1.0,
            updated_at=2.0,
            message_count=0,
        )
        self._threads[thread_id] = meta
        return meta

    async def rename_thread(self, thread_id: str, new_name: str) -> ThreadMetadata:
        raise NotImplementedError

    async def delete_thread(self, thread_id: str, *, keep_history: bool = False) -> None:
        del keep_history
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
    client.__enter__()
    resp = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert resp.status_code == 200
    return client


# ---------------------------------------------------------------------------
# ThreadManager 层（直接调，不走 HTTP）
# ---------------------------------------------------------------------------


def test_thread_manager_create_thread_signature() -> None:
    """ThreadManager.create_thread 真实签名校验（不只是 FakeTM）。

    通过反射 inspect 真实 ThreadManager 类的 create_thread 签名，确认含 backend_kind kw-only。
    """
    import inspect

    from web.thread_manager import ThreadManager

    sig = inspect.signature(ThreadManager.create_thread)
    params = sig.parameters
    assert "preset_id" in params
    assert params["preset_id"].default == ""
    assert "backend_kind" in params
    bk = params["backend_kind"]
    assert bk.kind == inspect.Parameter.KEYWORD_ONLY
    assert bk.default == "generic_chat"


@pytest.mark.asyncio
async def test_real_create_thread_default_generic_chat(tmp_path: Path) -> None:
    """ThreadManager 真实实例：默认创建 generic_chat。"""
    from web.thread_manager import ThreadManager

    cfg = _make_cfg()

    async def _fake_factory(tid: str, pid: str, adapter: Any, sinks: list[Any]) -> tuple[Any, Any]:
        del tid, pid, adapter, sinks
        raise NotImplementedError

    tm = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=_fake_factory)
    meta = await tm.create_thread("hello", "preset-1")
    assert meta.backend_kind == "generic_chat"
    assert meta.preset_id == "preset-1"
    assert meta.cwd == ""


@pytest.mark.asyncio
async def test_real_create_thread_with_workspace_cwd(tmp_path: Path) -> None:
    """ThreadManager 真实实例：创建时可持久化 cwd。"""
    from web.thread_manager import ThreadManager

    cfg = _make_cfg()

    async def _fake_factory(tid: str, pid: str, adapter: Any, sinks: list[Any]) -> tuple[Any, Any]:
        del tid, pid, adapter, sinks
        raise NotImplementedError

    tm = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=_fake_factory)
    meta = await tm.create_thread("hello", "preset-1", cwd="/tmp/project-a")
    assert meta.cwd == "/tmp/project-a"


@pytest.mark.asyncio
async def test_real_create_thread_claude_code_empty_preset_ok(tmp_path: Path) -> None:
    """ThreadManager 真实实例：claude_code 允许空 preset_id。"""
    from web.thread_manager import ThreadManager

    cfg = _make_cfg()

    async def _fake_factory(tid: str, pid: str, adapter: Any, sinks: list[Any]) -> tuple[Any, Any]:
        del tid, pid, adapter, sinks
        raise NotImplementedError

    tm = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=_fake_factory)
    meta = await tm.create_thread("c", "", backend_kind="claude_code")
    assert meta.backend_kind == "claude_code"
    assert meta.preset_id == ""


@pytest.mark.asyncio
async def test_real_create_thread_generic_chat_empty_preset_raises(
    tmp_path: Path,
) -> None:
    """ThreadManager 真实实例：generic_chat + 空 preset_id → ValueError。"""
    from web.thread_manager import ThreadManager

    cfg = _make_cfg()

    async def _fake_factory(tid: str, pid: str, adapter: Any, sinks: list[Any]) -> tuple[Any, Any]:
        del tid, pid, adapter, sinks
        raise NotImplementedError

    tm = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=_fake_factory)
    with pytest.raises(ValueError, match="preset_id required"):
        await tm.create_thread("x", "")


# ---------------------------------------------------------------------------
# REST 层
# ---------------------------------------------------------------------------


def test_post_threads_default_creates_generic_chat(tmp_path: Path) -> None:
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
        assert body["backend_kind"] == "generic_chat"
        assert body["preset_id"] == "p1"
        assert body["cwd"] == ""
    finally:
        client.__exit__(None, None, None)


def test_post_threads_generic_chat_with_cwd(tmp_path: Path) -> None:
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        resp = client.post(
            "/api/threads",
            json={"name": "t1", "preset_id": "p1", "cwd": "/tmp/project-a"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["cwd"] == "/tmp/project-a"
    finally:
        client.__exit__(None, None, None)


def test_post_threads_explicit_generic_chat(tmp_path: Path) -> None:
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        resp = client.post(
            "/api/threads",
            json={"name": "t1", "preset_id": "p1", "backend_kind": "generic_chat"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["backend_kind"] == "generic_chat"
    finally:
        client.__exit__(None, None, None)


def test_post_threads_claude_code_without_preset_id(tmp_path: Path) -> None:
    """claude_code 路径不强制 preset_id。"""
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        resp = client.post(
            "/api/threads",
            json={"name": "claude-t", "backend_kind": "claude_code"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["backend_kind"] == "claude_code"
        assert body["preset_id"] == ""
    finally:
        client.__exit__(None, None, None)


def test_post_threads_generic_chat_missing_preset_id_returns_400(
    tmp_path: Path,
) -> None:
    """generic_chat + 空 preset_id → 400。"""
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        resp = client.post(
            "/api/threads",
            json={"name": "t", "preset_id": "", "backend_kind": "generic_chat"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 400
        # 错误 detail 里含 "preset_id"
        body = resp.json()
        msg = str(body)
        assert "preset_id" in msg
    finally:
        client.__exit__(None, None, None)


def test_post_threads_generic_chat_omitted_preset_id_returns_400(
    tmp_path: Path,
) -> None:
    """generic_chat + 完全省略 preset_id → 同样 400。"""
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        resp = client.post(
            "/api/threads",
            json={"name": "t"},  # 完全没传 preset_id / backend_kind
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 400
    finally:
        client.__exit__(None, None, None)


def test_post_threads_relative_cwd_returns_400(tmp_path: Path) -> None:
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        resp = client.post(
            "/api/threads",
            json={"name": "t", "preset_id": "p1", "cwd": "relative/path"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 400
        assert "cwd" in str(resp.json())
    finally:
        client.__exit__(None, None, None)


def test_get_threads_includes_backend_kind(tmp_path: Path) -> None:
    """GET /api/threads 返回项含 backend_kind 字段。"""
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        # 创建一个 generic_chat + 一个 claude_code
        client.post(
            "/api/threads",
            json={"name": "g", "preset_id": "p1"},
            headers=CSRF_HEADERS,
        )
        client.post(
            "/api/threads",
            json={"name": "c", "backend_kind": "claude_code"},
            headers=CSRF_HEADERS,
        )
        resp = client.get("/api/threads")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 2
        backends = sorted(it["backend_kind"] for it in items)
        assert backends == ["claude_code", "generic_chat"]
    finally:
        client.__exit__(None, None, None)


def test_post_threads_invalid_backend_kind_returns_422(tmp_path: Path) -> None:
    """不在 Literal 里的 backend_kind → 422 (Pydantic 校验失败)。"""
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        resp = client.post(
            "/api/threads",
            json={"name": "t", "preset_id": "p", "backend_kind": "codex"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422
    finally:
        client.__exit__(None, None, None)
