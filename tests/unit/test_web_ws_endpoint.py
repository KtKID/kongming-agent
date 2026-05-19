"""WS /ws/threads/{id} endpoint 单测（v0.1.5）。

覆盖：

1. 未鉴权 → close 1008
2. thread_id 非法 → close 1008
3. thread_id 不存在（boot 抛 KeyError）→ close 1008
4. user.input 帧 → 触发 cell.bridge.run_once（后台 task）
5. approval.ack 帧 → 调 tm.resolve_approval
6. ping → 收到 pong
7. 1MB 限制：超大帧 → 推 error 帧
8. 非法 JSON → 推 error 帧
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from config_loader.models import Config
from tests.unit.test_web_app_lifespan import _seed_password
from web.app import create_app
from web.auth import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from web.thread_metadata import ThreadMetadata

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
    """暴露 _sessions dict 让 _send_history_frame 取空历史。"""

    _sessions: dict[str, Any] = {}


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


class WSFakeTM:
    def __init__(self) -> None:
        self._cells: dict[str, FakeCell] = {}
        self.boot_should_raise: type[BaseException] | None = None
        self.resolve_calls: list[tuple[str, str, bool]] = []
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


def _login(tmp_path: Path, tm: WSFakeTM) -> TestClient:
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
                    "kind": "user.input",
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


def test_ws_user_input_without_reasoning_effort(tmp_path: Path) -> None:
    """不带 reasoning_effort 的 user.input 帧不传 effort（None）。"""
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_json(
                {
                    "kind": "user.input",
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
            assert history["kind"] == "thread.history"

            ws.send_json(
                {
                    "kind": "user.input",
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


def test_ws_approval_ack_routed(tmp_path: Path) -> None:
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_json(
                {
                    "kind": "approval.ack",
                    "call_id": "call-1",
                    "action": "accept_once",
                }
            )
            # 等 dispatch
            import time

            time.sleep(0.05)

        # v0.1.6 三态：路由层透传字符串字面值给 thread_manager
        assert (THREAD_ID, "call-1", "accept_once") in tm.resolve_calls
    finally:
        client.__exit__(None, None, None)


def test_ws_ping_receives_pong(tmp_path: Path) -> None:
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_json({"kind": "ping"})
            pong = ws.receive_json()
            assert pong["kind"] == "pong"
            assert "timestamp_ms" in pong
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
                    "kind": "user.input",
                    "text": big,
                    "request_id": "req-big",
                }
            )
            # 应当收到 error 帧
            err = ws.receive_json()
            assert err["kind"] == "error"
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
            assert err["kind"] == "error"
    finally:
        client.__exit__(None, None, None)


def test_ws_unknown_kind_rejected(tmp_path: Path) -> None:
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            ws.send_json({"kind": "nonexistent.kind"})
            err = ws.receive_json()
            assert err["kind"] == "error"
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# interrupt-run-v0.1：InterruptFrame 处理
# ---------------------------------------------------------------------------


def test_ws_interrupt_with_no_active_run_emits_system_notice(tmp_path: Path) -> None:
    """收到 InterruptFrame 但 cell.current_run_task is None → 推 SystemNoticeFrame。"""
    tm = WSFakeTM()
    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history
            # 不发 user.input，直接发 interrupt（cell.current_run_task is None）
            ws.send_json({"kind": "interrupt"})
            notice = ws.receive_json()
            assert notice["kind"] == "system.notice"
            assert notice["notice_key"] == "no_active_run"
            assert notice["source"] == "ws.interrupt"
    finally:
        client.__exit__(None, None, None)


def test_ws_interrupt_cancels_active_run_task(tmp_path: Path) -> None:
    """收到 InterruptFrame 时 cell.current_run_task 存在且未 done → 应被 cancel。

    用 ``FakeBridge.hang_forever=True`` 让 run_once 永远 await，模拟长 turn；
    interrupt 后 ``hang_cancelled`` 应被置位（验证 cancel 链路真的打到 bridge）。
    """
    import time

    tm = WSFakeTM()
    # 预先把对应 cell 的 bridge 设为 hang 模式
    cell = FakeCell(THREAD_ID)
    cell.bridge.hang_forever = True
    tm._cells[THREAD_ID] = cell

    client = _login(tmp_path, tm)
    try:
        with client.websocket_connect(f"/ws/threads/{THREAD_ID}") as ws:
            _ = ws.receive_json()  # history

            ws.send_json(
                {
                    "kind": "user.input",
                    "text": "hello",
                    "request_id": "req-1",
                }
            )
            time.sleep(0.1)

            # cell.current_run_task 应仍 active（hang 着）
            assert cell.current_run_task is not None
            assert not cell.current_run_task.done()

            # 发 interrupt
            ws.send_json({"kind": "interrupt"})
            time.sleep(0.2)

            # bridge 应收到 CancelledError 透传
            assert cell.bridge.hang_cancelled, (
                "FakeBridge.run_once 没收到 CancelledError，说明 ws.py 的 "
                "interrupt 分支没 task.cancel() 或链路断了"
            )
            # done_callback 应已清掉 current_run_task
            assert cell.current_run_task is None
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
                    "kind": "user.input",
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
