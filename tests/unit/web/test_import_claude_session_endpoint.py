"""POST /api/threads/import-claude-session endpoint 单测（v0.2 #8）。

覆盖：
1. 未登录 → 401
2. cwd 不以 / 开头 → 422
3. 缺必填字段 → 422
4. 无现有绑定 → 创建新 thread + bind + imported=true
5. 已有绑定 → 跳现有 thread + imported=false（不调 create_thread）
6. DTO 字段完整透传
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.unit.test_web_app_lifespan import _seed_password
from tests.unit.test_web_routers_threads import CSRF_HEADERS, FakeTM, _login_client, _make_cfg
from web.app import create_app
from web.thread_metadata import ThreadMetadata


def _meta(thread_id: str, sdk_session_id: str = "", cwd: str = "") -> ThreadMetadata:
    return ThreadMetadata(
        id=thread_id,
        name="x",
        preset_id="",
        backend_kind="claude_code",
        sdk_session_id=sdk_session_id,
        cwd=cwd,
        created_at=1.0,
        updated_at=1.0,
        message_count=0,
    )


def test_unauthenticated_returns_401(tmp_path: Path) -> None:
    _seed_password(tmp_path, "pwd")
    tm = FakeTM()
    app = create_app(_make_cfg(), tm, home_dir=tmp_path)
    with TestClient(app) as client:
        resp = client.post(
            "/api/threads/import-claude-session",
            json={
                "sdk_session_id": "sid-1",
                "cwd": "/foo",
                "name": "x",
            },
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 401


def test_invalid_cwd_format_returns_422(tmp_path: Path) -> None:
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        resp = client.post(
            "/api/threads/import-claude-session",
            json={
                "sdk_session_id": "sid-1",
                "cwd": "relative/path",  # 不以 / 开头
                "name": "x",
            },
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_missing_required_fields_returns_422(tmp_path: Path) -> None:
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        # 缺 name
        resp = client.post(
            "/api/threads/import-claude-session",
            json={"sdk_session_id": "sid-1", "cwd": "/foo"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_creates_new_thread_when_unbound(tmp_path: Path) -> None:
    tm = FakeTM()
    # find_thread_by_sdk_session_id 默认返 None（未绑）
    # 实现 bind_sdk_session 把绑定信息写到 _threads 字典里
    bind_calls: list[tuple[str, str, str]] = []

    async def _fake_bind(thread_id: str, sdk_session_id: str, cwd: str) -> Any:
        bind_calls.append((thread_id, sdk_session_id, cwd))
        old = tm._threads[thread_id]
        new_meta = ThreadMetadata(
            id=old.id,
            name=old.name,
            preset_id=old.preset_id,
            backend_kind=old.backend_kind,
            sdk_session_id=sdk_session_id,
            cwd=cwd,
            created_at=old.created_at,
            updated_at=old.updated_at + 1.0,
            message_count=old.message_count,
        )
        tm._threads[thread_id] = new_meta
        return new_meta

    tm.bind_sdk_session = _fake_bind  # type: ignore[method-assign]

    client = _login_client(tmp_path, tm)
    try:
        resp = client.post(
            "/api/threads/import-claude-session",
            json={
                "sdk_session_id": "sid-new",
                "cwd": "/foo/bar",
                "name": "imported session",
            },
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["imported"] is True
        assert body["thread"]["sdk_session_id"] == "sid-new"
        assert body["thread"]["cwd"] == "/foo/bar"
        assert body["thread"]["name"] == "imported session"
        assert body["thread"]["backend_kind"] == "claude_code"
        # bind 被调用一次
        assert len(bind_calls) == 1
    finally:
        client.__exit__(None, None, None)


def test_returns_existing_thread_when_already_bound(tmp_path: Path) -> None:
    tm = FakeTM()
    existing = _meta(
        thread_id="thread-bbbbbbbbbbbb",
        sdk_session_id="sid-bound",
        cwd="/x/y",
    )
    tm._threads["thread-bbbbbbbbbbbb"] = existing

    # find_thread_by_sdk_session_id 命中
    def _find(sid: str) -> Any:
        if sid == "sid-bound":
            return existing
        return None

    tm.find_thread_by_sdk_session_id = _find  # type: ignore[method-assign]

    bind_calls: list[Any] = []

    async def _fake_bind(*args: Any, **kwargs: Any) -> Any:
        bind_calls.append((args, kwargs))
        raise AssertionError("不应调用 bind_sdk_session")

    tm.bind_sdk_session = _fake_bind  # type: ignore[method-assign]

    create_calls: list[Any] = []

    orig_create = tm.create_thread

    async def _fake_create(*args: Any, **kwargs: Any) -> Any:
        create_calls.append((args, kwargs))
        return await orig_create(*args, **kwargs)

    tm.create_thread = _fake_create  # type: ignore[method-assign]

    client = _login_client(tmp_path, tm)
    try:
        resp = client.post(
            "/api/threads/import-claude-session",
            json={
                "sdk_session_id": "sid-bound",
                "cwd": "/x/y",
                "name": "ignored",
            },
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["imported"] is False
        assert body["thread"]["id"] == "thread-bbbbbbbbbbbb"
        assert body["thread"]["sdk_session_id"] == "sid-bound"
        # 防重复：create / bind 都不调
        assert create_calls == []
        assert bind_calls == []
    finally:
        client.__exit__(None, None, None)


def test_dto_round_trip(tmp_path: Path) -> None:
    tm = FakeTM()

    async def _fake_bind(thread_id: str, sdk_session_id: str, cwd: str) -> Any:
        old = tm._threads[thread_id]
        new_meta = ThreadMetadata(
            id=old.id,
            name=old.name,
            preset_id=old.preset_id,
            backend_kind=old.backend_kind,
            sdk_session_id=sdk_session_id,
            cwd=cwd,
            created_at=old.created_at,
            updated_at=old.updated_at,
            message_count=old.message_count,
        )
        tm._threads[thread_id] = new_meta
        return new_meta

    tm.bind_sdk_session = _fake_bind  # type: ignore[method-assign]

    client = _login_client(tmp_path, tm)
    try:
        resp = client.post(
            "/api/threads/import-claude-session",
            json={
                "sdk_session_id": "sid-roundtrip",
                "cwd": "/work/dir",
                "name": "round trip",
            },
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        # 必含字段
        assert {"thread", "imported"} <= set(body.keys())
        thread = body["thread"]
        for k in (
            "id",
            "name",
            "preset_id",
            "backend_kind",
            "sdk_session_id",
            "cwd",
            "created_at",
            "updated_at",
            "message_count",
            "schema_version",
        ):
            assert k in thread, f"missing field {k}"
    finally:
        client.__exit__(None, None, None)
