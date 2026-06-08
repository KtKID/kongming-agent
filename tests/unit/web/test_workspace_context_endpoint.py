from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from config_loader.models import Config
from tests.unit.test_web_app_lifespan import _seed_password
from web.app import create_app
from web.auth import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from web.threads.metadata import ThreadMetadata

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


class FakeTM:
    def __init__(self, threads: list[ThreadMetadata]) -> None:
        self._threads = {thread.id: thread for thread in threads}
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
    ) -> ThreadMetadata:
        raise NotImplementedError

    async def rename_thread(self, thread_id: str, new_name: str) -> ThreadMetadata:
        raise NotImplementedError

    async def delete_thread(self, thread_id: str, *, keep_history: bool = False) -> None:
        del thread_id, keep_history

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

    def list_threads(self) -> list[ThreadMetadata]:
        return list(self._threads.values())

    def list_cells(self) -> list[Any]:
        return []

    def get_cell(self, thread_id: str) -> Any:
        del thread_id
        return None

    def find_thread_by_claude_thread_id(self, claude_thread_id: str) -> ThreadMetadata | None:
        del claude_thread_id
        return None

    async def bind_claude_thread(
        self,
        thread_id: str,
        claude_thread_id: str,
        cwd: str,
    ) -> ThreadMetadata:
        del thread_id, claude_thread_id, cwd
        raise NotImplementedError

    # task#3.3：``add_thread_usage`` 已删除；mock 改提供 ``usage_manager`` property。
    # 本测试不消费 usage_manager，简单 ``None`` 即可（类型注解 Any）。
    usage_manager = None  # type: ignore[assignment]

    def resolve_approval(self, thread_id: str, call_id: str, approved: bool) -> None:
        del thread_id, call_id, approved


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
    app = create_app(_make_cfg(), tm, home_dir=tmp_path)
    client = TestClient(app)
    client.__enter__()
    resp = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert resp.status_code == 200
    return client


def test_workspace_context_for_claude_code_thread(tmp_path: Path) -> None:
    tm = FakeTM(
        [
            ThreadMetadata(
                id="thread-000000000001",
                name="Claude",
                preset_id="",
                backend_kind="claude_code",
                claude_thread_id="sdk-1",
                cwd="/tmp/project-a",
                created_at=1.0,
                updated_at=2.0,
                message_count=0,
            )
        ]
    )
    client = _login_client(tmp_path, tm)
    try:
        resp = client.get(
            "/api/threads/thread-000000000001/workspace-context",
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["workspace_root"] == "/tmp/project-a"
        assert body["shell_provider"] == "claude_code"
        assert body["files_available"] is True
        assert body["shell_available"] is True
    finally:
        client.__exit__(None, None, None)


def test_workspace_context_empty_cwd_falls_back_to_server_cwd(tmp_path: Path) -> None:
    """thread.cwd 空时 fallback 到 ``app.state.workspace_root`` (server 启动目录)。

    task #2：纯聊天 thread 不再返回 ``workspace_root=""``，统一走 server cwd，
    让 files/shell 直接可用，与 ``resolve_workspace_cwd`` helper 行为对齐。
    """
    tm = FakeTM(
        [
            ThreadMetadata(
                id="thread-000000000002",
                name="Generic",
                preset_id="preset-a",
                backend_kind="generic_chat",
                claude_thread_id="",
                cwd="",
                created_at=1.0,
                updated_at=2.0,
                message_count=0,
            )
        ]
    )
    client = _login_client(tmp_path, tm)
    try:
        # 显式覆盖 app.state.workspace_root，固定 fallback 目标
        client.app.state.workspace_root = tmp_path  # type: ignore[attr-defined]
        resp = client.get(
            "/api/threads/thread-000000000002/workspace-context",
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["workspace_root"] == str(tmp_path)
        assert body["files_available"] is True
        assert body["shell_available"] is True
        # generic_chat thread 走 system_shell（非 claude_code）
        assert body["shell_provider"] == "system_shell"
        assert body["unavailable_reason"] is None
    finally:
        client.__exit__(None, None, None)


def test_workspace_context_for_generic_thread_with_cwd_uses_system_shell(tmp_path: Path) -> None:
    tm = FakeTM(
        [
            ThreadMetadata(
                id="thread-000000000003",
                name="Generic",
                preset_id="preset-a",
                backend_kind="generic_chat",
                claude_thread_id="",
                cwd="/tmp/project-b",
                created_at=1.0,
                updated_at=2.0,
                message_count=0,
            )
        ]
    )
    client = _login_client(tmp_path, tm)
    try:
        resp = client.get(
            "/api/threads/thread-000000000003/workspace-context",
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["workspace_root"] == "/tmp/project-b"
        assert body["shell_provider"] == "system_shell"
        assert body["files_available"] is True
        assert body["shell_available"] is True
        assert body["unavailable_reason"] is None
    finally:
        client.__exit__(None, None, None)
