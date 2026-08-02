"""Thread permissions REST 路由合同测试。

通过真实 FastAPI TestClient 和 PermissionsManager 覆盖 GET 空快照、PUT revision
CAS、409 冲突、path/body thread 身份隔离与未知字段拒绝。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from safety.approval._thread_permissions_store import ThreadPermissionsStore
from safety.approval.permissions_errors import PermissionsStoreError
from tests.unit.test_web_routers_threads import CSRF_HEADERS, FakeTM, _login_client


def _create_thread(client: TestClient) -> str:
    """通过真实 POST 路由创建测试 thread 并返回 id。"""
    response = client.post(
        "/api/threads",
        json={"name": "permissions", "preset_id": "p1"},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 201
    return str(response.json()["id"])


def test_get_put_and_stale_revision_conflict(tmp_path: Path) -> None:
    """GET/PUT 使用真实本子门户，stale revision 保持已提交内容。"""
    client = _login_client(tmp_path, FakeTM())
    try:
        thread_id = _create_thread(client)
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
        assert unchanged.json()["deny"] == [{"expression": "run_shell(curl:*)", "scope_cwd": None}]
    finally:
        client.__exit__(None, None, None)


def test_rejects_cross_thread_body_and_unknown_field(tmp_path: Path) -> None:
    """路由阻止 thread A 写 thread B，并严格拒绝旧 scope 字段。"""
    client = _login_client(tmp_path, FakeTM())
    try:
        thread_id = _create_thread(client)
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


def test_invalid_permission_expression_returns_stable_422(tmp_path: Path) -> None:
    """非法或未 canonicalize 的 DSL 返回稳定错误码，且不会物化本子。"""
    client = _login_client(tmp_path, FakeTM())
    try:
        thread_id = _create_thread(client)
        response = client.put(
            f"/api/threads/{thread_id}/permissions",
            json={
                "thread_id": thread_id,
                "revision": 0,
                "allow": [{"expression": " run_shell(git:*)", "scope_cwd": "/workspace"}],
                "deny": [],
            },
            headers=CSRF_HEADERS,
        )

        assert response.status_code == 422
        assert response.json()["detail"] == {
            "code": "invalid_permission_expression",
            "thread_id": thread_id,
            "message": "invalid permission expression: ' run_shell(git:*)'",
        }
        unchanged = client.get(f"/api/threads/{thread_id}/permissions")
        assert unchanged.status_code == 200
        assert unchanged.json()["revision"] == 0
        assert unchanged.json()["allow"] == []
    finally:
        client.__exit__(None, None, None)


def test_rejects_shell_allow_without_cwd_and_relative_scope(tmp_path: Path) -> None:
    """schema v2 对 Shell allow 缺失或非法 cwd 返回稳定 422。"""
    client = _login_client(tmp_path, FakeTM())
    try:
        thread_id = _create_thread(client)
        for scope_cwd in (None, "relative/path"):
            response = client.put(
                f"/api/threads/{thread_id}/permissions",
                json={
                    "thread_id": thread_id,
                    "revision": 0,
                    "allow": [
                        {
                            "expression": "run_shell(git status:*)",
                            "scope_cwd": scope_cwd,
                        }
                    ],
                    "deny": [],
                },
                headers=CSRF_HEADERS,
            )
            assert response.status_code == 422
            assert response.json()["detail"]["code"] == "invalid_permission_expression"
    finally:
        client.__exit__(None, None, None)


def test_get_migrates_v1_and_returns_one_time_summary(tmp_path: Path) -> None:
    """真实 GET 首次迁移 v1，返回失效计数并保留原文备份。"""
    client = _login_client(tmp_path, FakeTM())
    try:
        thread_id = _create_thread(client)
        store = ThreadPermissionsStore(tmp_path / "safety" / "thread_permissions")
        path = store.path_for(thread_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "thread_id": thread_id,
                    "revision": 3,
                    "allow": ["read_file", "run_shell(git:*)"],
                    "deny": ["run_shell(curl:*)"],
                    "updated_at": None,
                }
            ),
            encoding="utf-8",
        )

        migrated = client.get(f"/api/threads/{thread_id}/permissions")
        repeated = client.get(f"/api/threads/{thread_id}/permissions")

        assert migrated.status_code == 200
        assert migrated.json()["revision"] == 4
        assert migrated.json()["allow"] == [{"expression": "read_file", "scope_cwd": None}]
        assert migrated.json()["deny"] == [{"expression": "run_shell(curl:*)", "scope_cwd": None}]
        assert migrated.json()["migration_summary"] == {
            "from_schema_version": 1,
            "to_schema_version": 2,
            "invalidated_shell_allow_count": 1,
            "backup_path": store.backup_path_for(thread_id).as_posix(),
        }
        assert repeated.status_code == 200
        assert repeated.json()["migration_summary"] is None
        assert store.backup_path_for(thread_id).exists()
    finally:
        client.__exit__(None, None, None)


def test_get_reports_migration_storage_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1 原子迁移失败时 GET 返回稳定 503，原文件保持 v1。"""
    client = _login_client(tmp_path, FakeTM())
    try:
        thread_id = _create_thread(client)
        store = ThreadPermissionsStore(tmp_path / "safety" / "thread_permissions")
        path = store.path_for(thread_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        original = {
            "schema_version": 1,
            "thread_id": thread_id,
            "revision": 0,
            "allow": ["run_shell(git:*)"],
            "deny": [],
            "updated_at": None,
        }
        path.write_text(json.dumps(original), encoding="utf-8")

        def _fail_atomic_write(*_args: object, **_kwargs: object) -> None:
            raise PermissionsStoreError("injected migration write failure")

        monkeypatch.setattr(
            "safety.approval._thread_permissions_store._atomic_write_json",
            _fail_atomic_write,
        )
        response = client.get(f"/api/threads/{thread_id}/permissions")

        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "permissions_storage_unavailable"
        assert json.loads(path.read_text(encoding="utf-8")) == original
    finally:
        client.__exit__(None, None, None)
