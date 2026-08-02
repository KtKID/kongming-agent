"""ThreadManager pending input 队列状态机测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from application.agents.manager import AgentManager, SubmitMode
from core.agent_spec import AgentSpec
from core.contracts import Event, SteerRequest
from core.message import Message
from core.result import Result
from hosts.shared.host_dispatcher import HostDispatcher, SubmitReceipt
from hosts.web.app_support.host_adapter import WebHostAdapter
from hosts.web.threads.errors import ThreadPresetRefreshError
from hosts.web.threads.manager import (
    MAX_PENDING_INPUTS,
    PENDING_INPUT_NOT_INJECTABLE,
    PENDING_INPUT_QUEUE_FULL,
    ROOT_AGENT_REGISTRY_CLOSED,
    PendingInputOperationError,
    ThreadManager,
)
from infrastructure.config.models import Config


class _FakeRuntime:
    def __init__(self) -> None:
        self.aclose = AsyncMock(return_value=None)
        self._spec = AgentSpec(name="root", instructions="i", default_model="fake")
        self.calls: list[dict[str, Any]] = []
        self.steer_calls: list[dict[str, Any]] = []
        self.steer_result = True
        self.started: asyncio.Queue[str] = asyncio.Queue()
        self.releases: list[asyncio.Event] = []
        self.result_metadata_queue: list[dict[str, Any]] = []
        self.result_status_queue: list[str] = []

    @property
    def agent_spec(self) -> AgentSpec:
        return self._spec

    async def run(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        reasoning_effort: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
        event_context: dict[str, Any] | None = None,
        agent_id: str = "",
    ) -> Result:
        release = asyncio.Event()
        self.releases.append(release)
        self.calls.append(
            {
                "text": user_input,
                "session_id": session_id,
                "reasoning_effort": reasoning_effort,
                "attachments": attachments,
                "references": references,
                "event_context": event_context,
                "agent_id": agent_id,
            }
        )
        await self.started.put(user_input)
        await release.wait()
        metadata = self.result_metadata_queue.pop(0) if self.result_metadata_queue else {}
        status = self.result_status_queue.pop(0) if self.result_status_queue else "completed"
        return Result(
            run_id=f"run-{len(self.calls)}",
            session_id=session_id or "thread-aaaaaaaaaaaa",
            status=status,
            final_message=Message.assistant("ok"),
            turn_count=1,
            metadata=metadata,
        )

    def steer(self, session_id: str, request: SteerRequest) -> bool:
        self.steer_calls.append(
            {
                "session_id": session_id,
                "text": request.text,
                "pending_input_id": request.pending_input_id,
            }
        )
        return self.steer_result


class _RecordingHostDispatcher(HostDispatcher):
    """测试用 HostDispatcher，记录 submit 入口调用。"""

    def __init__(self, *, runtime: _FakeRuntime, session_id: str) -> None:
        super().__init__(runtime=runtime, session_id=session_id)  # type: ignore[arg-type]
        self.submit_calls: list[dict[str, Any]] = []

    async def submit(
        self,
        text: str,
        *,
        mode: SubmitMode = SubmitMode.QUEUE,
        attachments: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        steer_request: SteerRequest | None = None,
    ) -> SubmitReceipt:
        self.submit_calls.append(
            {
                "text": text,
                "mode": mode,
                "attachments": attachments,
                "references": references,
                "metadata": metadata,
                "steer_request": steer_request,
            }
        )
        return await super().submit(
            text,
            mode=mode,
            attachments=attachments,
            references=references,
            metadata=metadata,
            steer_request=steer_request,
        )

    async def run_text(
        self,
        user_input: str,
        *,
        reasoning_effort: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        repost_undelivered: bool = True,
    ) -> Result:
        self.submit_calls.append(
            {
                "text": user_input,
                "mode": SubmitMode.QUEUE,
                "attachments": attachments,
                "references": references,
                "metadata": metadata,
                "repost_undelivered": repost_undelivered,
            }
        )
        return await super().run_text(
            user_input,
            reasoning_effort=reasoning_effort,
            attachments=attachments,
            references=references,
            metadata=metadata,
            repost_undelivered=repost_undelivered,
        )


class _RuntimeWithoutAgentSpec:
    def __init__(self) -> None:
        self.aclose = AsyncMock(return_value=None)
        self.calls: list[dict[str, Any]] = []
        self.started: asyncio.Queue[str] = asyncio.Queue()
        self.releases: list[asyncio.Event] = []

    def steer(self, session_id: str, request: SteerRequest) -> bool:
        del session_id, request
        return False

    async def run(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        event_context: dict[str, Any] | None = None,
        agent_id: str = "",
    ) -> Result:
        release = asyncio.Event()
        self.releases.append(release)
        self.calls.append(
            {
                "text": user_input,
                "session_id": session_id,
                "event_context": event_context,
                "agent_id": agent_id,
            }
        )
        await self.started.put(user_input)
        await release.wait()
        return Result(
            run_id=f"run-{len(self.calls)}",
            session_id=session_id or "thread-aaaaaaaaaaaa",
            status="completed",
            final_message=Message.assistant("ok"),
            turn_count=1,
        )


class _CaptureWS:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.frames.append(payload)

    async def close(self) -> None:
        return None


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
                "idle_timeout_seconds": 60,
                "idle_check_interval_seconds": 10,
            },
        }
    )


async def _make_manager(tmp_path: Path) -> tuple[ThreadManager, _FakeRuntime, str]:
    runtime = _FakeRuntime()

    async def factory(
        thread_id: str,
        preset_id: str,
        adapter: WebHostAdapter,
        sinks: list[Any],
    ) -> tuple[_FakeRuntime, _RecordingHostDispatcher]:
        del preset_id, adapter, sinks
        dispatcher = _RecordingHostDispatcher(runtime=runtime, session_id=thread_id)
        return runtime, dispatcher

    manager = ThreadManager(_make_cfg(), kongming_home=tmp_path, runtime_factory=factory)
    meta = await manager.create_thread("demo", "preset-a")
    return manager, runtime, meta.id


async def _ensure_agent_manager(cell: Any) -> AgentManager:
    await cell.host_dispatcher.ensure_started()
    agent_manager = cell.host_dispatcher.agent_manager
    assert agent_manager is not None
    return agent_manager


async def test_submit_user_input_queues_when_run_is_active(tmp_path: Path) -> None:
    manager, bridge, thread_id = await _make_manager(tmp_path)

    first = await manager.submit_user_input(thread_id, "first")
    assert first.started is True
    assert await asyncio.wait_for(bridge.started.get(), timeout=1) == "first"
    assert bridge.calls[0]["event_context"] == {
        "run_epoch": 0,
        "mail_kind": "user_message",
        "mail_task_id": "",
        "conversation_id": thread_id,
    }

    second = await manager.submit_user_input(thread_id, "second")

    assert second.started is False
    assert second.pending_input is not None
    assert second.pending_input.content == "second"
    assert second.snapshot.items[0].content == "second"

    bridge.releases[0].set()
    assert await asyncio.wait_for(bridge.started.get(), timeout=1) == "second"
    assert bridge.calls[1]["event_context"] == {
        "run_epoch": 0,
        "mail_kind": "user_message",
        "mail_task_id": "",
        "conversation_id": thread_id,
    }
    bridge.releases[1].set()
    await asyncio.sleep(0)


async def test_choice_response_priority_drains_before_user_message(tmp_path: Path) -> None:
    manager, bridge, thread_id = await _make_manager(tmp_path)

    await manager.submit_user_input(thread_id, "first")
    assert await asyncio.wait_for(bridge.started.get(), timeout=1) == "first"
    await manager.submit_user_input(thread_id, "second")
    choice = await manager.submit_choice_result(
        thread_id,
        "用户已完成选择：\nrequest_id: call-1",
        request_id="call-1",
    )

    assert [item.priority for item in choice.snapshot.items] == [
        "choice_response",
        "user_message",
    ]

    bridge.releases[0].set()
    started_choice = await asyncio.wait_for(bridge.started.get(), timeout=1)
    assert started_choice.startswith("用户已完成选择")
    bridge.releases[1].set()
    assert await asyncio.wait_for(bridge.started.get(), timeout=1) == "second"
    bridge.releases[2].set()
    await asyncio.sleep(0)


async def test_reorder_pending_inputs_uses_final_drag_order(tmp_path: Path) -> None:
    manager, bridge, thread_id = await _make_manager(tmp_path)

    await manager.submit_user_input(thread_id, "running")
    assert await asyncio.wait_for(bridge.started.get(), timeout=1) == "running"

    queued = None
    for text in ["one", "two", "three", "four"]:
        queued = await manager.submit_user_input(thread_id, text)

    assert queued is not None
    ids = [item.id for item in queued.snapshot.items]
    reordered = await manager.reorder_pending_inputs(
        thread_id,
        [ids[0], ids[3], ids[1], ids[2]],
    )

    assert [item.content for item in reordered.items] == ["one", "four", "two", "three"]
    assert [item.sequence for item in reordered.items] == [1, 2, 3, 4]

    try:
        await manager.reorder_pending_inputs(thread_id, [ids[0], ids[0], ids[1], ids[2]])
    except Exception as exc:
        assert str(exc) == "ordered_ids must not contain duplicates"
    else:
        raise AssertionError("expected duplicate ordered_ids rejection")

    bridge.releases[0].set()
    for index, expected in enumerate(["one", "four", "two", "three"], start=1):
        assert await asyncio.wait_for(bridge.started.get(), timeout=1) == expected
        bridge.releases[index].set()
    await asyncio.sleep(0)


async def test_queue_full_raises_stable_reason(tmp_path: Path) -> None:
    manager, bridge, thread_id = await _make_manager(tmp_path)
    await manager.submit_user_input(thread_id, "first")
    assert await asyncio.wait_for(bridge.started.get(), timeout=1) == "first"

    for index in range(MAX_PENDING_INPUTS):
        await manager.submit_user_input(thread_id, f"queued {index}")

    try:
        await manager.submit_user_input(thread_id, "overflow")
    except Exception as exc:
        assert getattr(exc, "reason", None) == PENDING_INPUT_QUEUE_FULL
    else:
        raise AssertionError("expected pending input queue full error")

    bridge.releases[0].set()
    for index in range(MAX_PENDING_INPUTS):
        assert await asyncio.wait_for(bridge.started.get(), timeout=1) == f"queued {index}"
        bridge.releases[index + 1].set()
    await asyncio.sleep(0)


async def test_send_pending_input_now_injects_text_into_active_run(tmp_path: Path) -> None:
    manager, runtime, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    ws = _CaptureWS()
    cell.attach_ws(ws)

    await manager.submit_user_input(thread_id, "first")
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "first"
    queued = await manager.submit_user_input(thread_id, "send now")
    pending_id = queued.pending_input.id if queued.pending_input is not None else ""

    snapshot = await manager.send_pending_input_now(thread_id, pending_id)

    assert snapshot.items == []
    assert runtime.steer_calls == [
        {"session_id": thread_id, "text": "send now", "pending_input_id": pending_id},
    ]
    assert cell.host_dispatcher.submit_calls[-1]["text"] == "send now"  # type: ignore[attr-defined]
    assert cell.host_dispatcher.submit_calls[-1]["mode"] is SubmitMode.IMMEDIATE  # type: ignore[attr-defined]
    # SteerRequest 透传了 pending_input_id（消账主键）
    steer_req = cell.host_dispatcher.submit_calls[-1]["steer_request"]  # type: ignore[attr-defined]
    assert steer_req is not None
    assert steer_req.text == "send now"
    assert steer_req.pending_input_id == pending_id
    steered = [frame for frame in ws.frames if frame["frame_type"] == "pending-input.steered"]
    changed = [
        frame
        for frame in ws.frames
        if frame["frame_type"] == "pending-input.changed" and frame["reason"] == "sent_now"
    ]
    assert len(steered) == 1
    assert steered[0]["pending_input_id"] == pending_id
    assert steered[0]["pending_input"]["content"] == "send now"
    assert steered[0]["pending_input"]["status"] == "starting"
    assert steered[0]["run_id"] == ""
    assert steered[0]["turn"] is None
    assert len(changed) == 1
    assert changed[0]["items"] == []
    runtime.releases[0].set()
    await asyncio.sleep(0)


async def test_steer_injected_resends_steered_frame_for_visible_bubble(
    tmp_path: Path,
) -> None:
    manager, runtime, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    ws = _CaptureWS()
    cell.attach_ws(ws)

    await manager.submit_user_input(thread_id, "first")
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "first"
    queued = await manager.submit_user_input(thread_id, "send now")
    pending_id = queued.pending_input.id if queued.pending_input is not None else ""

    await manager.send_pending_input_now(thread_id, pending_id)
    ws.frames.clear()

    event = Event(
        kind="steer.injected",
        run_id="run-1",
        turn=3,
        payload={"pending_input_id": pending_id, "content_length": len("send now")},
    )
    for sink in cell.event_sinks:
        await sink.emit(event)

    steered = [frame for frame in ws.frames if frame["frame_type"] == "pending-input.steered"]
    assert len(steered) == 1
    assert steered[0]["pending_input_id"] == pending_id
    assert steered[0]["pending_input"]["content"] == "send now"
    assert steered[0]["active_run_id"] == "run-1"
    assert steered[0]["run_id"] == "run-1"
    assert steered[0]["turn"] == 3
    assert cell.pending_input_send_now_claims == []

    runtime.releases[0].set()
    await asyncio.sleep(0)


async def test_send_pending_input_now_requeues_undelivered_steer_before_queue(
    tmp_path: Path,
) -> None:
    manager, runtime, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    ws = _CaptureWS()
    cell.attach_ws(ws)

    await manager.submit_user_input(thread_id, "first")
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "first"
    await manager.submit_user_input(thread_id, "normal queued")
    inserted = await manager.submit_user_input(thread_id, "inserted now")
    pending_id = inserted.pending_input.id if inserted.pending_input is not None else ""

    snapshot = await manager.send_pending_input_now(thread_id, pending_id)

    assert [item.content for item in snapshot.items] == ["normal queued"]
    assert runtime.steer_calls == [
        {"session_id": thread_id, "text": "inserted now", "pending_input_id": pending_id},
    ]

    runtime.result_metadata_queue.append(
        {"steer_undelivered": [{"text": "inserted now", "pending_input_id": pending_id}]}
    )
    runtime.releases[0].set()
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "inserted now"
    submit_calls = [
        (call["text"], call["mode"])
        for call in cell.host_dispatcher.submit_calls  # type: ignore[attr-defined]
    ]
    assert submit_calls[:3] == [
        ("first", SubmitMode.QUEUE),
        ("inserted now", SubmitMode.IMMEDIATE),
        ("inserted now", SubmitMode.QUEUE),
    ]
    runtime.releases[1].set()
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "normal queued"
    submit_calls = [
        (call["text"], call["mode"])
        for call in cell.host_dispatcher.submit_calls  # type: ignore[attr-defined]
    ]
    assert ("normal queued", SubmitMode.QUEUE) in submit_calls
    runtime.releases[2].set()
    await asyncio.sleep(0)


async def test_failed_result_stops_pending_input_drain(tmp_path: Path) -> None:
    manager, runtime, thread_id = await _make_manager(tmp_path)

    await manager.submit_user_input(thread_id, "first")
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "first"
    queued = await manager.submit_user_input(thread_id, "queued after failure")
    assert queued.started is False
    assert queued.pending_input is not None

    runtime.result_status_queue.append("failed")
    runtime.releases[0].set()
    await asyncio.sleep(0.1)

    snapshot = await manager.pending_input_snapshot(thread_id)
    assert [item.content for item in snapshot.items] == ["queued after failure"]
    try:
        await asyncio.wait_for(runtime.started.get(), timeout=0.1)
    except TimeoutError:
        pass
    else:
        raise AssertionError("failed result should stop pending input drain")
    await manager.aclose_all()


async def test_send_pending_input_now_falls_back_to_queue_when_immediate_misses(
    tmp_path: Path,
) -> None:
    manager, runtime, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    runtime.steer_result = False

    await manager.submit_user_input(thread_id, "first")
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "first"
    queued = await manager.submit_user_input(thread_id, "fallback queue")
    pending_id = queued.pending_input.id if queued.pending_input is not None else ""

    snapshot = await manager.send_pending_input_now(thread_id, pending_id)

    assert snapshot.items == []
    assert runtime.steer_calls == [
        {"session_id": thread_id, "text": "fallback queue", "pending_input_id": pending_id},
    ]
    assert [call["mode"] for call in cell.host_dispatcher.submit_calls] == [  # type: ignore[attr-defined]
        SubmitMode.QUEUE,
        SubmitMode.IMMEDIATE,
    ]
    runtime.releases[0].set()
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "fallback queue"
    assert [call["mode"] for call in cell.host_dispatcher.submit_calls] == [  # type: ignore[attr-defined]
        SubmitMode.QUEUE,
        SubmitMode.IMMEDIATE,
        SubmitMode.QUEUE,
    ]
    runtime.releases[1].set()
    await asyncio.sleep(0)


async def test_send_pending_input_now_keeps_structured_input_queued(tmp_path: Path) -> None:
    manager, runtime, thread_id = await _make_manager(tmp_path)

    await manager.submit_user_input(thread_id, "first")
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "first"
    queued = await manager.submit_user_input(
        thread_id,
        "with effort",
        reasoning_effort="high",
    )
    pending_id = queued.pending_input.id if queued.pending_input is not None else ""

    try:
        await manager.send_pending_input_now(thread_id, pending_id)
    except PendingInputOperationError as exc:
        assert exc.reason == PENDING_INPUT_NOT_INJECTABLE
    else:
        raise AssertionError("expected structured pending input rejection")

    snapshot = await manager.pending_input_snapshot(thread_id)
    assert [item.id for item in snapshot.items] == [pending_id]
    assert runtime.steer_calls == []
    runtime.releases[0].set()
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "with effort"
    runtime.releases[1].set()
    await asyncio.sleep(0)


async def test_send_pending_input_now_starts_idle_mailbox_run(tmp_path: Path) -> None:
    manager, runtime, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    pending = manager._create_pending_input(
        cell,
        "idle queued",
        source="user_input",
        priority="user_message",
        metadata={},
    )
    async with cell.pending_input_lock:
        cell.pending_inputs = [pending]
        cell.pending_input_version += 1

    snapshot = await manager.send_pending_input_now(thread_id, pending.id)

    assert snapshot.items == []
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "idle queued"
    runtime.releases[0].set()
    await asyncio.sleep(0)


async def test_send_pending_input_now_treats_done_observer_as_idle_race(tmp_path: Path) -> None:
    manager, runtime, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    completed_task = asyncio.create_task(
        asyncio.sleep(
            0,
            result=Result(
                run_id="run-done",
                session_id=thread_id,
                status="completed",
                final_message=Message.assistant("ok"),
                turn_count=1,
            ),
        )
    )
    await completed_task
    cell.current_run_task = completed_task
    pending = manager._create_pending_input(
        cell,
        "race queued",
        source="user_input",
        priority="user_message",
        metadata={},
    )
    async with cell.pending_input_lock:
        cell.pending_inputs = [pending]
        cell.pending_input_version += 1

    snapshot = await manager.send_pending_input_now(thread_id, pending.id)

    assert snapshot.items == []
    assert runtime.steer_calls == []
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "race queued"
    runtime.releases[0].set()
    await asyncio.sleep(0)


async def test_send_pending_input_now_runtime_without_agent_spec_uses_dispatcher(
    tmp_path: Path,
) -> None:
    runtime = _RuntimeWithoutAgentSpec()

    async def factory(
        thread_id: str,
        preset_id: str,
        adapter: WebHostAdapter,
        sinks: list[Any],
    ) -> tuple[_RuntimeWithoutAgentSpec, _RecordingHostDispatcher]:
        del preset_id, adapter, sinks
        return runtime, _RecordingHostDispatcher(runtime=runtime, session_id=thread_id)

    manager = ThreadManager(_make_cfg(), kongming_home=tmp_path, runtime_factory=factory)
    meta = await manager.create_thread("demo", "preset-a")
    cell = await manager.boot_or_attach(meta.id)
    assert cell.host_dispatcher.agent_manager is None
    pending = manager._create_pending_input(
        cell,
        "default spec",
        source="user_input",
        priority="user_message",
        metadata={},
    )
    async with cell.pending_input_lock:
        cell.pending_inputs = [pending]
        cell.pending_input_version += 1

    snapshot = await manager.send_pending_input_now(meta.id, pending.id)

    assert snapshot.items == []
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "default spec"
    runtime.releases[0].set()
    await manager.aclose_all()


async def test_send_pending_input_now_closed_registry_keeps_item_queued(
    tmp_path: Path,
) -> None:
    manager, _runtime, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    agent_manager = await _ensure_agent_manager(cell)
    agent_manager.registry.close_registry()
    pending = manager._create_pending_input(
        cell,
        "after interrupt",
        source="user_input",
        priority="user_message",
        metadata={},
    )
    async with cell.pending_input_lock:
        cell.pending_inputs = [pending]
        cell.pending_input_version += 1

    try:
        await manager.send_pending_input_now(thread_id, pending.id)
    except PendingInputOperationError as exc:
        assert exc.reason == ROOT_AGENT_REGISTRY_CLOSED
    else:
        raise AssertionError("expected root_agent_registry_closed rejection")

    snapshot = await manager.pending_input_snapshot(thread_id)
    assert [item.id for item in snapshot.items] == [pending.id]
    assert cell.pending_input_drain_block_reason == ROOT_AGENT_REGISTRY_CLOSED
    await manager.aclose_all()


async def test_submit_user_input_after_closed_registry_stays_queued(
    tmp_path: Path,
) -> None:
    manager, runtime, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    agent_manager = await _ensure_agent_manager(cell)
    agent_manager.registry.close_registry()

    result = await manager.submit_user_input(thread_id, "after interrupt")

    assert result.started is False
    assert result.pending_input is not None
    assert result.pending_input.content == "after interrupt"
    assert cell.pending_input_drain_block_reason == ROOT_AGENT_REGISTRY_CLOSED
    try:
        await asyncio.wait_for(runtime.started.get(), timeout=0.1)
    except TimeoutError:
        pass
    else:
        raise AssertionError("closed registry should keep input queued")
    await manager.aclose_all()


async def test_done_callback_after_closed_registry_keeps_next_input_queued(
    tmp_path: Path,
) -> None:
    """run 结束 + registry closed 时：队列保留 + block reason 被清（死锁修复）。

    run-end-reason-fix 核心修复：旧实现设 ``pending_input_drain_block_reason =
    ROOT_AGENT_REGISTRY_CLOSED`` 但清除入口被 ``_has_active_run`` 门槛挡死，形成
    idle 状态下不可恢复的死锁。现在 ``_handle_pending_run_done`` 主动
    ``reset_for_reuse`` 拆树并清 block reason——队列仍保留（cancelled 不 drain），
    但不再永久焊死，用户下次发消息能正常启动。
    """
    manager, runtime, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    agent_manager = await _ensure_agent_manager(cell)
    pending = manager._create_pending_input(
        cell,
        "queued after interrupt",
        source="user_input",
        priority="user_message",
        metadata={},
    )
    async with cell.pending_input_lock:
        cell.pending_inputs = [pending]
        cell.pending_input_version += 1
    finished = asyncio.create_task(
        asyncio.sleep(
            0,
            result=Result(
                run_id="run-cancelled",
                session_id=thread_id,
                status="cancelled",
                final_message=Message.assistant("cancelled"),
                turn_count=1,
                metadata={"cancel_reason": "user_interrupt"},
            ),
        )
    )
    await finished
    cell.current_run_task = finished
    agent_manager.registry.close_registry()

    await manager._handle_pending_run_done(cell, finished, finished)

    snapshot = await manager.pending_input_snapshot(thread_id)
    # 队列保留（cancelled 不自动 drain）
    assert [item.id for item in snapshot.items] == [pending.id]
    assert cell.current_run_task is None
    # 死锁修复：block reason 被清除（不再永久焊死 ROOT_AGENT_REGISTRY_CLOSED）
    assert cell.pending_input_drain_block_reason is None
    try:
        await asyncio.wait_for(runtime.started.get(), timeout=0.1)
    except TimeoutError:
        pass
    else:
        raise AssertionError("cancelled result should not auto-drain next input")
    await manager.aclose_all()


async def test_registry_closed_deadlock_recovery_allows_new_submit(
    tmp_path: Path,
) -> None:
    """死锁修复集成验证：registry closed → reset → 用户新消息能正常启动。

    这是 bug 的核心恢复场景：旧实现在 registry closed 后永久焊死 block reason，
    用户发新消息时被 ``打断收口状态`` 提示挡住，刷新页面也无效。修复后
    ``_handle_pending_run_done`` 主动拆树清 block reason，下次 submit 正常 boot。
    """
    manager, runtime, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    agent_manager = await _ensure_agent_manager(cell)

    finished = asyncio.create_task(
        asyncio.sleep(
            0,
            result=Result(
                run_id="run-1",
                session_id=thread_id,
                status="completed",
                final_message=Message.assistant("ok"),
                turn_count=1,
            ),
        )
    )
    await finished
    cell.current_run_task = finished
    agent_manager.registry.close_registry()

    await manager._handle_pending_run_done(cell, finished, finished)

    # 死锁修复验证：block reason 被清，agent_manager 被拆（reset_for_reuse）
    assert cell.pending_input_drain_block_reason is None

    # 用户发新消息应该能正常启动（不再被 ROOT_AGENT_REGISTRY_CLOSED 挡住）
    result = await manager.submit_user_input(thread_id, "recovery message")
    assert result.started is True
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "recovery message"
    runtime.releases[-1].set()
    await manager.aclose_all()


async def test_max_turns_does_not_drain_queue(tmp_path: Path) -> None:
    """max_turns 结束后队列不自动 drain（用户确认的 drain 策略）。

    run-end-reason-fix：max_turns 走 reason=MAX_TURNS（非 COMPLETE），不自动
    消费队列下一条，把控制权交还用户。
    """
    from core.errors import MaxTurnsExceededError

    manager, runtime, thread_id = await _make_manager(tmp_path)

    await manager.submit_user_input(thread_id, "first")
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "first"
    queued = await manager.submit_user_input(thread_id, "queued after max_turns")
    assert queued.started is False
    assert queued.pending_input is not None

    # 模拟 max_turns：让 runtime.run 抛 MaxTurnsExceededError（runner 会收口成 failed）
    runtime.result_status_queue.append("failed")

    # _FakeRuntime.run 不带 error，直接 patch 让它返回带 MaxTurnsExceededError 的 failed
    # 更简单：直接构造 finished task 传给 _handle_pending_run_done
    cell = await manager.boot_or_attach(thread_id)
    finished = asyncio.create_task(
        asyncio.sleep(
            0,
            result=Result(
                run_id="run-1",
                session_id=thread_id,
                status="failed",
                final_message=None,
                turn_count=10,
                error=MaxTurnsExceededError("exceeded", details={"max_turns": 10}),
            ),
        )
    )
    await finished
    cell.current_run_task = finished
    runtime.releases[0].set()
    await asyncio.sleep(0.1)

    await manager._handle_pending_run_done(cell, finished, finished)

    snapshot = await manager.pending_input_snapshot(thread_id)
    assert [item.content for item in snapshot.items] == ["queued after max_turns"]
    # max_turns 不 drain：队列里的消息保留，等用户手动发
    try:
        await asyncio.wait_for(runtime.started.get(), timeout=0.1)
    except TimeoutError:
        pass
    else:
        raise AssertionError("max_turns should not auto-drain queue")
    await manager.aclose_all()


async def test_interrupt_agent_tree_waits_for_pending_input_lock_before_close(
    tmp_path: Path,
) -> None:
    manager, _runtime, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    agent_manager = await _ensure_agent_manager(cell)
    root_agent_id = agent_manager._root_agent_id
    assert root_agent_id is not None
    root = agent_manager.get_agent(root_agent_id)
    assert root is not None
    run_task: asyncio.Task[object] = asyncio.create_task(asyncio.sleep(10))
    root.state = "running"
    root.run_task = run_task
    agent_manager.registry.register_run(run_task, root.agent_id, None)

    await cell.pending_input_lock.acquire()
    interrupt_task = asyncio.create_task(manager.interrupt_agent_tree(thread_id))
    await asyncio.sleep(0)

    assert agent_manager.registry.is_closed is False

    cell.pending_input_lock.release()
    assert await asyncio.wait_for(interrupt_task, timeout=1) is True
    assert agent_manager.registry.is_closed is True
    assert run_task.cancelled()
    assert cell.host_dispatcher.agent_manager is None
    await manager.aclose_all()


async def test_interrupt_agent_tree_returns_false_without_active_work(tmp_path: Path) -> None:
    manager, _runtime, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)

    assert await manager.interrupt_agent_tree(thread_id) is False
    assert cell.host_dispatcher.agent_manager is None
    await manager.aclose_all()


async def test_start_root_mailbox_run_rechecks_closed_registry_with_manager(
    tmp_path: Path,
) -> None:
    manager, _runtime, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    agent_manager = await _ensure_agent_manager(cell)
    agent_manager.registry.close_registry()

    try:
        manager._start_host_dispatcher_run(
            cell,
            "late queued",
            metadata={},
            task_name="late-queued",
        )
    except PendingInputOperationError as exc:
        assert exc.reason == ROOT_AGENT_REGISTRY_CLOSED
    else:
        raise AssertionError("expected root_agent_registry_closed rejection")

    assert cell.current_run_task is None
    await manager.aclose_all()


async def test_done_but_not_cleared_run_keeps_submit_queued(tmp_path: Path) -> None:
    manager, bridge, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)

    completed_task = asyncio.create_task(
        asyncio.sleep(
            0,
            result=Result(
                run_id="run-done",
                session_id=thread_id,
                status="completed",
                final_message=Message.assistant("ok"),
                turn_count=1,
            ),
        )
    )
    await completed_task
    cell.current_run_task = completed_task
    queued = manager._create_pending_input(
        cell,
        "queued-before-drain",
        source="user_input",
        priority="user_message",
        metadata={},
    )
    cell.pending_inputs = [queued]
    cell.pending_input_version += 1

    submitted = await manager.submit_user_input(thread_id, "submitted-during-drain-window")

    assert submitted.started is False
    assert [item.content for item in submitted.snapshot.items] == [
        "queued-before-drain",
        "submitted-during-drain-window",
    ]

    await manager._handle_pending_run_done(cell, completed_task, completed_task)

    assert await asyncio.wait_for(bridge.started.get(), timeout=1) == "queued-before-drain"
    try:
        await asyncio.wait_for(bridge.started.get(), timeout=0.1)
    except TimeoutError:
        pass
    else:
        raise AssertionError("new submission should wait for the drained run")

    bridge.releases[0].set()
    assert await asyncio.wait_for(bridge.started.get(), timeout=1) == (
        "submitted-during-drain-window"
    )
    bridge.releases[1].set()
    await asyncio.sleep(0)


async def test_submit_with_pending_queue_starts_existing_item_first(tmp_path: Path) -> None:
    manager, bridge, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    queued = manager._create_pending_input(
        cell,
        "queued-before-submit",
        source="user_input",
        priority="user_message",
        metadata={},
    )
    cell.pending_inputs = [queued]
    cell.pending_input_version += 1

    submitted = await manager.submit_user_input(thread_id, "new-submit")

    assert submitted.started is False
    assert [item.content for item in submitted.snapshot.items] == ["new-submit"]
    assert await asyncio.wait_for(bridge.started.get(), timeout=1) == "queued-before-submit"
    try:
        await asyncio.wait_for(bridge.started.get(), timeout=0.1)
    except TimeoutError:
        pass
    else:
        raise AssertionError("new submission should wait behind existing pending input")

    bridge.releases[0].set()
    assert await asyncio.wait_for(bridge.started.get(), timeout=1) == "new-submit"
    bridge.releases[1].set()
    await asyncio.sleep(0)


async def test_submit_respects_pending_input_drain_block_reason(tmp_path: Path) -> None:
    manager, bridge, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    queued = manager._create_pending_input(
        cell,
        "queued-before-block",
        source="user_input",
        priority="user_message",
        metadata={},
    )
    cell.pending_inputs = [queued]
    cell.pending_input_version += 1
    cell.pending_input_drain_block_reason = "runtime_refresh_failed"

    submitted = await manager.submit_user_input(thread_id, "new-submit")

    assert submitted.started is False
    assert [item.content for item in submitted.snapshot.items] == [
        "queued-before-block",
        "new-submit",
    ]
    try:
        await asyncio.wait_for(bridge.started.get(), timeout=0.1)
    except TimeoutError:
        pass
    else:
        raise AssertionError("drain block should keep all pending input queued")


async def test_evict_skips_pending_input_drain(tmp_path: Path) -> None:
    manager, bridge, thread_id = await _make_manager(tmp_path)
    await manager.submit_user_input(thread_id, "first")
    assert await asyncio.wait_for(bridge.started.get(), timeout=1) == "first"
    await manager.submit_user_input(thread_id, "second")

    await manager.evict_cell(thread_id, reason="manual_stop", notify_ws=False)

    try:
        await asyncio.wait_for(bridge.started.get(), timeout=0.1)
    except TimeoutError:
        pass
    else:
        raise AssertionError("pending input should not drain after evict")


async def test_runtime_refresh_failure_sets_drain_block_reason(tmp_path: Path) -> None:
    runtime = _FakeRuntime()
    calls = 0

    async def factory(
        thread_id: str,
        preset_id: str,
        adapter: WebHostAdapter,
        sinks: list[Any],
    ) -> tuple[_FakeRuntime, _RecordingHostDispatcher]:
        nonlocal calls
        del preset_id, adapter, sinks
        calls += 1
        if calls > 1:
            raise RuntimeError("refresh failed")
        return runtime, _RecordingHostDispatcher(runtime=runtime, session_id=thread_id)

    manager = ThreadManager(_make_cfg(), kongming_home=tmp_path, runtime_factory=factory)
    meta = await manager.create_thread("demo", "preset-a")
    cell = await manager.boot_or_attach(meta.id)
    cell.metadata = cell.metadata.model_copy(update={"preset_id": "preset-b"})

    refreshed = await manager.ensure_cell_runtime_preset_current(meta.id)

    assert refreshed is False
    assert cell.pending_input_drain_block_reason == "runtime_refresh_failed"


async def test_preset_refresh_failure_rollback_allows_future_queue_drain(
    tmp_path: Path,
) -> None:
    runtime = _FakeRuntime()
    calls = 0

    async def factory(
        thread_id: str,
        preset_id: str,
        adapter: WebHostAdapter,
        sinks: list[Any],
    ) -> tuple[_FakeRuntime, _RecordingHostDispatcher]:
        nonlocal calls
        del preset_id, adapter, sinks
        calls += 1
        if calls > 1:
            raise RuntimeError("refresh failed")
        return runtime, _RecordingHostDispatcher(runtime=runtime, session_id=thread_id)

    manager = ThreadManager(_make_cfg(), kongming_home=tmp_path, runtime_factory=factory)
    meta = await manager.create_thread("demo", "preset-a")
    cell = await manager.boot_or_attach(meta.id)

    try:
        await manager.update_thread_preset(meta.id, "preset-b")
    except ThreadPresetRefreshError:
        pass
    else:
        raise AssertionError("expected preset refresh failure")

    assert cell.metadata.preset_id == "preset-a"
    assert cell.pending_input_drain_block_reason is None

    await manager.submit_user_input(meta.id, "first")
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "first"
    queued = await manager.submit_user_input(meta.id, "second")
    assert queued.started is False

    runtime.releases[0].set()
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "second"
    runtime.releases[1].set()
    await asyncio.sleep(0)


async def test_interrupt_keeps_pending_queue_without_auto_drain(tmp_path: Path) -> None:
    """user_interrupt 后队列原样保留，不自动起跑下一条。

    守护 drain 白名单契约：只有 normal 继续 drain，user_interrupt 不消费队列。
    用户显式重新发送才能触发下一条 run（见 test_interrupt_allows_manual_resume）。
    """
    manager, runtime, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    # 队列里预置两条待发消息（current_run_task 仍为 None，模拟刚被 interrupt 清空的稳态）
    pending_a = manager._create_pending_input(
        cell,
        "queued-a",
        source="user_input",
        priority="user_message",
        metadata={},
    )
    pending_b = manager._create_pending_input(
        cell,
        "queued-b",
        source="user_input",
        priority="user_message",
        metadata={},
    )
    async with cell.pending_input_lock:
        cell.pending_inputs = [pending_a, pending_b]
        cell.pending_input_version += 1
    # 构造一个已完成的 cancelled run（cancel_reason=user_interrupt）
    finished = asyncio.create_task(
        asyncio.sleep(
            0,
            result=Result(
                run_id="run-interrupted",
                session_id=thread_id,
                status="cancelled",
                final_message=Message.assistant("interrupted"),
                turn_count=1,
                metadata={"cancel_reason": "user_interrupt"},
            ),
        )
    )
    await finished
    cell.current_run_task = finished

    await manager._handle_pending_run_done(cell, finished, finished)

    # 队列两条消息原样保留，顺序不变
    snapshot = await manager.pending_input_snapshot(thread_id)
    assert [item.id for item in snapshot.items] == [pending_a.id, pending_b.id]
    assert snapshot.items[0].content == "queued-a"
    assert snapshot.items[1].content == "queued-b"
    # current_run_task 已清空，但没起跑新 run
    assert cell.current_run_task is None
    # runtime 从未被调用——证明没有自动 drain
    try:
        await asyncio.wait_for(runtime.started.get(), timeout=0.1)
    except TimeoutError:
        pass
    else:
        raise AssertionError("user_interrupt must not auto-drain the next pending input")
    await manager.aclose_all()


async def test_interrupt_then_normal_drains_next(tmp_path: Path) -> None:
    """对比：normal 完成后仍正常 drain，确认改动只影响 user_interrupt 分支。

    防止把白名单改窄时误伤 normal → next 的自动连跑契约。
    """
    manager, runtime, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    pending_next = manager._create_pending_input(
        cell,
        "next-after-normal",
        source="user_input",
        priority="user_message",
        metadata={},
    )
    async with cell.pending_input_lock:
        cell.pending_inputs = [pending_next]
        cell.pending_input_version += 1
    # 构造一个已完成的 normal run
    finished = asyncio.create_task(
        asyncio.sleep(
            0,
            result=Result(
                run_id="run-ok",
                session_id=thread_id,
                status="completed",
                final_message=Message.assistant("ok"),
                turn_count=1,
                metadata={},
            ),
        )
    )
    await finished
    cell.current_run_task = finished

    await manager._handle_pending_run_done(cell, finished, finished)

    # normal 完成 → 自动起跑队首
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "next-after-normal"
    # 队列已 pop
    snapshot = await manager.pending_input_snapshot(thread_id)
    assert snapshot.items == []
    await manager.aclose_all()


async def test_interrupt_allows_manual_resume(tmp_path: Path) -> None:
    """interrupt 后用户显式重新发送能正常起跑（验证 cell 恢复可用，无卡死）。

    守护「不自动 drain」≠「cell 损坏」：队列保留 + 手动发送仍能触发 run。
    """
    manager, runtime, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    # 模拟刚被 interrupt 后的稳态：队列留一条，current_run_task=None
    pending = manager._create_pending_input(
        cell,
        "queued-after-interrupt",
        source="user_input",
        priority="user_message",
        metadata={},
    )
    async with cell.pending_input_lock:
        cell.pending_inputs = [pending]
        cell.pending_input_version += 1
    assert cell.current_run_task is None

    # 用户显式发送一条新消息
    await manager.submit_user_input(thread_id, "manual-resume")

    # 应当起跑（队列里原本那一条按 FIFO 先跑，manual-resume 排后）
    first = await asyncio.wait_for(runtime.started.get(), timeout=1)
    assert first == "queued-after-interrupt"
    await manager.aclose_all()


# ---------------------------------------------------------------------------
# 负向测试：pending_input_id 精确消账（覆盖原 content_length bug 复现场景）
# ---------------------------------------------------------------------------


async def test_concurrent_same_length_send_now_matches_by_id_not_length(
    tmp_path: Path,
) -> None:
    """两条等长 send-now + 事件乱序到达，每条精确匹配自己的 claim（原 bug 复现场景）。

    原 bug：steer.injected 只带 content_length，两条等长内容会用 pop(0) 盲弹，
    导致第二条事件先到时把第一条 claim 弹出，UI 标记错气泡。修复后按 id 精确匹配。
    """
    import logging as _logging

    manager, runtime, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    ws = _CaptureWS()
    cell.attach_ws(ws)

    await manager.submit_user_input(thread_id, "first")
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "first"
    # 两条等长内容（都是 5 个字符）——原 bug 正是靠同长度撞车
    queued_a = await manager.submit_user_input(thread_id, "AAAAA")
    queued_b = await manager.submit_user_input(thread_id, "BBBBB")
    pid_a = queued_a.pending_input.id if queued_a.pending_input is not None else ""
    pid_b = queued_b.pending_input.id if queued_b.pending_input is not None else ""

    await manager.send_pending_input_now(thread_id, pid_a)
    await manager.send_pending_input_now(thread_id, pid_b)
    ws.frames.clear()

    # 关键：第二条的 steer.injected 事件先到（乱序）
    event_b = Event(
        kind="steer.injected",
        run_id="run-1",
        turn=1,
        payload={"pending_input_id": pid_b, "content_length": 5},
    )
    for sink in cell.event_sinks:
        await sink.emit(event_b)

    steered = [f for f in ws.frames if f["frame_type"] == "pending-input.steered"]
    assert len(steered) == 1
    # 必须精确匹配到 pid_b，而不是盲弹 pop(0) 命中 pid_a
    assert steered[0]["pending_input_id"] == pid_b
    assert steered[0]["pending_input"]["content"] == "BBBBB"

    # 第一条事件后到，仍能精确匹配到 pid_a
    event_a = Event(
        kind="steer.injected",
        run_id="run-1",
        turn=2,
        payload={"pending_input_id": pid_a, "content_length": 5},
    )
    for sink in cell.event_sinks:
        await sink.emit(event_a)

    steered_all = [f for f in ws.frames if f["frame_type"] == "pending-input.steered"]
    assert len(steered_all) == 2
    assert steered_all[1]["pending_input_id"] == pid_a
    assert steered_all[1]["pending_input"]["content"] == "AAAAA"

    runtime.releases[0].set()
    await manager.aclose_all()

    # 确保上面没有意外打出 error 日志（精确匹配不应触发）
    _ = _logging.getLogger("hosts.web.threads.manager")


async def test_steer_injected_missing_pending_input_id_logs_error_and_skips(
    tmp_path: Path,
    caplog: Any,
) -> None:
    """steer.injected 事件缺 pending_input_id → 记 error 日志、不弹任何 claim。"""
    import logging as _logging

    manager, runtime, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    ws = _CaptureWS()
    cell.attach_ws(ws)

    await manager.submit_user_input(thread_id, "first")
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "first"
    queued = await manager.submit_user_input(thread_id, "send now")
    pending_id = queued.pending_input.id if queued.pending_input is not None else ""

    await manager.send_pending_input_now(thread_id, pending_id)
    ws.frames.clear()

    # 事件 payload 缺 pending_input_id（模拟旧路径或外部直调）
    event = Event(
        kind="steer.injected",
        run_id="run-1",
        turn=1,
        payload={"content_length": len("send now")},
    )
    with caplog.at_level(_logging.ERROR, logger="hosts.web.threads.manager"):
        for sink in cell.event_sinks:
            await sink.emit(event)

    # 没有 steered 帧（不消账）
    steered = [f for f in ws.frames if f["frame_type"] == "pending-input.steered"]
    assert steered == []
    # claim 仍在（未被弹出）
    assert len(cell.pending_input_send_now_claims) == 1
    # 记了 error 日志
    assert any(
        "missing pending_input_id" in record.getMessage()
        for record in caplog.records
        if record.levelno >= _logging.ERROR
    )

    runtime.releases[0].set()
    await manager.aclose_all()


async def test_steer_injected_unknown_id_logs_error_and_skips(tmp_path: Path) -> None:
    """steer.injected 的 pending_input_id 在 claims 里找不到 → 记 error、不弹。"""
    import logging as _logging

    manager, runtime, thread_id = await _make_manager(tmp_path)
    cell = await manager.boot_or_attach(thread_id)
    ws = _CaptureWS()
    cell.attach_ws(ws)

    await manager.submit_user_input(thread_id, "first")
    assert await asyncio.wait_for(runtime.started.get(), timeout=1) == "first"
    queued = await manager.submit_user_input(thread_id, "send now")
    pending_id = queued.pending_input.id if queued.pending_input is not None else ""

    await manager.send_pending_input_now(thread_id, pending_id)
    ws.frames.clear()

    # 事件带的 id 在 claims 里不存在
    event = Event(
        kind="steer.injected",
        run_id="run-1",
        turn=1,
        payload={"pending_input_id": "pin-nonexistent", "content_length": 99},
    )
    handler_records: list[Any] = []
    handler = _logging.Handler()
    handler.emit = lambda record: handler_records.append(record)  # type: ignore[assignment]
    logger = _logging.getLogger("hosts.web.threads.manager")
    logger.addHandler(handler)
    try:
        for sink in cell.event_sinks:
            await sink.emit(event)
    finally:
        logger.removeHandler(handler)

    # 没有 steered 帧
    steered = [f for f in ws.frames if f["frame_type"] == "pending-input.steered"]
    assert steered == []
    # claim 仍在（不盲弹 pop(0)）
    assert len(cell.pending_input_send_now_claims) == 1
    # 记了 error 日志（id 找不到）
    assert any(
        "not found in" in record.getMessage() and record.levelno >= _logging.ERROR
        for record in handler_records
    )

    runtime.releases[0].set()
    await manager.aclose_all()
