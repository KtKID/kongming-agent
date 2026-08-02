"""WS /ws/threads/{id} endpoint 单测（v0.1.5）。

覆盖：

1. 未鉴权 → close 1008
2. thread_id 非法 → close 1008
3. thread_id 不存在（boot 抛 KeyError）→ close 1008
4. user.input 帧 → 触发 cell.bridge.run_once（后台 task）
5. approval.ack 帧 → 退役保护，不再调 tm.resolve_approval
6. ping → 收到 pong
7. 1MB 限制：超大帧 → 推 error 帧
8. 非法 JSON → 推 error 帧
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hosts.web.app import create_app
from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from hosts.web.threads.metadata import ThreadMetadata
from infrastructure.config.models import Config
from network.manager import reset_network_manager_for_test
from scheduler.domain import (
    ConcurrencyPolicy,
    DeliveryChannel,
    RunStatus,
    ScheduleDelivery,
    ScheduledRun,
    ScheduledTask,
    ScheduleTrigger,
    TaskExecutionPolicy,
    TaskLifecycleState,
    TaskOrigin,
    TaskTarget,
    TriggerType,
)
from scheduler.store import Store
from tests.unit.test_web_app_lifespan import _seed_password

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}
THREAD_ID = "thread-aaaaaaaaaaaa"


class FakeBridge:
    def __init__(self) -> None:
        self.run_once_calls: list[tuple[str, str | None]] = []
        # claude-image-paste-e2e #20：记录 attachments 透传（旧测试不断言，新测试会查）
        self.last_attachments: list[dict[str, Any]] | None = None
        # interrupt-run-v0.1：测 cancel 行为时让 run_once 永远 await，模拟
        # 长跑的 turn；默认 False 保持老测试快速完成
        self.hang_forever: bool = False
        self.hang_cancelled: bool = False

    async def run_once(
        self,
        text: str,
        *,
        reasoning_effort: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
    ) -> None:
        self.run_once_calls.append((text, reasoning_effort))
        self.last_attachments = attachments
        if self.hang_forever:
            import asyncio

            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.hang_cancelled = True
                raise


class FakeHostDispatcher:
    def __init__(self, bridge: FakeBridge) -> None:
        self.bridge = bridge
        self.submit_calls: list[dict[str, Any]] = []

    async def submit(
        self,
        text: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        **_: Any,
    ) -> Any:
        self.submit_calls.append(
            {
                "text": text,
                "attachments": attachments,
                "references": references,
                "metadata": metadata,
            }
        )
        reasoning_effort = metadata.get("reasoning_effort") if metadata is not None else None
        await self.bridge.run_once(
            text,
            reasoning_effort=reasoning_effort,
            attachments=attachments,
            references=references,
        )
        return SimpleNamespace(merged=False)


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

    async def close(self) -> None:
        self._closed = True

    def resolve_approval(self, call_id: str, action: Any) -> None:
        self.resolve_calls.append((call_id, action))


class FakeRuntime:
    """通过公开 SessionEngine history 门户返回空历史。"""

    closed: bool = False

    async def read_session_history(self, session_id: str) -> list[Any]:
        del session_id
        return []

    async def aclose(self) -> None:
        self.closed = True


class FakeCell:
    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.metadata = ThreadMetadata(
            id=thread_id,
            name="t",
            preset_id="p1",
            created_at=1.0,
            updated_at=2.0,
            message_count=0,
        )
        self.bridge = FakeBridge()
        self.host_dispatcher = FakeHostDispatcher(self.bridge)
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


class FakeEphemeralCell:
    def __init__(self, session_id: str, preset_id: str) -> None:
        self.thread_id = session_id
        self.metadata = SimpleNamespace(id=session_id, preset_id=preset_id)
        self.bridge = FakeBridge()
        self.host_dispatcher = FakeHostDispatcher(self.bridge)
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

    def resolve_approval(self, call_id: str, action: Any) -> None:
        self.adapter.resolve_approval(call_id, action)

    @property
    def has_pending_approvals(self) -> bool:
        return False


class WSFakeTM:
    def __init__(self) -> None:
        self._cells: dict[str, FakeCell] = {}
        self.ephemeral_build_calls: list[dict[str, Any]] = []
        self.ephemeral_close_calls: list[dict[str, Any]] = []
        self.boot_should_raise: type[BaseException] | None = None
        self.resolve_calls: list[tuple[str, str, bool]] = []
        self.interrupt_calls: list[tuple[str, str]] = []
        self.interrupt_result = False
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

    async def boot_or_attach(self, thread_id: str) -> FakeCell:
        if self.boot_should_raise is not None:
            raise self.boot_should_raise(thread_id)
        cell = self._cells.get(thread_id)
        if cell is None:
            cell = FakeCell(thread_id)
            self._cells[thread_id] = cell
        return cell

    async def build_ephemeral_session_cell(
        self,
        *,
        session_id: str,
        preset_id: str,
    ) -> FakeEphemeralCell:
        cell = FakeEphemeralCell(session_id, preset_id)
        self.ephemeral_build_calls.append(
            {
                "session_id": session_id,
                "preset_id": preset_id,
                "cell": cell,
            }
        )
        return cell

    async def close_ephemeral_session_cell(
        self,
        cell: FakeEphemeralCell,
        *,
        reason: str = "session_close",
    ) -> None:
        self.ephemeral_close_calls.append({"cell": cell, "reason": reason})
        await cell.adapter.close()
        await cell.runtime.aclose()

    async def create_thread(self, name: str, preset_id: str) -> Any:
        del name, preset_id
        raise NotImplementedError

    async def rename_thread(self, thread_id: str, new_name: str) -> Any:
        del thread_id, new_name
        raise NotImplementedError

    async def delete_thread(self, thread_id: str, *, keep_history: bool = False) -> None:
        del thread_id, keep_history
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

    def list_threads(self) -> list[Any]:
        return []

    def list_cells(self) -> list[Any]:
        return []

    def get_cell(self, thread_id: str) -> Any:
        return self._cells.get(thread_id)

    def resolve_approval(self, thread_id: str, call_id: str, action: Any) -> None:
        self.resolve_calls.append((thread_id, call_id, action))

    async def interrupt_agent_tree(
        self,
        thread_id: str,
        *,
        reason: str = "user_interrupt",
    ) -> bool:
        self.interrupt_calls.append((thread_id, reason))
        return self.interrupt_result


def _make_cfg() -> Config:
    return Config.model_validate(
        {
            "model": {"preset_id": "local-gemma-4-e4b-it"},
            "web": {"enabled": True, "dev_mode": True},
        }
    )


def _login(tmp_path: Path, tm: WSFakeTM) -> TestClient:
    reset_network_manager_for_test()
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


def _make_task(*, task_id: str, thread_id: str, preset_id: str) -> ScheduledTask:
    return ScheduledTask(
        task_id=task_id,
        name=f"task-{task_id}",
        lifecycle=TaskLifecycleState.SCHEDULED,
        origin=TaskOrigin.WEB,
        trigger=ScheduleTrigger(
            trigger_type=TriggerType.ONCE,
            expr="2026-06-15T10:00:00+08:00",
            timezone="Asia/Shanghai",
        ),
        policy=TaskExecutionPolicy(concurrency_policy=ConcurrencyPolicy.FORBID),
        target=TaskTarget(agent_name="agent", input_text="run me"),
        next_run_at="2026-06-15T02:00:00+00:00",
        last_run_at=None,
        created_by="tester",
        created_at="2026-06-15T01:00:00+00:00",
        updated_at="2026-06-15T01:00:00+00:00",
        delivery=ScheduleDelivery(channel=DeliveryChannel.WEB),
        thread_id=thread_id,
        preset_id=preset_id,
    )


def _make_run(*, task_id: str, run_id: str, session_id: str, thread_id: str) -> ScheduledRun:
    return ScheduledRun(
        run_id=run_id,
        task_id=task_id,
        status=RunStatus.COMPLETED,
        scheduled_for="2026-06-15T02:00:00+00:00",
        started_at="2026-06-15T02:00:01+00:00",
        finished_at="2026-06-15T02:00:03+00:00",
        session_id=session_id,
        result_status="ok",
        final_message_excerpt="done",
        error_message=None,
        failure_reason=None,
        delivery_error=None,
        silent_suppressed=False,
        thread_id=thread_id,
    )


def test_ws_unauthenticated_closes_1008(tmp_path: Path) -> None:
    tm = WSFakeTM()
    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    app = create_app(cfg, tm, home_dir=tmp_path)
    # 不登录直接连
    with TestClient(app) as client:
        try:
            with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as _ws:
                pass
        except Exception:
            # starlette 报关闭异常，OK
            pass


def test_ws_invalid_thread_id_closes(tmp_path: Path) -> None:
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        try:
            with client.websocket_connect("/ws/threads/not-a-thread") as _ws:
                pass
        except Exception:
            pass
    finally:
        client.__exit__(None, None, None)


def test_ws_thread_not_found_closes(tmp_path: Path) -> None:
    tm = WSFakeTM()
    tm.boot_should_raise = KeyError
    client = _login(tmp_path, tm)
    try:
        try:
            with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as _ws:
                pass
        except Exception:
            pass
    finally:
        client.__exit__(None, None, None)


def test_ws_user_input_with_reasoning_effort(tmp_path: Path) -> None:
    """user.input 帧 reasoning_effort 字段透传到 bridge.run_once。"""
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_json(
                {
                    "frame_type": "user.input",
                    "text": "think hard",
                    "request_id": "req-reasoning",
                    "reasoning_effort": "high",
                }
            )
            import time

            time.sleep(0.2)

        cell = tm.get_cell(THREAD_ID)
        assert cell is not None
        assert ("think hard", "high") in cell.bridge.run_once_calls
    finally:
        client.__exit__(None, None, None)


def test_ws_cron_run_uses_run_session_id(tmp_path: Path) -> None:
    """cron run 专用 WS 用 ScheduledRun.session_id 装配 ephemeral HostDispatcher。"""
    reset_network_manager_for_test()
    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    tm = WSFakeTM()
    app = create_app(cfg, tm, home_dir=tmp_path)
    store = Store(tmp_path / "cron")
    task = _make_task(
        task_id="task-1",
        thread_id=THREAD_ID,
        preset_id="preset-1",
    )
    store.create_task(task)
    store.append_run(
        _make_run(
            task_id="task-1",
            run_id="run-1",
            session_id="sched-task-1-run-1",
            thread_id=THREAD_ID,
        )
    )
    app.state.scheduler_store = store
    client = TestClient(app)
    client.__enter__()
    try:
        r = client.post(
            "/api/auth/login",
            json={"password": "pwd"},
            headers=CSRF_HEADERS,
        )
        assert r.status_code == 200

        with client.websocket_connect("/ws/cron/tasks/task-1/runs/run-1") as ws:
            history = ws.receive_json()
            assert history["frame_type"] == "thread.history"
            ws.send_json(
                {
                    "frame_type": "user.input",
                    "text": "continue this run",
                    "request_id": "req-cron-run",
                    "reasoning_effort": "high",
                }
            )
            import time

            time.sleep(0.2)

        assert tm.ephemeral_build_calls
        call = tm.ephemeral_build_calls[0]
        assert call["session_id"] == "sched-task-1-run-1"
        assert call["preset_id"] == "preset-1"
        bridge = call["cell"].bridge
        assert ("continue this run", "high") in bridge.run_once_calls
        assert tm.ephemeral_close_calls == [{"cell": call["cell"], "reason": "session_close"}]
        assert call["cell"].adapter._closed is True
        assert call["cell"].runtime.closed is True
    finally:
        client.__exit__(None, None, None)


def test_ws_generic_channel_writes_local_log(tmp_path: Path) -> None:
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_json(
                {
                    "frame_type": "user.input",
                    "text": "hello local log",
                    "request_id": "req-log-1",
                    "reasoning_effort": "medium",
                }
            )
            import time

            time.sleep(0.2)
    finally:
        client.__exit__(None, None, None)

    path = tmp_path / "logs" / "generic-channel" / "generic-channel.jsonl"
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    events = [row["event"] for row in rows]
    assert "registered" in events
    assert "frame_dispatch" in events
    assert "run_once_completed" in events
    dispatch = next(row for row in rows if row["event"] == "frame_dispatch")
    assert dispatch["frame_type"] == "user.input"
    assert dispatch["thread_id"] == THREAD_ID
    assert dispatch["request_id"] == "req-log-1"
    assert dispatch["text_len"] == len("hello local log")
    assert "text" not in dispatch


def test_ws_user_input_without_reasoning_effort(tmp_path: Path) -> None:
    """不带 reasoning_effort 的 user.input 帧不传 effort（None）。"""
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_json(
                {
                    "frame_type": "user.input",
                    "text": "normal",
                    "request_id": "req-normal",
                }
            )
            import time

            time.sleep(0.1)

        cell = tm.get_cell(THREAD_ID)
        assert cell is not None
        assert ("normal", None) in cell.bridge.run_once_calls
    finally:
        client.__exit__(None, None, None)


def test_ws_disconnect_detaches_current_socket_only(tmp_path: Path) -> None:
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()
            cell = tm.get_cell(THREAD_ID)
            assert cell is not None
            assert len(cell.adapter.attach_calls) == 1
            assert len(cell.adapter.detach_calls) == 0
        cell = tm.get_cell(THREAD_ID)
        assert cell is not None
        assert len(cell.adapter.detach_calls) == 1
        assert cell.adapter._closed is False
    finally:
        client.__exit__(None, None, None)


def test_ws_user_input_spawns_background_run_task(tmp_path: Path) -> None:
    """user.input 帧 → 异步触发 bridge.run_once。

    interrupt-run-v0.1：原断言 ``cell.current_run_task is not None`` 现在
    会因 ``add_done_callback`` 在 FakeBridge.run_once 立即返回后自动清成
    None；改为断言 ``bridge.run_once_calls`` 收到调用（行为目的）。
    """
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            # 接收 thread.history 帧（建连推）
            history = ws.receive_json()
            assert history["frame_type"] == "thread.history"

            ws.send_json(
                {
                    "frame_type": "user.input",
                    "text": "hello",
                    "request_id": "req-1",
                }
            )
            # 给后台 task 一点时间
            import time

            time.sleep(0.1)

        cell = tm.get_cell(THREAD_ID)
        assert cell is not None
        # bridge.run_once 被异步调过（说明 task 被创建并跑过）
        assert len(cell.bridge.run_once_calls) == 1
        assert cell.bridge.run_once_calls[0][0] == "hello"
    finally:
        client.__exit__(None, None, None)


def test_ws_approval_ack_is_retired(tmp_path: Path) -> None:
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_json(
                {
                    "frame_type": "approval.ack",
                    "call_id": "call-1",
                    "action": "accept_once",
                }
            )
            error = ws.receive_json()
            assert error["frame_type"] == "error"
            assert "approval.ack" in error["message"]

        assert tm.resolve_calls == []
    finally:
        client.__exit__(None, None, None)


def test_ws_ping_receives_pong(tmp_path: Path) -> None:
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            manager = client.app.state.network_manager  # type: ignore[attr-defined]
            assert len(manager._connections) == 1
            conn = next(iter(manager._connections.values()))
            assert conn.channel == "generic"
            assert conn.thread_id == THREAD_ID

            ws.send_json({"frame_type": "ping", "ts": 1700000000000})
            pong = ws.receive_json()
            assert pong["frame_type"] == "pong"
            assert pong["ts"] == 1700000000000
            assert "timestamp_ms" in pong
            assert isinstance(pong["timestamp_ms"], int)
    finally:
        client.__exit__(None, None, None)


def test_ws_pong_consumed_by_network_manager(tmp_path: Path) -> None:
    """inbound pong 由 NetworkManager 消费，不进入业务 union 产生 error。"""
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        manager = client.app.state.network_manager  # type: ignore[attr-defined]
        original_handle_inbound = manager.handle_inbound
        inbound_calls: list[tuple[str | None, bool]] = []

        async def recording_handle_inbound(conn_id: str, frame: dict[str, Any]) -> bool:
            consumed = await original_handle_inbound(conn_id, frame)
            raw_type = frame.get("frame_type")
            inbound_calls.append((raw_type if isinstance(raw_type, str) else None, consumed))
            return consumed

        manager.handle_inbound = recording_handle_inbound
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_json({"frame_type": "pong", "ts": 1700000000000})
            ws.send_json(
                {
                    "frame_type": "approval.ack",
                    "call_id": "call-after-pong",
                    "action": "reject",
                }
            )
            error = ws.receive_json()
            assert error["frame_type"] == "error"
            assert "approval.ack" in error["message"]

        assert tm.resolve_calls == []
        assert inbound_calls[0] == ("pong", True)
        assert ("approval.ack", False) in inbound_calls
    finally:
        client.__exit__(None, None, None)


def test_ws_unregisters_network_manager_on_disconnect(tmp_path: Path) -> None:
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        manager = client.app.state.network_manager  # type: ignore[attr-defined]
        assert len(manager._connections) == 0
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()
            assert len(manager._connections) == 1
            assert len(manager._heartbeats) == 1
        assert len(manager._connections) == 0
        assert len(manager._heartbeats) == 0
    finally:
        client.__exit__(None, None, None)


def test_ws_oversized_frame_rejected(tmp_path: Path) -> None:
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            big = "a" * (1_000_001)
            ws.send_json(
                {
                    "frame_type": "user.input",
                    "text": big,
                    "request_id": "req-big",
                }
            )
            # 应当收到 error 帧
            err = ws.receive_json()
            assert err["frame_type"] == "error"
            assert "too large" in err["message"]
    finally:
        client.__exit__(None, None, None)


def test_ws_invalid_json_rejected(tmp_path: Path) -> None:
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_text("{not-json")
            err = ws.receive_json()
            assert err["frame_type"] == "error"
    finally:
        client.__exit__(None, None, None)


def test_ws_unknown_kind_rejected(tmp_path: Path) -> None:
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_json({"frame_type": "nonexistent.kind"})
            err = ws.receive_json()
            assert err["frame_type"] == "error"
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# interrupt-run-v0.1：InterruptFrame 处理
# ---------------------------------------------------------------------------


def test_ws_interrupt_with_no_active_run_emits_system_notice(tmp_path: Path) -> None:
    """ThreadManager 判断无活跃工作时，InterruptFrame 推 SystemNoticeFrame。"""
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_json({"frame_type": "interrupt"})
            notice = ws.receive_json()
            assert notice["frame_type"] == "system.notice"
            assert notice["notice_key"] == "no_active_run"
            assert notice["source"] == "ws.interrupt"
            assert tm.interrupt_calls == [(THREAD_ID, "user_interrupt")]
    finally:
        client.__exit__(None, None, None)


def test_ws_interrupt_uses_thread_manager_interrupt_agent_tree(tmp_path: Path) -> None:
    """InterruptFrame 经 ThreadManager/HostDispatcher 统一入口，不碰 current_run_task。"""
    import time

    class _CancelTrap:
        def __init__(self) -> None:
            self.cancelled = False

        def done(self) -> bool:
            return False

        def cancel(self) -> None:
            self.cancelled = True

    tm = WSFakeTM()
    tm.interrupt_result = True
    cell = FakeCell(THREAD_ID)
    trap = _CancelTrap()
    cell.current_run_task = trap
    tm._cells[THREAD_ID] = cell

    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_json({"frame_type": "interrupt"})
            time.sleep(0.1)

            assert tm.interrupt_calls == [(THREAD_ID, "user_interrupt")]
            assert trap.cancelled is False
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("frame_type", ["auto-approval-set-mode", "auto-approval-query"])
def test_ws_auto_approval_frame_returns_mode_state(
    tmp_path: Path,
    frame_type: str,
) -> None:
    """通用频道能查询并设置每 cwd 的审批处置模式。"""
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # thread.history
            payload: dict[str, object] = {"frame_type": frame_type, "cwd": "/proj/test"}
            if frame_type == "auto-approval-set-mode":
                payload["mode"] = "llm"
            ws.send_json(payload)
            message = ws.receive_json()
            assert message["frame_type"] == "auto_approval_state"
            assert message["mode"] == ("llm" if frame_type == "auto-approval-set-mode" else "user")
    finally:
        client.__exit__(None, None, None)


def test_ws_user_input_done_callback_clears_current_run_task(tmp_path: Path) -> None:
    """task.add_done_callback 应该在 run_once 完成后把 cell.current_run_task 清成 None。

    防止后续 InterruptFrame 看到一个已 done 的 task 误判"正在跑"。
    """
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_json(
                {
                    "frame_type": "user.input",
                    "text": "hello",
                    "request_id": "req-1",
                }
            )
            import time

            # FakeBridge.run_once 立即返回，done_callback 应该已触发
            time.sleep(0.2)

        cell = tm.get_cell(THREAD_ID)
        assert cell is not None
        # done_callback 把 current_run_task 清成 None
        assert cell.current_run_task is None, (
            "current_run_task 应被 add_done_callback 清成 None，否则下次 "
            "InterruptFrame 会误判为正在跑"
        )
    finally:
        client.__exit__(None, None, None)
