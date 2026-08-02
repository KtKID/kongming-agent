"""pending input WebSocket 协议合同测试。"""

from __future__ import annotations

from hosts.web.protocol.ws_frames import (
    ErrorFrame,
    PendingInputCancelFrame,
    PendingInputChangedFrame,
    PendingInputDTO,
    PendingInputReorderFrame,
    PendingInputSendNowFrame,
    PendingInputSnapshotFrame,
    PendingInputStartedFrame,
    PendingInputSteeredFrame,
    PendingInputUpdateFrame,
    WSFrameC2SAdapter,
    WSFrameS2CAdapter,
)


def _pending_item() -> PendingInputDTO:
    return PendingInputDTO(
        id="pin-1",
        thread_id="thread-aaaaaaaaaaaa",
        source="user_input",
        priority="user_message",
        content="hello",
        preview="hello",
        created_at_ms=1,
        updated_at_ms=1,
        sequence=1,
        metadata={"request_id": "req-1"},
    )


def test_pending_input_c2s_frames_round_trip() -> None:
    update = WSFrameC2SAdapter.validate_python(
        {
            "frame_type": "pending-input.update",
            "pending_input_id": "pin-1",
            "content": "updated",
        }
    )
    cancel = WSFrameC2SAdapter.validate_python(
        {"frame_type": "pending-input.cancel", "pending_input_id": "pin-1"}
    )
    reorder = WSFrameC2SAdapter.validate_python(
        {
            "frame_type": "pending-input.reorder",
            "ordered_ids": ["pin-2", "pin-1"],
        }
    )
    send_now = WSFrameC2SAdapter.validate_python(
        {
            "frame_type": "pending-input.send-now",
            "pending_input_id": "pin-1",
            "request_id": "req-1",
        }
    )

    assert isinstance(update, PendingInputUpdateFrame)
    assert isinstance(cancel, PendingInputCancelFrame)
    assert isinstance(reorder, PendingInputReorderFrame)
    assert isinstance(send_now, PendingInputSendNowFrame)
    assert reorder.ordered_ids == ["pin-2", "pin-1"]
    assert send_now.request_id == "req-1"


def test_pending_input_s2c_frames_round_trip() -> None:
    item = _pending_item()
    snapshot = PendingInputSnapshotFrame(
        timestamp_ms=10,
        thread_id="thread-aaaaaaaaaaaa",
        items=[item],
        max_items=20,
        version=1,
    )
    changed = PendingInputChangedFrame(
        timestamp_ms=11,
        thread_id="thread-aaaaaaaaaaaa",
        items=[item],
        max_items=20,
        reason="added",
        version=2,
    )
    started = PendingInputStartedFrame(
        timestamp_ms=12,
        thread_id="thread-aaaaaaaaaaaa",
        pending_input_id="pin-1",
        pending_input=item.model_copy(update={"content": "updated", "preview": "updated"}),
        run_id="run-1",
        version=3,
    )
    steered = PendingInputSteeredFrame(
        timestamp_ms=13,
        thread_id="thread-aaaaaaaaaaaa",
        pending_input_id="pin-1",
        pending_input=item.model_copy(update={"content": "steered", "preview": "steered"}),
        active_run_id=None,
        version=4,
    )

    assert isinstance(
        WSFrameS2CAdapter.validate_python(snapshot.model_dump()), PendingInputSnapshotFrame
    )
    assert isinstance(
        WSFrameS2CAdapter.validate_python(changed.model_dump()), PendingInputChangedFrame
    )
    restored = WSFrameS2CAdapter.validate_python(started.model_dump())
    assert isinstance(restored, PendingInputStartedFrame)
    assert restored.pending_input.content == "updated"
    restored_steered = WSFrameS2CAdapter.validate_python(steered.model_dump())
    assert isinstance(restored_steered, PendingInputSteeredFrame)
    assert restored_steered.pending_input.content == "steered"


def test_error_frame_carries_pending_queue_reason() -> None:
    frame = ErrorFrame(
        timestamp_ms=1,
        error_code="internal",
        message="队列已满",
        reason="pending_input_queue_full",
    )
    restored = WSFrameS2CAdapter.validate_python(frame.model_dump())

    assert isinstance(restored, ErrorFrame)
    assert restored.reason == "pending_input_queue_full"
