"""Codex WebSocket route 主路径单测（v0.1 T1 最小覆盖）。

覆盖 4 类入站消息分发主路径：

1. ``codex-command``        → :meth:`CodexService.query` 装配参数验证
2. ``abort-session``        → :meth:`CodexService.abort` + ``complete(aborted=True)``
3. ``check-session-status`` → ``session-status`` 帧（active / inactive 两路）
4. unknown frame_type       → ``frame_type:error`` + 主循环不退出

完整边界（resume 缺 sessionId / 字段类型 / WebSocketDisconnect 安全退出）见 T4。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from config_loader.models import Config
from tests.unit.test_web_app_lifespan import FakeThreadManager, _seed_password
from web.app import create_app
from web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from web.shared.session_manager import SessionManager

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


def _make_cfg() -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {"enabled": True, "dev_mode": True},
        },
    )


@pytest.fixture
def app_client(tmp_path: Path) -> Iterator[tuple[TestClient, AsyncMock, SessionManager]]:
    """已登录 TestClient + AsyncMock 替换的 codex_service + 共享 SessionManager。

    返回 ``(client, fake_service, session_manager)``，让用例自定义 mock 行为
    （例如让 ``is_active`` 返回 True 模拟 active session）。
    """
    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    tm = FakeThreadManager()
    app = create_app(cfg, tm, home_dir=tmp_path)

    # 替换 codex_service 为 AsyncMock：query/abort 都是 awaitable
    fake_service = AsyncMock()
    fake_service.query = AsyncMock(return_value=None)
    fake_service.abort = AsyncMock(return_value=True)
    app.state.codex_service = fake_service

    sessions: SessionManager = app.state.codex_session_manager

    client = TestClient(app)
    client.__enter__()
    r = client.post("/api/auth/login", json={"password": "pwd"}, headers=CSRF_HEADERS)
    assert r.status_code == 200
    try:
        yield client, fake_service, sessions
    finally:
        client.__exit__(None, None, None)


class TestCodexCommand:
    """``codex-command`` 帧 → service.query 参数装配。"""

    def test_dispatches_to_service_query_with_options(
        self,
        app_client: tuple[TestClient, AsyncMock, SessionManager],
    ) -> None:
        client, fake_service, _ = app_client
        with client.websocket_connect("/ws/codex") as ws:
            ws.send_json(
                {
                    "frame_type": "codex-command",
                    "command": "hi",
                    "options": {
                        "cwd": "/tmp",
                        "permissionMode": "acceptEdits",
                    },
                },
            )
            # 再发一条 unknown 让主循环切片，保证 codex-command 的 bg_task
            # 已被 create_task 调度过（route 在 _handle 完后立即返回，
            # AsyncMock 的 query 即被 await）
            ws.send_json({"frame_type": "_ping_unknown_"})
            # 拿到 unknown 的 error 帧——证明主循环还活着
            err = ws.receive_json()
            assert err.get("frame_type") == "error"

        # ws 关闭后 server 端 bg_task 已 await 完
        fake_service.query.assert_called_once()
        kwargs = fake_service.query.call_args.kwargs
        assert kwargs["command"] == "hi"
        assert kwargs["cwd"] == "/tmp"
        assert kwargs["permission_mode"] == "acceptEdits"
        assert kwargs["resume"] is False
        assert kwargs["model"] is None
        # 没传 sessionId → 生成 pending-XXX 占位
        sid = kwargs["session_id"]
        assert isinstance(sid, str) and sid.startswith("pending-")
        assert kwargs["kongming_thread_id"] is None

    def test_passes_thread_id_query_to_service(
        self,
        app_client: tuple[TestClient, AsyncMock, SessionManager],
    ) -> None:
        client, fake_service, _ = app_client
        with client.websocket_connect("/ws/codex?thread_id=thread-aaaaaaaaaaaa") as ws:
            ws.send_json(
                {
                    "frame_type": "codex-command",
                    "command": "hi",
                    "options": {},
                },
            )
            ws.send_json({"frame_type": "_ping_unknown_"})
            err = ws.receive_json()
            assert err.get("frame_type") == "error"

        fake_service.query.assert_called_once()
        kwargs = fake_service.query.call_args.kwargs
        assert kwargs["kongming_thread_id"] == "thread-aaaaaaaaaaaa"

    def test_resume_without_session_id_returns_error(
        self,
        app_client: tuple[TestClient, AsyncMock, SessionManager],
    ) -> None:
        client, fake_service, _ = app_client
        with client.websocket_connect("/ws/codex") as ws:
            ws.send_json(
                {
                    "frame_type": "codex-command",
                    "command": "hi",
                    "options": {"resume": True},
                },
            )
            msg = ws.receive_json()
            assert msg.get("frame_type") == "error"
            assert "resume" in msg.get("error", "").lower()
        # 没有真正 spawn
        fake_service.query.assert_not_called()


class TestAbortSession:
    """``abort-session`` → service.abort + complete(aborted=True)。"""

    def test_abort_calls_service_and_sends_complete(
        self,
        app_client: tuple[TestClient, AsyncMock, SessionManager],
    ) -> None:
        client, fake_service, _ = app_client
        with client.websocket_connect("/ws/codex") as ws:
            ws.send_json(
                {
                    "frame_type": "abort-session",
                    "sessionId": "019dee",
                    "provider": "codex",
                },
            )
            msg = ws.receive_json()
            assert msg.get("frame_type") == "complete"
            assert msg.get("aborted") is True
            assert msg.get("sessionId") == "019dee"
            assert msg.get("provider") == "codex"

        fake_service.abort.assert_awaited_once_with("019dee")


class TestCheckSessionStatus:
    """``check-session-status`` → session-status 帧（active/inactive 两路）。"""

    def test_inactive_session_returns_status_false(
        self,
        app_client: tuple[TestClient, AsyncMock, SessionManager],
    ) -> None:
        client, _, _ = app_client
        with client.websocket_connect("/ws/codex") as ws:
            ws.send_json(
                {
                    "frame_type": "check-session-status",
                    "sessionId": "ghost",
                },
            )
            msg = ws.receive_json()
            assert msg.get("frame_type") == "session-status"
            assert msg.get("sessionId") == "ghost"
            assert msg.get("isProcessing") is False

    def test_active_session_replaces_writer_and_returns_true(
        self,
        app_client: tuple[TestClient, AsyncMock, SessionManager],
    ) -> None:
        client, _, sessions = app_client
        # 预填一个活跃 session（writer 用 dummy）
        loop = asyncio.new_event_loop()
        try:
            dummy_writer = AsyncMock()
            loop.run_until_complete(sessions.register("real-sid", dummy_writer))
        finally:
            loop.close()

        with client.websocket_connect("/ws/codex") as ws:
            ws.send_json(
                {
                    "frame_type": "check-session-status",
                    "sessionId": "real-sid",
                },
            )
            msg = ws.receive_json()
            assert msg.get("frame_type") == "session-status"
            assert msg.get("sessionId") == "real-sid"
            assert msg.get("isProcessing") is True

        # writer 被换过一次（不再是 dummy）
        record = sessions.get("real-sid")
        assert record is not None
        assert record.writer is not dummy_writer


class TestUnknownType:
    """未知 frame_type → error 帧 + 主循环不退出。"""

    def test_unknown_type_returns_error_and_keeps_loop(
        self,
        app_client: tuple[TestClient, AsyncMock, SessionManager],
    ) -> None:
        client, _, _ = app_client
        with client.websocket_connect("/ws/codex") as ws:
            ws.send_json({"frame_type": "foo-bar"})
            msg1 = ws.receive_json()
            assert msg1.get("frame_type") == "error"
            assert "unknown" in msg1.get("error", "").lower()

            # 再发一条，验证主循环仍活
            ws.send_json({"frame_type": "still-bogus"})
            msg2 = ws.receive_json()
            assert msg2.get("frame_type") == "error"


class TestWebSocketDisconnect:
    """客户端断连 → 主循环安全退出，不主动 abort 子进程。"""

    def test_disconnect_does_not_call_abort(
        self,
        app_client: tuple[TestClient, AsyncMock, SessionManager],
    ) -> None:
        client, fake_service, _ = app_client
        with client.websocket_connect("/ws/codex") as ws:
            # 立即关闭，不发任何帧
            ws.close()
        # 断连不应触发 service.abort
        fake_service.abort.assert_not_called()
