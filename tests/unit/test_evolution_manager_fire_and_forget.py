"""EvolutionManager fire-and-forget 单测：异常吞 + 不冒泡。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from evolution.evolution_manager import EvolutionManager
from evolution.models import TranscriptMessage
from tests._helpers.mock_transcript_provider import MockTranscriptProvider


def _cfg(tmp_path: Path) -> object:
    from infrastructure.config import load_config

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
                        }
                    )
                }
            )
        }
    )


def _provider() -> MockTranscriptProvider:
    return MockTranscriptProvider(
        thread_id="thread-aabbccddeeff",
        _messages=(
            TranscriptMessage(turn=1, role="user", content="q"),
            TranscriptMessage(turn=1, role="assistant", content="a"),
        ),
    )


class TestFireAndForget:
    @pytest.mark.asyncio
    async def test_state_store_exception_swallowed(self, tmp_path: Path) -> None:
        manager = EvolutionManager(config=_cfg(tmp_path), kongming_home=tmp_path)
        with patch.object(
            manager._state_store, "record_parent_run", side_effect=RuntimeError("disk full")
        ):
            # 不应该抛
            await manager.notify_user_message(
                thread_id="thread-aabbccddeeff", provider=_provider(), cwd="/tmp"
            )
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_provider_exception_swallowed(self, tmp_path: Path) -> None:
        class _ExplodingProvider:
            @property
            def channel_id(self) -> str:
                return "mock"

            async def build_window(self, *, run_id: str, max_messages: int) -> object:
                raise RuntimeError("jsonl corrupt")

        manager = EvolutionManager(config=_cfg(tmp_path), kongming_home=tmp_path)
        await manager.notify_user_message(
            thread_id="thread-aabbccddeeff", provider=_ExplodingProvider(), cwd="/tmp"
        )
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_reviewer_exception_swallowed(self, tmp_path: Path) -> None:
        manager = EvolutionManager(config=_cfg(tmp_path), kongming_home=tmp_path)
        with patch.object(
            manager,
            "_run_review",
            new_callable=AsyncMock,
            side_effect=RuntimeError("reviewer crash"),
        ):
            await manager.notify_user_message(
                thread_id="thread-aabbccddeeff", provider=_provider(), cwd="/tmp"
            )
            import asyncio

            await asyncio.sleep(0.05)
        await manager.aclose()
