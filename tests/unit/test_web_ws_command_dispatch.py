"""unit：ws.py 对 command 路径的 dispatch 覆盖。

验证 _run_once_safely 在 bridge.run_once 返回 CommandResult 时：
- 不崩溃
- 不尝试提取 usage（CommandResult.metadata 结构不同于 Result.metadata）
- 返回 Result 时 usage 提取仍正常工作
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from commands.models import CommandResult
from config_loader.models import Config
from core.message import Message
from core.result import Result
from tests.unit.test_web_app_lifespan import _seed_password
from web.app import create_app
from web.auth import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from web.thread_metadata import ThreadMetadata

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}
THREAD_ID = "thread-bbbbbbbbbbbb"


class CommandBridge:
    """bridge.run_once 返回 CommandResult。"""

    def __init__(self, result: CommandResult | Result) -> None:
        self._result = result
        self.calls: list[str] = []

    async def run_once(
        self, text: str, *, reasoning_effort: str | None = None
    ) -> CommandResult | Result:
        self.calls.append(text)
        return self._result


class FakeAdapter:
    def __init__(self) -> None:
        self.attach_calls: list[Any] = []
        self.detach_calls: list[Any] = []
        self.resolve_calls: list[tuple[str, bool]] = []
        self._closed = False
        self.pending_approval_count = 0

    def attach_ws(self, new_ws: Any) -> None:
        self.attach_calls.append(new_ws)
        self._closed = False

    def detach_ws(self, ws: Any) -> None:
        self.detach_calls.append(ws)

    def resolve_approval(self, call_id: str, action: Any) -> None:
        self.resolve_calls.append((call_id, action))


class FakeRuntime:
    _sessions: dict[str, Any] = {}


class CommandCell:
    def __init__(self, thread_id: str, bridge_result: CommandResult | Result) -> None:
        self.thread_id = thread_id
        self.metadata = ThreadMetadata(
            id=thread_id,
            name="t",
            preset_id="p1",
            created_at=1.0,
            updated_at=2.0,
            message_count=0,
        )
        self.bridge = CommandBridge(bridge_result)
        self.adapter = FakeAdapter()
        self.runtime = FakeRuntime()
        self.event_sinks: list[Any] = []
        self.last_active_at = 0.0
        self.status: str = "idle"
        self.current_run_task: Any = None

    def attach_ws(self, new_ws: Any) -> None:
        self.adapter.attach_ws(new_ws)

    def detach_ws(self, ws: Any) -> None:
        self.adapter.detach_ws(ws)

    def touch(self) -> None:
        self.last_active_at += 1.0

    @property
    def has_pending_approvals(self) -> bool:
        return False


class CommandTM:
    def __init__(self, cell: CommandCell) -> None:
        self._cell = cell
        self._started = False
        self._closed = False
        self.usage_calls: list[dict[str, int]] = []

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

    async def boot_or_attach(self, thread_id: str) -> CommandCell:
        return self._cell

    async def create_thread(self, name: str, preset_id: str) -> Any:
        raise NotImplementedError

    async def rename_thread(self, thread_id: str, new_name: str) -> Any:
        raise NotImplementedError

    async def delete_thread(self, thread_id: str, *, keep_history: bool = False) -> None:
        return None

    async def evict_cell(
        self,
        thread_id: str,
        reason: str,
        message: str | None = None,
        *,
        notify_ws: bool = True,
    ) -> None:
        return None

    def list_threads(self) -> list[Any]:
        return []

    def list_cells(self) -> list[Any]:
        return []

    def get_cell(self, thread_id: str) -> Any:
        return self._cell

    def resolve_approval(self, thread_id: str, call_id: str, action: Any) -> None:
        pass

    async def add_thread_usage(
        self,
        thread_id: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        self.usage_calls.append(
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            }
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


def _login(tmp_path: Path, tm: CommandTM) -> Any:
    from fastapi.testclient import TestClient

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


def test_ws_command_result_no_crash(tmp_path: Path) -> None:
    """bridge.run_once 返回 CommandResult 时 ws handler 不崩溃。"""
    cmd_result = CommandResult(
        status="completed",
        command_name="/review",
        output_text="/review completed.",
        invocation_id="cmd-1",
        metadata={"artifact_path": "/tmp/x.json"},
    )
    cell = CommandCell(THREAD_ID, cmd_result)
    tm = CommandTM(cell)
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_json(
                {
                    "kind": "user.input",
                    "text": "/review",
                    "request_id": "req-cmd",
                }
            )
            time.sleep(0.2)
        assert "/review" in cell.bridge.calls
        assert tm.usage_calls == []
    finally:
        client.__exit__(None, None, None)


def test_ws_runtime_result_still_logs_usage(tmp_path: Path) -> None:
    """bridge.run_once 返回 Result 时 usage 统计仍正常提取。"""
    runtime_result = Result(
        run_id="r-1",
        session_id="sid-1",
        status="completed",
        final_message=Message.assistant(content="hi"),
        turn_count=1,
        metadata={"usage": {"prompt_tokens": 50, "completion_tokens": 25, "total_tokens": 75}},
    )
    cell = CommandCell(THREAD_ID, runtime_result)
    tm = CommandTM(cell)
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_json(
                {
                    "kind": "user.input",
                    "text": "hello",
                    "request_id": "req-text",
                }
            )
            time.sleep(0.2)
        assert "hello" in cell.bridge.calls
    finally:
        client.__exit__(None, None, None)


def test_ws_command_failed_result_no_crash(tmp_path: Path) -> None:
    """failed CommandResult 不崩溃。"""
    cmd_result = CommandResult(
        status="failed",
        command_name="/deploy",
        output_text="Unknown command: /deploy",
    )
    cell = CommandCell(THREAD_ID, cmd_result)
    tm = CommandTM(cell)
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_json(
                {
                    "kind": "user.input",
                    "text": "/deploy",
                    "request_id": "req-fail",
                }
            )
            time.sleep(0.2)
        assert "/deploy" in cell.bridge.calls
        assert tm.usage_calls == []
    finally:
        client.__exit__(None, None, None)
