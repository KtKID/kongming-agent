from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.unit.test_web_app_lifespan import _seed_password
from web.app import create_app
from web.auth import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from web.thread_metadata import ThreadMetadata

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


class FakeTM:
    def __init__(
        self,
        workspace_root: Path,
        *,
        backend_kind: str = "claude_code",
        sdk_session_id: str = "sdk-1",
    ) -> None:
        self._started = False
        self._closed = False
        self.bind_calls: list[tuple[str, str, str]] = []
        self._meta = ThreadMetadata(
            id="thread-000000000004",
            name="Claude",
            preset_id="",
            backend_kind=backend_kind,
            sdk_session_id=sdk_session_id,
            cwd=str(workspace_root),
            created_at=1.0,
            updated_at=2.0,
            message_count=0,
        )

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

    def list_threads(self) -> list[ThreadMetadata]:
        return [self._meta]

    async def create_thread(self, *args, **kwargs) -> Any:
        raise NotImplementedError

    async def rename_thread(self, *args, **kwargs) -> Any:
        raise NotImplementedError

    async def delete_thread(self, *args, **kwargs) -> None:
        return None

    async def boot_or_attach(self, *args, **kwargs) -> Any:
        raise NotImplementedError

    async def evict_cell(self, *args, **kwargs) -> None:
        return None

    def list_cells(self) -> list[Any]:
        return []

    def get_cell(self, thread_id: str) -> Any:
        del thread_id
        return None

    def find_thread_by_sdk_session_id(self, sdk_session_id: str) -> ThreadMetadata | None:
        del sdk_session_id
        return None

    async def bind_sdk_session(
        self, thread_id: str, sdk_session_id: str, cwd: str
    ) -> ThreadMetadata:
        self.bind_calls.append((thread_id, sdk_session_id, cwd))
        self._meta = self._meta.model_copy(
            update={
                "sdk_session_id": sdk_session_id,
                "cwd": cwd,
            }
        )
        return self._meta

    async def add_thread_usage(self, *args, **kwargs) -> ThreadMetadata:
        raise NotImplementedError

    def resolve_approval(self, *args, **kwargs) -> None:
        return None


class FakeShellProcess:
    instances: list[FakeShellProcess] = []

    def __init__(self, *, command, cwd, emit_output, emit_status, create_subprocess_exec=None):
        del create_subprocess_exec
        self.command = command
        self.cwd = cwd
        self.emit_output = emit_output
        self.emit_status = emit_status
        self.writes: list[str] = []
        self.resizes: list[tuple[int, int]] = []
        self.terminated = False
        FakeShellProcess.instances.append(self)

    async def start(self, *, cols=120, rows=32) -> None:
        self.resizes.append((cols, rows))
        await self.emit_status(
            {
                "type": "shell-status",
                "status": "running",
                "cwd": str(self.cwd),
                "command": self.command,
            }
        )

    async def write(self, data: str) -> None:
        self.writes.append(data)
        await self.emit_output(data)

    def resize(self, *, cols: int, rows: int) -> None:
        self.resizes.append((cols, rows))

    async def terminate(self) -> None:
        self.terminated = True


class FakeResumeFailThenPlainShellProcess(FakeShellProcess):
    async def start(self, *, cols=120, rows=32) -> None:
        self.resizes.append((cols, rows))
        if self.command and self.command[0] == "claude":
            raise RuntimeError("claude resume failed")
        await self.emit_status(
            {
                "type": "shell-status",
                "status": "running",
                "cwd": str(self.cwd),
                "command": self.command,
            }
        )


def _make_cfg():
    from tests.unit.web.test_workspace_context_endpoint import _make_cfg

    return _make_cfg()


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


def test_workspace_shell_ws_routes_input_resize_and_terminate(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    monkeypatch.setattr("web.routers.workspace_shell.WorkspaceShellProcess", FakeShellProcess)
    client = _login_client(tmp_path, FakeTM(workspace))
    try:
        with client.websocket_connect("/ws/workspace-shell?thread_id=thread-000000000004") as ws:
            starting = ws.receive_json()
            assert starting["type"] == "shell-status"
            assert starting["status"] == "starting"
            running = ws.receive_json()
            assert running["status"] == "running"

            ws.send_json({"type": "shell-input", "data": "hello\n"})
            echoed = ws.receive_json()
            assert echoed == {"type": "shell-output", "data": "hello\n"}

            ws.send_json({"type": "shell-resize", "cols": 90, "rows": 20})
            ws.send_json({"type": "shell-terminate"})
            terminated = ws.receive_json()
            assert terminated["status"] == "terminated"

        fake = FakeShellProcess.instances[-1]
        assert fake.command == ["claude", "--resume", "sdk-1"]
        assert fake.writes == ["hello\n"]
        assert (90, 20) in fake.resizes
        assert fake.terminated is True
    finally:
        client.__exit__(None, None, None)


def test_workspace_shell_ws_falls_back_to_plain_shell_on_resume_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr(
        "web.routers.workspace_shell.WorkspaceShellProcess",
        FakeResumeFailThenPlainShellProcess,
    )
    client = _login_client(tmp_path, FakeTM(workspace))
    try:
        with client.websocket_connect("/ws/workspace-shell?thread_id=thread-000000000004") as ws:
            starting = ws.receive_json()
            assert starting["type"] == "shell-status"
            assert starting["status"] == "starting"
            assert starting["command"] == ["claude", "--resume", "sdk-1"]

            error = ws.receive_json()
            assert error["type"] == "shell-error"
            assert "claude resume failed" in error["detail"]
            fallback_starting = ws.receive_json()
            assert fallback_starting == {
                "type": "shell-status",
                "status": "starting",
                "cwd": str(workspace),
                "command": ["/bin/zsh", "-l"],
            }
            fallback_running = ws.receive_json()
            assert fallback_running["status"] == "running"
            assert fallback_running["command"] == ["/bin/zsh", "-l"]

        first, second = FakeShellProcess.instances[-2:]
        assert first.command == ["claude", "--resume", "sdk-1"]
        assert second.command == ["/bin/zsh", "-l"]
        assert first.terminated is True
        assert second.terminated is True
    finally:
        client.__exit__(None, None, None)


def test_workspace_shell_ws_uses_system_shell_for_generic_chat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr("web.routers.workspace_shell.WorkspaceShellProcess", FakeShellProcess)
    client = _login_client(
        tmp_path,
        FakeTM(workspace, backend_kind="generic_chat", sdk_session_id=""),
    )
    try:
        with client.websocket_connect("/ws/workspace-shell?thread_id=thread-000000000004") as ws:
            starting = ws.receive_json()
            assert starting["command"] == ["/bin/zsh", "-l"]
            running = ws.receive_json()
            assert running["command"] == ["/bin/zsh", "-l"]
    finally:
        client.__exit__(None, None, None)


def test_workspace_shell_ws_binds_new_claude_session_for_unbound_thread(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    monkeypatch.setattr("web.routers.workspace_shell.WorkspaceShellProcess", FakeShellProcess)

    async def _fake_wait_for_new_claude_session(*args, **kwargs) -> str:
        del args, kwargs
        return "sid-new"

    monkeypatch.setattr(
        "web.routers.workspace_shell.wait_for_new_claude_session",
        _fake_wait_for_new_claude_session,
    )
    tm = FakeTM(workspace, backend_kind="claude_code", sdk_session_id="")
    client = _login_client(tmp_path, tm)
    try:
        with client.websocket_connect("/ws/workspace-shell?thread_id=thread-000000000004") as ws:
            starting = ws.receive_json()
            assert starting["command"] == ["claude"]
            running = ws.receive_json()
            assert running["status"] == "running"
        assert tm.bind_calls == [("thread-000000000004", "sid-new", str(workspace))]
    finally:
        client.__exit__(None, None, None)
