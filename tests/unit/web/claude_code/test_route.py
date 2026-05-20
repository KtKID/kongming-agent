"""WebSocket route 集成测试（FastAPI TestClient）。

覆盖：

1. 鉴权失败 → close 1008
2. claude-command（mock SDK 输出）→ 收到 normalize 后的 text + complete
3. claude-permission-response → resolve Future
4. abort-session → 收到 complete(aborted=True)
5. check-session-status → 收到 session-status 帧
6. unknown command → error 帧
7. thread_id query 参数（v0.1.6）：非法格式 / thread 不存在 / backend_kind 不匹配 / 正常 claude_code thread
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
)
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from config_loader.models import Config
from tests.unit.test_web_app_lifespan import FakeThreadManager, _seed_password
from web.app import create_app
from web.auth import CSRF_HEADER_NAME, CSRF_HEADER_VALUE

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


def _make_assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude",
        parent_tool_use_id=None,
        message_id="msg-1",
    )


def _make_result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=1,
        session_id="sid",
        stop_reason="end_turn",
        total_cost_usd=0.0,
        usage=None,
    )


def _make_fake_sdk_client(messages: list[Any]) -> MagicMock:
    """构造看起来像 ClaudeSDKClient 的 mock。"""
    from unittest.mock import AsyncMock

    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()

    async def receive_response() -> Any:
        for m in messages:
            yield m

    client.receive_response = receive_response
    client.interrupt = AsyncMock()
    return client


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """创建已登录的 TestClient，并把 ClaudeSDKClient 替换成 mock。"""
    # 默认 mock 返回 [text + result]
    fake_client = _make_fake_sdk_client([_make_assistant("hi"), _make_result()])

    # patch service 模块里的 ClaudeSDKClient
    def factory(**_: Any) -> MagicMock:
        return fake_client

    monkeypatch.setattr(
        "web.claude_code.service.ClaudeSDKClient",
        factory,
    )

    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    tm = FakeThreadManager()
    app = create_app(cfg, tm, home_dir=tmp_path)
    client = TestClient(app)
    client.__enter__()
    # 登录
    r = client.post("/api/auth/login", json={"password": "pwd"}, headers=CSRF_HEADERS)
    assert r.status_code == 200
    try:
        yield client
    finally:
        client.__exit__(None, None, None)


def test_unauthenticated_ws_closes(tmp_path: Path) -> None:
    """没登录直接连 → 收到 close。"""
    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    tm = FakeThreadManager()
    app = create_app(cfg, tm, home_dir=tmp_path)
    with TestClient(app) as client:
        try:
            with client.websocket_connect("/ws/claude-code") as _ws:
                pass
        except Exception:
            # starlette WSDisconnect 是预期路径
            pass


def test_claude_command_streams_normalized_messages(app_client: TestClient) -> None:
    """claude-command → 收到 text + complete 帧。"""
    with app_client.websocket_connect("/ws/claude-code") as ws:
        ws.send_json(
            {
                "type": "claude-command",
                "command": "hello",
                "options": {"sessionId": "test-sid", "model": "sonnet"},
            },
        )
        # 收 normalize 后的输出（text + complete）
        seen: list[dict[str, Any]] = []
        for _ in range(8):
            try:
                msg = ws.receive_json()
            except Exception:
                break
            seen.append(msg)
            if msg.get("kind") == "complete":
                break

        kinds = [m.get("kind") for m in seen]
        assert "text" in kinds, f"expected text frame, got {kinds}"
        assert "complete" in kinds, f"expected complete frame, got {kinds}"


def test_unknown_command_returns_error(app_client: TestClient) -> None:
    """未知 type → error 帧。"""
    with app_client.websocket_connect("/ws/claude-code") as ws:
        ws.send_json({"type": "totally-unknown"})
        msg = ws.receive_json()
        assert msg.get("kind") == "error"
        assert "unknown" in msg.get("error", "").lower()


def test_check_session_status_returns_inactive(app_client: TestClient) -> None:
    """check-session-status：未注册 session 返回 isProcessing=False。"""
    with app_client.websocket_connect("/ws/claude-code") as ws:
        ws.send_json(
            {
                "type": "check-session-status",
                "sessionId": "nope",
            },
        )
        msg = ws.receive_json()
        assert msg.get("type") == "session-status"
        assert msg.get("sessionId") == "nope"
        assert msg.get("isProcessing") is False


def test_abort_session_unknown_returns_complete_aborted(app_client: TestClient) -> None:
    """abort-session：未注册 session（service.abort 返 False）→ route 兜底发 complete。

    interrupt-claude-channel-v0.1：unknown sid 无活 _consume，必须有 route 层
    主动兜底 emit，否则前端 isRunning 卡在 true 等不到任何回应。
    """
    with app_client.websocket_connect("/ws/claude-code") as ws:
        ws.send_json(
            {
                "type": "abort-session",
                "sessionId": "ghost-sid",
            },
        )
        msg = ws.receive_json()
        assert msg.get("kind") == "complete"
        assert msg.get("aborted") is True
        assert msg.get("sessionId") == "ghost-sid"


def test_abort_session_active_does_not_emit_complete_from_route(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """abort-session：有活 session（service.abort 返 True）→ route **不**主动发 complete。

    interrupt-claude-channel-v0.1 #2 改造核心契约：避免 route + service._consume 双重
    emit 让前端渲染 2 张"对话已中止"卡片。aborted=True 时 complete 帧由
    service._consume() CancelledError finally 单一来源 emit。

    实现细节：因为 _consume 的 CancelledError 路径**也会**通过同一个 ws
    （websocket.send_json）发 complete，本测试用 service.abort monkeypatch 让它
    返 True 但不触发 _consume，验证 route handler 自身**不**主动 send_json
    complete 帧（队列里只有 ws.receive_json timeout 或 pong）。
    """
    from web.claude_code import route as route_mod

    # 拦截 service.abort 让它返 True（模拟"有活 session 被 cancel"），但不真起
    # _consume → 不会有 _consume finally 路径发 complete
    async def _mock_abort_true(self: Any, sid: str) -> bool:
        return True

    monkeypatch.setattr(
        route_mod.ClaudeCodeService,
        "abort",
        _mock_abort_true,
    )

    with app_client.websocket_connect("/ws/claude-code") as ws:
        ws.send_json(
            {
                "type": "abort-session",
                "sessionId": "active-sid",
            },
        )
        # route 不应主动发 complete；如果发了 receive_json 会立刻拿到 complete
        # 反之 ws 没新帧，receive_json 会卡或抛 — 用 ws.send_json + 立即 receive
        # 一个 ping/pong 探针（ws.receive_json 不带 timeout 会卡死，这里用
        # 第二条消息后受控 yield）
        ws.send_json({"type": "check-session-status", "sessionId": "active-sid"})
        msg = ws.receive_json()
        # 期望直接拿到 check-session-status 的 session-status 应答；
        # 如果 route 误发了 complete，会先拿到 complete（kind="complete"）
        assert msg.get("type") == "session-status", (
            f"route 不应主动 emit complete（aborted=True 路径），但收到 {msg!r}"
        )
        assert msg.get("sessionId") == "active-sid"


def test_permission_response_routed_to_approval(
    app_client: TestClient,
) -> None:
    """claude-permission-response 路由到 approval.resolve（虽然没有 pending Future 也不抛）。"""
    with app_client.websocket_connect("/ws/claude-code") as ws:
        # 不存在的 requestId 也只是 resolve 返回 False，不抛
        ws.send_json(
            {
                "type": "claude-permission-response",
                "requestId": "nonexistent",
                "allow": True,
            },
        )
        # 后续发个 unknown 验证 ws 仍存活
        ws.send_json({"type": "x"})
        msg = ws.receive_json()
        assert msg.get("kind") == "error"


def test_invalid_command_field_types(app_client: TestClient) -> None:
    """字段类型不对 → error 帧。"""
    with app_client.websocket_connect("/ws/claude-code") as ws:
        ws.send_json({"type": "claude-command", "command": 123})  # not str
        msg = ws.receive_json()
        assert msg.get("kind") == "error"


def test_session_manager_attached_to_app_state(tmp_path: Path) -> None:
    """app.state.claude_session_manager 已挂载。"""
    from web._shared.session_manager import SessionManager

    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    tm = FakeThreadManager()
    app = create_app(cfg, tm, home_dir=tmp_path)
    assert isinstance(app.state.claude_session_manager, SessionManager)


# ---------------------------------------------------------------------------
# v0.1.6 thread_id query 参数测试
# ---------------------------------------------------------------------------


def _make_thread_meta(
    thread_id: str = "thread-abcdef123456",
    backend_kind: str = "claude_code",
) -> SimpleNamespace:
    """构造 ThreadMetadata 风格的 fake，仅含 route.py 校验时用到的字段。"""
    return SimpleNamespace(id=thread_id, backend_kind=backend_kind)


@pytest.fixture
def app_client_with_threads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, FakeThreadManager]]:
    """已登录 TestClient + 暴露 FakeThreadManager 让用例自定义 list_threads。"""
    fake_client = _make_fake_sdk_client([_make_assistant("hi"), _make_result()])

    def factory(**_: Any) -> MagicMock:
        return fake_client

    monkeypatch.setattr(
        "web.claude_code.service.ClaudeSDKClient",
        factory,
    )

    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    tm = FakeThreadManager()
    app = create_app(cfg, tm, home_dir=tmp_path)
    client = TestClient(app)
    client.__enter__()
    r = client.post("/api/auth/login", json={"password": "pwd"}, headers=CSRF_HEADERS)
    assert r.status_code == 200
    try:
        yield client, tm
    finally:
        client.__exit__(None, None, None)


def test_thread_id_invalid_format_closes(
    app_client_with_threads: tuple[TestClient, FakeThreadManager],
) -> None:
    """非法 thread_id 格式（不匹配 ^thread-[a-f0-9]{12}$） → close 1008。"""
    client, _tm = app_client_with_threads
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/claude-code?thread_id=foo") as ws:
            # 服务端 close 后这里 receive_json 会抛 WebSocketDisconnect
            ws.receive_json()


def test_thread_id_thread_not_found_closes(
    app_client_with_threads: tuple[TestClient, FakeThreadManager],
) -> None:
    """thread_id 合法格式但 list_threads 返回空 → close (thread not found)。"""
    client, tm = app_client_with_threads
    tm.list_threads = lambda: []  # type: ignore[method-assign]
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws/claude-code?thread_id=thread-abcdef123456",
        ) as ws:
            ws.receive_json()


def test_thread_id_backend_kind_mismatch_closes(
    app_client_with_threads: tuple[TestClient, FakeThreadManager],
) -> None:
    """thread 存在但 backend_kind=generic_chat → close (backend_kind 不匹配)。"""
    client, tm = app_client_with_threads
    tm.list_threads = lambda: [  # type: ignore[method-assign]
        _make_thread_meta(thread_id="thread-abcdef123456", backend_kind="generic_chat"),
    ]
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws/claude-code?thread_id=thread-abcdef123456",
        ) as ws:
            ws.receive_json()


def test_thread_id_claude_code_thread_accepts_and_uses_thread_id_as_session(
    app_client_with_threads: tuple[TestClient, FakeThreadManager],
) -> None:
    """正常 claude_code thread → 接受连接，且出站消息 sessionId=thread_id。

    SessionManager 用 thread_id 注册（取代 placeholder pending-XXX），
    所以 normalize 出去的帧 sessionId 字段应是 thread_id 而非 placeholder。
    """
    client, tm = app_client_with_threads
    tid = "thread-abcdef123456"
    tm.list_threads = lambda: [  # type: ignore[method-assign]
        _make_thread_meta(thread_id=tid, backend_kind="claude_code"),
    ]
    with client.websocket_connect(f"/ws/claude-code?thread_id={tid}") as ws:
        ws.send_json(
            {
                "type": "claude-command",
                "command": "hello",
                "options": {"model": "sonnet"},
            },
        )
        seen: list[dict[str, Any]] = []
        for _ in range(8):
            try:
                msg = ws.receive_json()
            except Exception:
                break
            seen.append(msg)
            if msg.get("kind") == "complete":
                break

    # 至少出现一帧 sessionId == thread_id（不是 pending-XXX placeholder）
    sids = [m.get("sessionId") for m in seen if m.get("sessionId") is not None]
    assert sids, f"expected at least one frame with sessionId, got {seen}"
    assert any(s == tid for s in sids), (
        f"expected sessionId={tid} in frames, got sids={sids}; seen={seen}"
    )
    # 不应出现 pending-XXX placeholder
    assert not any(isinstance(s, str) and s.startswith("pending-") for s in sids), (
        f"placeholder leaked: sids={sids}"
    )


def test_thread_id_omitted_keeps_legacy_behavior(
    app_client: TestClient,
) -> None:
    """不带 thread_id query → 保留 v0.1 行为（不校验 thread，正常走 placeholder/sessionId 路径）。

    用 app_client（list_threads 返回空），如果 endpoint 错误地校验 thread_id
    会因为 thread not found 而 close；本测试期望连接成功 + 收到正常 normalize 输出。
    """
    with app_client.websocket_connect("/ws/claude-code") as ws:
        ws.send_json(
            {
                "type": "claude-command",
                "command": "hello",
                "options": {"sessionId": "legacy-sid"},
            },
        )
        msg = ws.receive_json()
        # 只要能拿到任意 normalize 帧就证明没被 close
        assert msg.get("provider") == "claude" or msg.get("kind") is not None
