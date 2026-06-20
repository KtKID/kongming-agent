"""ThreadManager.set_archived + PATCH /api/threads/{tid} is_archived 单测
（claude-session-rename-archive-metadata-source task#2）。

覆盖：

1. ``ThreadManager.set_archived(tid, True)`` 写盘后 ``read_thread_metadata``
   读出 ``is_archived=True`` + ``schema_version=11``。
2. ``set_archived(tid, False)`` 反向往返还原。
3. ``set_archived`` 对不存在 thread_id 抛 ``KeyError``。
4. ``set_archived`` 后已 boot cell 的 ``cell.metadata`` 同步更新。
5. ``PATCH /api/threads/{tid}`` body=``{"is_archived": true}`` → 200 + DTO 字段。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hosts.web.app import create_app
from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from hosts.web.threads.cell import ThreadCell
from hosts.web.threads.metadata import ThreadMetadata, read_thread_metadata, write_thread_metadata
from infrastructure.config.models import Config
from tests.unit.test_web_app_lifespan import _seed_password

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


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


async def _fake_factory(tid: str, pid: str, adapter: Any, sinks: list[Any]) -> tuple[Any, Any]:
    del tid, pid, adapter, sinks
    raise NotImplementedError


# ---------------------------------------------------------------------------
# ThreadManager 层（直接调，不走 HTTP）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_archived_true_roundtrip(tmp_path: Path) -> None:
    """set_archived(tid, True) 写盘后 read 拿到 is_archived=True + schema v11。"""
    from hosts.web.threads.manager import ThreadManager

    cfg = _make_cfg()
    tm = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=_fake_factory)
    meta = await tm.create_thread("t1", "preset-1")
    assert meta.is_archived is False

    updated = await tm.set_archived(meta.id, True)
    assert updated.is_archived is True
    assert updated.id == meta.id
    assert updated.name == meta.name
    assert updated.schema_version == 11

    # 重新读盘确认持久化
    on_disk = read_thread_metadata(tmp_path, meta.id)
    assert on_disk is not None
    assert on_disk.is_archived is True
    assert on_disk.schema_version == 11


@pytest.mark.asyncio
async def test_set_archived_false_reverse_roundtrip(tmp_path: Path) -> None:
    """set_archived(tid, False) 反向往返还原。"""
    from hosts.web.threads.manager import ThreadManager

    cfg = _make_cfg()
    tm = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=_fake_factory)
    meta = await tm.create_thread("t1", "preset-1")

    await tm.set_archived(meta.id, True)
    re_disk = read_thread_metadata(tmp_path, meta.id)
    assert re_disk is not None and re_disk.is_archived is True

    reverted = await tm.set_archived(meta.id, False)
    assert reverted.is_archived is False

    on_disk = read_thread_metadata(tmp_path, meta.id)
    assert on_disk is not None
    assert on_disk.is_archived is False


@pytest.mark.asyncio
async def test_set_archived_unknown_thread_raises_keyerror(tmp_path: Path) -> None:
    """set_archived 对不存在 thread_id 抛 KeyError。"""
    from hosts.web.threads.manager import ThreadManager

    cfg = _make_cfg()
    tm = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=_fake_factory)

    with pytest.raises(KeyError, match="thread not found"):
        await tm.set_archived("thread-deadbeef0000", True)


@pytest.mark.asyncio
async def test_set_archived_updates_booted_cell_metadata(tmp_path: Path) -> None:
    """set_archived 后若 cell 已 boot，cell.metadata 同步刷新。"""
    from hosts.web.threads.manager import ThreadManager

    cfg = _make_cfg()
    tm = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=_fake_factory)
    meta = await tm.create_thread("t1", "preset-1")

    # 手动构造一个 cell 注入到 tm._cells，避免触发 runtime_factory
    # 用 object() 占位无关字段；本测试只关心 metadata 同步
    fake_cell = ThreadCell(
        thread_id=meta.id,
        metadata=meta,
        runtime=object(),  # type: ignore[arg-type]
        bridge=object(),  # type: ignore[arg-type]
        adapter=object(),  # type: ignore[arg-type]
        event_sinks=[],
    )
    tm._cells[meta.id] = fake_cell  # type: ignore[attr-defined]

    assert fake_cell.metadata.is_archived is False
    updated = await tm.set_archived(meta.id, True)
    assert updated.is_archived is True

    # cell.metadata 应已被同步替换
    assert fake_cell.metadata.is_archived is True
    assert fake_cell.metadata is updated


@pytest.mark.asyncio
async def test_set_archived_preserves_other_fields(tmp_path: Path) -> None:
    """set_archived 不破坏 name / is_pinned / claude_thread_id 等其它字段。"""
    from hosts.web.threads.manager import ThreadManager

    cfg = _make_cfg()
    tm = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=_fake_factory)
    meta = await tm.create_thread("hello-name", "preset-1")

    # 同时设 is_pinned + claude_thread_id（直接写盘模拟历史状态）
    pre = ThreadMetadata(
        id=meta.id,
        name="hello-name",
        preset_id=meta.preset_id,
        backend_kind=meta.backend_kind,
        claude_thread_id="claude-uuid-1",
        codex_thread_id="",
        cwd="/tmp/proj",
        created_at=meta.created_at,
        updated_at=meta.updated_at,
        message_count=meta.message_count,
        is_pinned=True,
        is_archived=False,
    )
    write_thread_metadata(tmp_path, pre)

    updated = await tm.set_archived(meta.id, True)
    assert updated.is_archived is True
    assert updated.is_pinned is True  # 保留
    assert updated.name == "hello-name"
    assert updated.claude_thread_id == "claude-uuid-1"
    assert updated.cwd == "/tmp/proj"


# ---------------------------------------------------------------------------
# REST 层
# ---------------------------------------------------------------------------


def _login_client_with_real_tm(tmp_path: Path) -> tuple[TestClient, Any]:
    """装好真实 ThreadManager 的 app + login。"""
    from hosts.web.threads.manager import ThreadManager

    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    tm = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=_fake_factory)
    app = create_app(cfg, tm, home_dir=tmp_path)
    client = TestClient(app)
    client.__enter__()
    resp = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert resp.status_code == 200
    return client, tm


def test_patch_thread_is_archived_true_returns_dto(tmp_path: Path) -> None:
    """PATCH /api/threads/{tid} body={is_archived: true} → 200 + DTO is_archived=True。"""
    client, _tm = _login_client_with_real_tm(tmp_path)
    try:
        # 先建一个 thread
        resp = client.post(
            "/api/threads",
            json={"name": "t1", "preset_id": "p1"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 201
        thread_id = resp.json()["id"]
        assert resp.json()["is_archived"] is False

        # PATCH is_archived=True
        resp = client.patch(
            f"/api/threads/{thread_id}",
            json={"is_archived": True},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == thread_id
        assert body["is_archived"] is True
        assert body["schema_version"] == 11
    finally:
        client.__exit__(None, None, None)


def test_patch_thread_is_archived_false_reverse(tmp_path: Path) -> None:
    """PATCH is_archived=False 可以把归档状态切回去。"""
    client, _tm = _login_client_with_real_tm(tmp_path)
    try:
        resp = client.post(
            "/api/threads",
            json={"name": "t1", "preset_id": "p1"},
            headers=CSRF_HEADERS,
        )
        thread_id = resp.json()["id"]

        # 先归档
        resp = client.patch(
            f"/api/threads/{thread_id}",
            json={"is_archived": True},
            headers=CSRF_HEADERS,
        )
        assert resp.json()["is_archived"] is True

        # 再取消归档
        resp = client.patch(
            f"/api/threads/{thread_id}",
            json={"is_archived": False},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["is_archived"] is False
    finally:
        client.__exit__(None, None, None)


def test_patch_thread_is_archived_unknown_returns_404(tmp_path: Path) -> None:
    """PATCH 不存在的 thread → 404。"""
    client, _tm = _login_client_with_real_tm(tmp_path)
    try:
        resp = client.patch(
            "/api/threads/thread-deadbeef0000",
            json={"is_archived": True},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_patch_thread_combined_name_and_archive(tmp_path: Path) -> None:
    """同一 PATCH 同时改 name 和 is_archived，两个字段都应生效。"""
    client, _tm = _login_client_with_real_tm(tmp_path)
    try:
        resp = client.post(
            "/api/threads",
            json={"name": "old", "preset_id": "p1"},
            headers=CSRF_HEADERS,
        )
        thread_id = resp.json()["id"]

        resp = client.patch(
            f"/api/threads/{thread_id}",
            json={"name": "new", "is_archived": True},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "new"
        assert body["is_archived"] is True
    finally:
        client.__exit__(None, None, None)


def test_get_threads_includes_is_archived(tmp_path: Path) -> None:
    """GET /api/threads 返回项含 is_archived 字段。"""
    client, _tm = _login_client_with_real_tm(tmp_path)
    try:
        resp = client.post(
            "/api/threads",
            json={"name": "t1", "preset_id": "p1"},
            headers=CSRF_HEADERS,
        )
        thread_id = resp.json()["id"]
        client.patch(
            f"/api/threads/{thread_id}",
            json={"is_archived": True},
            headers=CSRF_HEADERS,
        )

        resp = client.get("/api/threads")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["is_archived"] is True
    finally:
        client.__exit__(None, None, None)
