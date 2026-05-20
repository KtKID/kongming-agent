"""EvolutionManager 事件发射覆盖——拦住"没 emit 事件到 EventBus"类 bug。

之前 _run_review 单测只测了 build+aclose，没验证事件是否发到 EventBus。
本文件验证：
1. reviewer 成功时 emit evolution.review.started + completed
2. reviewer 失败时 emit evolution.review.started + failed
3. 事件 payload 包含必要字段（review_id / session_id / duration_ms）
4. EventBus 路由到注册的 sink
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core.contracts import Event
from evolution.evolution_manager import EvolutionManager
from evolution.models import TranscriptMessage
from tests._helpers.mock_transcript_provider import MockTranscriptProvider


@dataclass
class _EventRecorder:
    """记录收到的所有事件。"""

    events: list[Event] = field(default_factory=list)

    async def emit(self, event: Event) -> None:
        self.events.append(event)


def _cfg(tmp_path: Path) -> object:
    from config_loader import load_config

    cfg = load_config(None)
    return cfg.model_copy(
        update={
            "evolution": cfg.evolution.model_copy(
                update={
                    "learning": cfg.evolution.learning.model_copy(
                        update={
                            "enabled": True,
                            "root_path": str(tmp_path / "evo"),
                            "every_n_runs": 1,
                            "min_user_turns": 1,
                            "review_timeout_seconds": 5.0,
                        }
                    )
                }
            )
        }
    )


def _provider() -> MockTranscriptProvider:
    return MockTranscriptProvider(
        thread_id="thread-aabbccddeeff",
        _channel="mock",
        _messages=(
            TranscriptMessage(turn=1, role="user", content="question"),
            TranscriptMessage(turn=1, role="assistant", content="answer"),
        ),
    )


class TestEmitEvents:
    @pytest.mark.asyncio
    async def test_successful_review_emits_started_and_completed(self, tmp_path: Path) -> None:
        """reviewer 成功时必须 emit started + completed。"""
        manager = EvolutionManager(config=_cfg(tmp_path), kongming_home=tmp_path)

        # 注册一个 event recorder 到 EventBus
        recorder = _EventRecorder()
        manager.register_event_route("thread-aabbccddeeff", recorder)

        # mock _build_stub_parent_runtime + run_child_review 避免真调 LLM
        mock_outcome = AsyncMock()
        mock_outcome.write_ok = True
        mock_outcome.write_status = "written"
        mock_outcome.timed_out = False
        mock_outcome.duration_ms = 1000
        mock_outcome.timeout_seconds = 5.0
        mock_outcome.write_data = {"nutrients_written": 2, "written_nutrient_ids": ["a", "b"]}
        mock_outcome.write_error = None

        with patch(
            "evolution.reviewer_runtime.run_child_review",
            new_callable=AsyncMock,
            return_value=mock_outcome,
        ):
            await manager.notify_user_message(
                thread_id="thread-aabbccddeeff", provider=_provider(), cwd="/tmp"
            )
            await asyncio.sleep(0.2)

        kinds = [e.kind for e in recorder.events]
        assert "evolution.review.started" in kinds, f"missing started event, got: {kinds}"
        assert "evolution.review.completed" in kinds, f"missing completed event, got: {kinds}"

        # 检查 completed payload
        completed = next(e for e in recorder.events if e.kind == "evolution.review.completed")
        assert completed.payload["write_status"] == "written"
        assert completed.payload["duration_ms"] == 1000
        assert completed.payload["session_id"] == "thread-aabbccddeeff"
        assert "review_id" in completed.payload

        await manager.aclose()

    @pytest.mark.asyncio
    async def test_failed_review_emits_started_and_failed(self, tmp_path: Path) -> None:
        """reviewer 写入失败时必须 emit started + failed。"""
        manager = EvolutionManager(config=_cfg(tmp_path), kongming_home=tmp_path)

        recorder = _EventRecorder()
        manager.register_event_route("thread-aabbccddeeff", recorder)

        mock_outcome = AsyncMock()
        mock_outcome.write_ok = False
        mock_outcome.write_status = "tool_not_called"
        mock_outcome.timed_out = False
        mock_outcome.duration_ms = 500
        mock_outcome.timeout_seconds = 5.0
        mock_outcome.write_data = None
        mock_outcome.write_error = "evolution_write was not called"

        with patch(
            "evolution.reviewer_runtime.run_child_review",
            new_callable=AsyncMock,
            return_value=mock_outcome,
        ):
            await manager.notify_user_message(
                thread_id="thread-aabbccddeeff", provider=_provider(), cwd="/tmp"
            )
            await asyncio.sleep(0.2)

        kinds = [e.kind for e in recorder.events]
        assert "evolution.review.started" in kinds
        assert "evolution.review.failed" in kinds

        failed = next(e for e in recorder.events if e.kind == "evolution.review.failed")
        assert failed.payload["write_status"] == "tool_not_called"
        assert failed.payload["session_id"] == "thread-aabbccddeeff"

        await manager.aclose()

    @pytest.mark.asyncio
    async def test_exception_emits_started_and_failed(self, tmp_path: Path) -> None:
        """reviewer 异常时必须 emit started + failed（异常路径）。"""
        manager = EvolutionManager(config=_cfg(tmp_path), kongming_home=tmp_path)

        recorder = _EventRecorder()
        manager.register_event_route("thread-aabbccddeeff", recorder)

        with patch(
            "evolution.reviewer_runtime.run_child_review",
            new_callable=AsyncMock,
            side_effect=TimeoutError(),
        ):
            await manager.notify_user_message(
                thread_id="thread-aabbccddeeff", provider=_provider(), cwd="/tmp"
            )
            await asyncio.sleep(0.2)

        kinds = [e.kind for e in recorder.events]
        assert "evolution.review.started" in kinds
        assert "evolution.review.failed" in kinds

        failed = next(e for e in recorder.events if e.kind == "evolution.review.failed")
        assert failed.payload["error_kind"] == "TimeoutError"

        await manager.aclose()
