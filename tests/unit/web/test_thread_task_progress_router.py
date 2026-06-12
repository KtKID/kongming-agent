"""Thread task progress REST router 单元测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi.testclient import TestClient

from hosts.web.app import create_app
from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from hosts.web.threads.metadata import ThreadMetadata
from infrastructure.config.models import Config
from sessions import TASK_PROGRESS_MAX_ITEMS, SessionTaskProgressManager
from tests.unit.test_web_app_lifespan import _seed_password

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


class FakeTM:
    """满足 create_app 和 task-progress router 的最小 ThreadManager。"""

    def __init__(self, threads: list[ThreadMetadata] | None = None) -> None:
        self._threads = {item.id: item for item in (threads or [])}
        self.usage_manager = object()

    async def start(self) -> None:
        return None

    async def aclose_all(self) -> None:
        return None

    async def create_thread(
        self,
        name: str,
        preset_id: str = "",
        *,
        backend_kind: Literal["generic_chat", "claude_code", "codex"] = "generic_chat",
        cwd: str = "",
    ) -> ThreadMetadata:
        del name, preset_id, backend_kind, cwd
        raise NotImplementedError

    async def rename_thread(self, thread_id: str, new_name: str) -> ThreadMetadata:
        del thread_id, new_name
        raise NotImplementedError

    async def pin_thread(self, thread_id: str, is_pinned: bool) -> ThreadMetadata:
        del thread_id, is_pinned
        raise NotImplementedError

    async def delete_thread(self, thread_id: str, *, keep_history: bool = False) -> None:
        del thread_id, keep_history
        return None

    async def boot_or_attach(self, thread_id: str) -> Any:
        del thread_id
        return None

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
        del thread_id
        return None

    def find_thread_by_claude_thread_id(self, claude_thread_id: str) -> Any:
        del claude_thread_id
        return None

    def find_thread_by_codex_thread_id(self, codex_thread_id: str) -> Any:
        del codex_thread_id
        return None

    async def bind_claude_thread(
        self,
        thread_id: str,
        claude_thread_id: str,
        cwd: str,
    ) -> ThreadMetadata:
        del claude_thread_id, cwd
        return self._threads[thread_id]

    async def create_and_bind_claude_thread(
        self,
        *,
        claude_thread_id: str,
        cwd: str,
        name: str,
        preset_id: str = "",
    ) -> tuple[ThreadMetadata, bool]:
        del claude_thread_id, cwd, name, preset_id
        raise NotImplementedError

    async def bind_codex_thread(
        self,
        thread_id: str,
        codex_thread_id: str,
        cwd: str,
    ) -> ThreadMetadata:
        del codex_thread_id, cwd
        return self._threads[thread_id]

    def resolve_approval(self, thread_id: str, call_id: str, approved: bool) -> None:
        del thread_id, call_id, approved
        return None


def _meta(thread_id: str) -> ThreadMetadata:
    return ThreadMetadata(
        id=thread_id,
        name="t",
        preset_id="p",
        backend_kind="generic_chat",
        cwd="",
        created_at=1.0,
        updated_at=2.0,
        message_count=0,
    )


def _cfg(session_root: Path) -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "session": {
                "backend": "file",
                "file_store_path": str(session_root),
            },
            "web": {
                "enabled": True,
                "dev_mode": True,
            },
        }
    )


def _login_client(tmp_path: Path, tm: FakeTM) -> TestClient:
    _seed_password(tmp_path, "pwd")
    cfg = _cfg(tmp_path / "sessions")
    app = create_app(
        cfg,
        tm,
        home_dir=tmp_path,
        task_progress_manager=SessionTaskProgressManager.from_config(cfg),
    )
    client = TestClient(app)
    client.__enter__()
    resp = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert resp.status_code == 200
    return client


def _payload(status: str = "in_progress") -> dict[str, object]:
    return {
        "tasks": [
            {
                "id": "manual:run-1",
                "orchestration_task_id": "manual:run-1",
                "task_id": "task-1",
                "task_run_id": "run-1",
                "desc": "实现 REST API",
                "status": status,
                "display_order": 0,
            }
        ]
    }


def test_get_missing_file_returns_empty_snapshot(tmp_path: Path) -> None:
    thread_id = "thread-abc123abc123"
    client = _login_client(tmp_path, FakeTM([_meta(thread_id)]))
    try:
        resp = client.get(f"/api/threads/{thread_id}/task-progress")
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == thread_id
        assert body["source"] == "api"
        assert body["tasks"] == []
        assert body["counts"] == {
            "pending": 0,
            "in_progress": 0,
            "completed": 0,
            "total": 0,
        }
    finally:
        client.__exit__(None, None, None)


def test_put_writes_api_source_and_path_thread_id(tmp_path: Path) -> None:
    thread_id = "thread-abc123abc123"
    client = _login_client(tmp_path, FakeTM([_meta(thread_id)]))
    try:
        resp = client.put(
            f"/api/threads/{thread_id}/task-progress",
            json=_payload(),
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == thread_id
        assert body["source"] == "api"
        assert body["counts"]["in_progress"] == 1
        assert (tmp_path / "sessions" / thread_id / "task_progress.json").is_file()
    finally:
        client.__exit__(None, None, None)


def test_put_rejects_invalid_status(tmp_path: Path) -> None:
    thread_id = "thread-abc123abc123"
    client = _login_client(tmp_path, FakeTM([_meta(thread_id)]))
    try:
        resp = client.put(
            f"/api/threads/{thread_id}/task-progress",
            json=_payload(status="failed"),
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_put_rejects_domain_validation_error_as_422(tmp_path: Path) -> None:
    thread_id = "thread-abc123abc123"
    client = _login_client(tmp_path, FakeTM([_meta(thread_id)]))
    body = _payload()
    body["tasks"][0]["desc"] = ""
    try:
        resp = client.put(
            f"/api/threads/{thread_id}/task-progress",
            json=body,
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422
        assert "invalid task progress payload" in resp.json()["message"]
    finally:
        client.__exit__(None, None, None)


def test_put_rejects_body_source_override(tmp_path: Path) -> None:
    thread_id = "thread-abc123abc123"
    client = _login_client(tmp_path, FakeTM([_meta(thread_id)]))
    body = _payload()
    body["source"] = "llm"
    try:
        resp = client.put(
            f"/api/threads/{thread_id}/task-progress",
            json=body,
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_put_rejects_too_many_tasks(tmp_path: Path) -> None:
    thread_id = "thread-abc123abc123"
    client = _login_client(tmp_path, FakeTM([_meta(thread_id)]))
    tasks = []
    for index in range(TASK_PROGRESS_MAX_ITEMS + 1):
        tasks.append(
            {
                "id": f"manual:run-{index}",
                "orchestration_task_id": f"manual:run-{index}",
                "task_id": f"task-{index}",
                "task_run_id": f"run-{index}",
                "desc": "x",
                "status": "pending",
                "display_order": index,
            }
        )
    try:
        resp = client.put(
            f"/api/threads/{thread_id}/task-progress",
            json={"tasks": tasks},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_get_rejects_corrupted_progress_json_as_422(tmp_path: Path) -> None:
    thread_id = "thread-abc123abc123"
    progress_path = tmp_path / "sessions" / thread_id / "task_progress.json"
    progress_path.parent.mkdir(parents=True)
    progress_path.write_text("{bad json", encoding="utf-8")
    client = _login_client(tmp_path, FakeTM([_meta(thread_id)]))
    try:
        resp = client.get(f"/api/threads/{thread_id}/task-progress")
        assert resp.status_code == 422
        assert "invalid task progress json" in resp.json()["message"]
    finally:
        client.__exit__(None, None, None)


def test_requires_existing_thread(tmp_path: Path) -> None:
    client = _login_client(tmp_path, FakeTM([]))
    try:
        resp = client.get("/api/threads/thread-abc123abc123/task-progress")
        assert resp.status_code == 404
    finally:
        client.__exit__(None, None, None)
