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
from unittest.mock import AsyncMock, MagicMock

import pytest

from commands.models import CommandResult
from core.message import Message
from core.result import Result
from hosts.web.app import create_app
from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from hosts.web.threads.metadata import ThreadMetadata
from hosts.web.websocket.thread_status import (
    get_thread_status_manager,
    reset_broadcaster_for_testing,
)
from infrastructure.config.models import Config
from tests.unit.test_web_app_lifespan import _seed_password

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}
THREAD_ID = "thread-bbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _reset_thread_status_broadcaster() -> None:
    """隔离 thread-status broadcaster，避免命令分发测试之间共享连接或 mock。"""
    reset_broadcaster_for_testing()
    yield
    reset_broadcaster_for_testing()


class CommandBridge:
    """记录 ephemeral cell 的 HostDispatcher.submit 调用并返回预制结果。"""

    def __init__(self, result: CommandResult | Result) -> None:
        self._result = result
        self.calls: list[str] = []

    async def run_once(
        self,
        text: str,
        *,
        reasoning_effort: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
    ) -> CommandResult | Result:
        self.calls.append(text)
        return self._result

    async def submit(
        self,
        text: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
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
    def __init__(self) -> None:
        self._sessions: dict[str, Any] = {}
        self.session = object()

    def get_or_create_session(self, _session_id: str) -> object:
        return self.session


class FakeEvolutionManager:
    """记录 `/evolve` 控制命令是否直达 child reviewer 入口。"""

    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any, str]] = []

    def register_event_route(self, _thread_id: str, _sink: Any) -> None:
        return None

    def unregister_event_route(self, _thread_id: str, _sink: Any = None) -> None:
        return None

    async def start_manual_command_review(
        self,
        *,
        parent_runtime: Any,
        session: Any,
        thread_id: str,
    ) -> str:
        self.calls.append((parent_runtime, session, thread_id))
        return f"run-manual-command-{thread_id}-1"


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
        self.host_dispatcher = self.bridge
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

    def get_client_event_sink(self) -> None:
        return None

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
            "model": {"preset_id": "local-gemma-4-e4b-it"},
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
                    "frame_type": "user.input",
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
                    "frame_type": "user.input",
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
                    "frame_type": "user.input",
                    "text": "/deploy",
                    "request_id": "req-fail",
                }
            )
            time.sleep(0.2)
        assert "/deploy" in cell.bridge.calls
        assert tm.usage_calls == []
    finally:
        client.__exit__(None, None, None)


def test_ws_evolve_command_starts_child_reviewer_without_main_llm(tmp_path: Path) -> None:
    """`/evolve` 被控制面消费，当前 thread bridge 不收到命令文本。"""
    runtime_result = Result(
        run_id="unused",
        session_id=THREAD_ID,
        status="completed",
        final_message=Message.assistant(content="unused"),
        turn_count=1,
    )
    cell = CommandCell(THREAD_ID, runtime_result)
    tm = CommandTM(cell)
    client = _login(tmp_path, tm)
    manager = FakeEvolutionManager()
    client.app.state.evolution_manager = manager
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_json(
                {
                    "frame_type": "user.input",
                    "text": "/evolve",
                    "request_id": "req-evolve",
                }
            )
            time.sleep(0.2)

        assert manager.calls == [(cell.runtime, cell.runtime.session, THREAD_ID)]
        assert cell.bridge.calls == []
        assert tm.usage_calls == []
    finally:
        client.__exit__(None, None, None)


def test_ws_evolve_command_restores_idle_thread_status_after_control_dispatch(
    tmp_path: Path,
) -> None:
    """`/evolve` 由控制面消费后发布独立 control run 终态。"""
    runtime_result = Result(
        run_id="unused",
        session_id=THREAD_ID,
        status="completed",
        final_message=Message.assistant(content="unused"),
        turn_count=1,
    )
    cell = CommandCell(THREAD_ID, runtime_result)
    tm = CommandTM(cell)
    client = _login(tmp_path, tm)
    manager = FakeEvolutionManager()
    client.app.state.evolution_manager = manager
    try:
        with (
            client.websocket_connect("/ws/thread-status") as status_ws,
            client.websocket_connect(f"/ws/threads/{THREAD_ID}") as thread_ws,
        ):
            status_snapshot = status_ws.receive_json()
            assert status_snapshot["frame_type"] == "thread-status.snapshot"
            inbox_snapshot = status_ws.receive_json()
            assert inbox_snapshot["frame_type"] == "approval.inbox.snapshot"
            _ = thread_ws.receive_json()  # history
            thread_ws.send_json(
                {
                    "frame_type": "user.input",
                    "text": "/evolve",
                    "request_id": "req-evolve-status",
                }
            )
            terminal = status_ws.receive_json()

        assert terminal["frame_type"] == "thread-status"
        assert terminal["threadId"] == THREAD_ID
        assert terminal["phase"] == "idle"
        assert terminal["runId"].startswith(f"control-{THREAD_ID}-")
        assert terminal["runGeneration"] == 1
        assert terminal["sequence"] == 1
    finally:
        client.__exit__(None, None, None)


def test_ws_evolve_command_preserves_active_main_run_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主 run 活跃时，`/evolve` 控制命令不得广播 idle 覆盖真实运行态。"""
    runtime_result = Result(
        run_id="unused",
        session_id=THREAD_ID,
        status="completed",
        final_message=Message.assistant(content="unused"),
        turn_count=1,
    )
    cell = CommandCell(THREAD_ID, runtime_result)
    active_run = MagicMock()
    active_run.done.return_value = False
    cell.current_run_task = active_run
    tm = CommandTM(cell)
    client = _login(tmp_path, tm)
    manager = FakeEvolutionManager()
    client.app.state.evolution_manager = manager
    publish_status = AsyncMock()
    monkeypatch.setattr(
        get_thread_status_manager(),
        "publish_status",
        publish_status,
    )
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_json(
                {
                    "frame_type": "user.input",
                    "text": "/evolve",
                    "request_id": "req-evolve-active-run",
                }
            )
            time.sleep(0.2)

        assert manager.calls == [(cell.runtime, cell.runtime.session, THREAD_ID)]
        publish_status.assert_not_awaited()
    finally:
        client.__exit__(None, None, None)


def test_ws_evolve_command_rejects_arguments_without_main_llm(tmp_path: Path) -> None:
    """`/evolve` 不接收 prompt 参数，错误分支同样不进入当前 thread LLM。"""
    runtime_result = Result(
        run_id="unused",
        session_id=THREAD_ID,
        status="completed",
        final_message=Message.assistant(content="unused"),
        turn_count=1,
    )
    cell = CommandCell(THREAD_ID, runtime_result)
    tm = CommandTM(cell)
    client = _login(tmp_path, tm)
    manager = FakeEvolutionManager()
    client.app.state.evolution_manager = manager
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_json(
                {
                    "frame_type": "user.input",
                    "text": "/evolve 请关注缓存",
                    "request_id": "req-evolve-args",
                }
            )
            error = ws.receive_json()

        assert error["frame_type"] == "error"
        assert "/evolve 不接受参数" in error["message"]
        assert manager.calls == []
        assert cell.bridge.calls == []
    finally:
        client.__exit__(None, None, None)
