"""cron run started/finished Python↔TS wire 合同测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from hosts.web.protocol import (
    CronRunCompletedFrame,
    CronRunFinishedFrame,
    CronRunStartedFrame,
    GenericChatS2CAdapter,
)


def test_cron_run_started_adapter_dispatches_typed_frame() -> None:
    payload = {
        "frame_type": "cron.run.started",
        "timestamp_ms": 1,
        "task_id": "task-1",
        "task_name": "daily",
        "run_id": "run-1",
        "thread_id": "thread-aaaaaaaaaaaa",
        "session_id": "session-1",
        "scheduled_for": "2026-07-31T00:00:00+00:00",
        "started_at": "2026-07-31T00:00:01+00:00",
        "status": "running",
    }

    frame = GenericChatS2CAdapter.validate_python(payload)

    assert isinstance(frame, CronRunStartedFrame)
    assert frame.run_id == "run-1"


def test_cron_run_started_adapter_rejects_terminal_status() -> None:
    payload = {
        "frame_type": "cron.run.started",
        "timestamp_ms": 1,
        "task_id": "task-1",
        "task_name": "daily",
        "run_id": "run-1",
        "thread_id": "thread-aaaaaaaaaaaa",
        "session_id": "session-1",
        "scheduled_for": "2026-07-31T00:00:00+00:00",
        "started_at": "2026-07-31T00:00:01+00:00",
        "status": "completed",
    }

    with pytest.raises(ValidationError):
        GenericChatS2CAdapter.validate_python(payload)


def test_cron_run_finished_adapter_rejects_unknown_status() -> None:
    payload = {
        "frame_type": "cron.run.finished",
        "timestamp_ms": 2,
        "task_id": "task-1",
        "task_name": "daily",
        "run_id": "run-1",
        "thread_id": "thread-aaaaaaaaaaaa",
        "session_id": "session-1",
        "scheduled_for": "2026-07-31T00:00:00+00:00",
        "started_at": "2026-07-31T00:00:01+00:00",
        "finished_at": "2026-07-31T00:00:02+00:00",
        "status": "unknown",
        "final_message": None,
        "error_message": None,
        "delivery_error": None,
        "next_run_at": None,
    }

    with pytest.raises(ValidationError):
        GenericChatS2CAdapter.validate_python(payload)


def test_cron_run_finished_adapter_dispatches_typed_frame() -> None:
    frame = GenericChatS2CAdapter.validate_python(
        {
            "frame_type": "cron.run.finished",
            "timestamp_ms": 2,
            "task_id": "task-1",
            "task_name": "daily",
            "run_id": "run-1",
            "thread_id": "thread-aaaaaaaaaaaa",
            "session_id": "session-1",
            "scheduled_for": "2026-07-31T00:00:00+00:00",
            "started_at": "2026-07-31T00:00:01+00:00",
            "finished_at": "2026-07-31T00:00:02+00:00",
            "status": "cancelled",
            "final_message": None,
            "error_message": "replaced",
            "delivery_error": None,
            "next_run_at": None,
        }
    )

    assert isinstance(frame, CronRunFinishedFrame)
    assert frame.status == "cancelled"


def test_cron_run_finished_adapter_rejects_running_status() -> None:
    payload = {
        "frame_type": "cron.run.finished",
        "timestamp_ms": 2,
        "task_id": "task-1",
        "task_name": "daily",
        "run_id": "run-1",
        "thread_id": "thread-aaaaaaaaaaaa",
        "session_id": "session-1",
        "scheduled_for": "2026-07-31T00:00:00+00:00",
        "started_at": "2026-07-31T00:00:01+00:00",
        "finished_at": None,
        "status": "running",
        "final_message": None,
        "error_message": None,
        "delivery_error": None,
        "next_run_at": None,
    }

    with pytest.raises(ValidationError):
        GenericChatS2CAdapter.validate_python(payload)


def test_cron_run_completed_adapter_dispatches_typed_frame() -> None:
    frame = GenericChatS2CAdapter.validate_python(
        {
            "frame_type": "cron.run.completed",
            "timestamp_ms": 3,
            "task_id": "task-1",
            "task_name": "daily",
            "run_id": "run-1",
            "thread_id": "thread-aaaaaaaaaaaa",
            "session_id": "session-1",
            "final_message": "done",
            "delivered_at_iso": "2026-07-31T00:00:03+00:00",
            "scheduled_for": "2026-07-31T00:00:00+00:00",
            "delivery_target": "thread:thread-aaaaaaaaaaaa",
            "next_run_at": None,
            "status": "completed",
        }
    )

    assert isinstance(frame, CronRunCompletedFrame)
    assert frame.final_message == "done"


def test_typescript_protocol_contains_both_cron_run_frames_and_union_members() -> None:
    source = Path("web/src/protocol.ts").read_text(encoding="utf-8")

    assert "interface CronRunStartedFrame" in source
    assert 'frame_type: "cron.run.started"' in source
    assert "interface CronRunFinishedFrame" in source
    assert 'frame_type: "cron.run.finished"' in source
    assert "interface CronRunCompletedFrame" in source
    assert 'frame_type: "cron.run.completed"' in source
    assert "| CronRunStartedFrame" in source
    assert "| CronRunFinishedFrame" in source
    assert "| CronRunCompletedFrame" in source
