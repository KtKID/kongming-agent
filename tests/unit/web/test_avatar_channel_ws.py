"""Avatar WebSocket channel 单元测试。

本脚本验证 `/ws/avatar/v1/threads/{threadId}` 的真实 TestClient 链路。关键流程是
用 FakeThreadManager/FakeBridge 替代真实 LLM runtime，连接 Avatar channel 后验证
thread.history、user.input、普通 S2C frame、approval.ack 三态和非法 thread。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from core.contracts import Event
from hosts.web.app import create_app
from hosts.web.threads.metadata import ThreadMetadata
from hosts.web.websocket.event_sink import WSEventSink
from hosts.web.websocket.fanout import WebSocketFanout
from infrastructure.config.models import Config
from network.manager import reset_network_manager_for_test
from tests.unit.test_web_app_lifespan import _seed_password

THREAD_ID = "thread-aaaaaaaaaaaa"
OTHER_THREAD_ID = "thread-bbbbbbbbbbbb"


class _FakeRuntime:
    """测试 runtime，提供空 session history。"""

    _sessions: dict[str, Any] = {}


class _FakeBridge:
    """测试 bridge，记录 run_once 并通过 WSEventSink 推普通 S2C frame。"""

    def __init__(self, sinks: list[Any]) -> None:
        """初始化 fake bridge。

        关键输入：cell event sinks。
        关键输出：可记录调用并发出事件的 bridge。
        """
        self._sinks = sinks
        self.calls: list[dict[str, Any]] = []

    async def run_once(
        self,
        text: str,
        *,
        reasoning_effort: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        """记录用户输入并发出普通 generic_chat S2C 事件。

        关键输入：文本、reasoning effort 和附件。
        关键输出：content.delta/turn 帧通过 WSEventSink 进入 WS。
        """
        self.calls.append(
            {
                "text": text,
                "reasoning_effort": reasoning_effort,
                "attachments": attachments,
            }
        )
        for sink in self._sinks:
            await sink.emit(Event(kind="turn.start", run_id="fake-run-1", turn=1))
            await sink.emit(
                Event(
                    kind="content.delta",
                    run_id="fake-run-1",
                    turn=1,
                    payload={"delta": "avatar reply", "seq": 1},
                )
            )
            await sink.emit(Event(kind="turn.end", run_id="fake-run-1", turn=1))


class _FakeCell:
    """测试 cell，提供 Avatar channel 所需字段和 attach/detach。"""

    def __init__(self, thread_id: str, metadata: ThreadMetadata) -> None:
        """初始化 fake cell。

        关键输入：thread_id 和 metadata。
        关键输出：带 WSEventSink 和 FakeBridge 的 cell。
        """
        self.thread_id = thread_id
        self.metadata = metadata
        self.runtime = _FakeRuntime()
        self.fanout = WebSocketFanout()
        self.event_sinks: list[Any] = [WSEventSink(self.fanout, thread_id=thread_id)]
        self.bridge = _FakeBridge(self.event_sinks)
        self.current_run_task = None
        self.last_active_at = 0.0
        self.status = "idle"

    def attach_ws(self, new_ws: Any) -> None:
        """把测试 WS 注册到所有 sink。"""
        for sink in self.event_sinks:
            attach = getattr(sink, "attach_ws", None)
            if callable(attach):
                attach(new_ws)

    def detach_ws(self, ws: Any) -> None:
        """把测试 WS 从所有 sink 注销。"""
        for sink in self.event_sinks:
            detach = getattr(sink, "detach_ws", None)
            if callable(detach):
                detach(ws)

    def touch(self) -> None:
        """记录 cell 活跃时间推进。"""
        self.last_active_at += 1.0


class _AvatarWSFakeTM:
    """Avatar WS 测试 ThreadManager。"""

    def __init__(self) -> None:
        """初始化 fake thread、cell 和审批记录。"""
        metadata = ThreadMetadata(
            id=THREAD_ID,
            name="Avatar",
            preset_id="local-default",
            backend_kind="generic_chat",
            created_at=1.0,
            updated_at=2.0,
            message_count=0,
        )
        self._threads: dict[str, ThreadMetadata] = {THREAD_ID: metadata}
        self._cells: dict[str, _FakeCell] = {}
        self.refresh_calls: list[str] = []
        self.resolve_calls: list[tuple[str, str, str]] = []
        self._started = False
        self._closed = False

    @property
    def started(self) -> bool:
        """返回 start 状态。"""
        return self._started

    @property
    def closed(self) -> bool:
        """返回 close 状态。"""
        return self._closed

    async def start(self) -> None:
        """标记 fake manager 已启动。"""
        self._started = True

    async def aclose_all(self) -> None:
        """标记 fake manager 已关闭。"""
        self._closed = True

    def list_threads(self) -> list[ThreadMetadata]:
        """返回 fake thread metadata 列表。"""
        return list(self._threads.values())

    async def boot_or_attach(self, thread_id: str) -> _FakeCell:
        """返回或创建 fake cell。"""
        metadata = self._threads.get(thread_id)
        if metadata is None:
            raise KeyError(thread_id)
        cell = self._cells.get(thread_id)
        if cell is None:
            cell = _FakeCell(thread_id, metadata)
            self._cells[thread_id] = cell
        return cell

    def get_cell(self, thread_id: str) -> _FakeCell | None:
        """按 thread_id 返回 fake cell。"""
        return self._cells.get(thread_id)

    async def ensure_cell_runtime_preset_current(self, thread_id: str) -> bool:
        """记录 runtime refresh 并返回成功。"""
        self.refresh_calls.append(thread_id)
        return True

    def resolve_approval(self, thread_id: str, call_id: str, action: str) -> None:
        """记录 approval.ack 三态 action。"""
        self.resolve_calls.append((thread_id, call_id, action))

    def add_non_generic_thread(self) -> None:
        """添加一个非 generic_chat thread，用于非法 thread 测试。"""
        self._threads[OTHER_THREAD_ID] = ThreadMetadata(
            id=OTHER_THREAD_ID,
            name="Claude",
            preset_id="",
            backend_kind="claude_code",
            created_at=1.0,
            updated_at=2.0,
            message_count=0,
        )


def _make_cfg() -> Config:
    """构造 Web 测试配置。"""
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


def _client(tmp_path: Path, tm: _AvatarWSFakeTM) -> TestClient:
    """创建 Avatar WS TestClient。

    关键输入：pytest tmp_path 和 fake ThreadManager。
    关键输出：已进入 lifespan 的 TestClient。
    """
    reset_network_manager_for_test()
    _seed_password(tmp_path, "pwd")
    app = create_app(_make_cfg(), tm, home_dir=tmp_path)
    client = TestClient(app)
    client.__enter__()
    return client


def test_avatar_ws_user_input_runs_generic_bridge_and_streams_frames(tmp_path: Path) -> None:
    """验证 Avatar WS user.input 会复用 bridge.run_once 并返回普通 S2C frame。"""
    tm = _AvatarWSFakeTM()
    client = _client(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/avatar/v1/threads/{THREAD_ID}") as ws:
            history = ws.receive_json()
            assert history["frame_type"] == "thread.history"
            assert history["messages"] == []

            attachment = {
                "asset_id": "asset-1",
                "kind": "image",
                "mime_type": "image/png",
                "size_bytes": 128,
                "width": 16,
                "height": 16,
                "duration_ms": None,
                "preview_url": "/api/uploads/asset-1",
                "status": "ready",
            }
            ws.send_json(
                {
                    "frame_type": "user.input",
                    "text": "hello avatar",
                    "request_id": "req-avatar-1",
                    "reasoning_effort": "high",
                    "attachments": [attachment],
                }
            )
            frames = [ws.receive_json(), ws.receive_json(), ws.receive_json()]
            assert [frame["frame_type"] for frame in frames] == [
                "turn.start",
                "content.delta",
                "turn.end",
            ]
            assert frames[1]["delta"] == "avatar reply"

        cell = tm.get_cell(THREAD_ID)
        assert cell is not None
        assert tm.refresh_calls == [THREAD_ID]
        assert cell.bridge.calls == [
            {
                "text": "hello avatar",
                "reasoning_effort": "high",
                "attachments": [attachment],
            }
        ]
    finally:
        client.__exit__(None, None, None)


def test_avatar_ws_approval_ack_routes_three_actions(tmp_path: Path) -> None:
    """验证 approval.ack 三态均透传到 ThreadManager.resolve_approval。"""
    tm = _AvatarWSFakeTM()
    client = _client(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/avatar/v1/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()
            for action in ("accept_once", "accept_for_session", "reject"):
                ws.send_json(
                    {
                        "frame_type": "approval.ack",
                        "call_id": f"call-{action}",
                        "action": action,
                    }
                )
            time.sleep(0.1)

        assert tm.resolve_calls == [
            (THREAD_ID, "call-accept_once", "accept_once"),
            (THREAD_ID, "call-accept_for_session", "accept_for_session"),
            (THREAD_ID, "call-reject", "reject"),
        ]
    finally:
        client.__exit__(None, None, None)


def test_avatar_ws_rejects_invalid_thread(tmp_path: Path) -> None:
    """验证 Avatar WS 拒绝不存在和非 generic_chat thread。"""
    tm = _AvatarWSFakeTM()
    tm.add_non_generic_thread()
    client = _client(tmp_path, tm)
    try:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/avatar/v1/threads/thread-cccccccccccc"):
                pass
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/ws/avatar/v1/threads/{OTHER_THREAD_ID}"):
                pass
    finally:
        client.__exit__(None, None, None)
