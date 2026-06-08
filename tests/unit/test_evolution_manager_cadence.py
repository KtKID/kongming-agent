"""EvolutionManager cadence 单测：5 case。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from evolution.evolution_manager import EvolutionManager
from evolution.models import TranscriptMessage
from tests._helpers.mock_transcript_provider import MockTranscriptProvider


def _cfg(tmp_path: Path, *, enabled: bool = True, every_n: int = 5, min_turns: int = 3) -> object:
    from infrastructure.config import load_config

    cfg = load_config(None)
    return cfg.model_copy(
        update={
            "evolution": cfg.evolution.model_copy(
                update={
                    "learning": cfg.evolution.learning.model_copy(
                        update={
                            "enabled": enabled,
                            "root_path": str(tmp_path / "evo"),
                            "every_n_runs": every_n,
                            "min_user_turns": min_turns,
                        }
                    )
                }
            )
        }
    )


def _provider(thread_id: str = "thread-aabbccddeeff") -> MockTranscriptProvider:
    return MockTranscriptProvider(
        thread_id=thread_id,
        _messages=(
            TranscriptMessage(turn=1, role="user", content="q1"),
            TranscriptMessage(turn=1, role="assistant", content="a1"),
        ),
    )


class TestCadence:
    @pytest.mark.asyncio
    async def test_disabled_no_state_touch(self, tmp_path: Path) -> None:
        manager = EvolutionManager(config=_cfg(tmp_path, enabled=False), kongming_home=tmp_path)
        state_file = tmp_path / "evo" / "evolution.state.json"
        await manager.notify_user_message(
            thread_id="thread-aabbccddeeff", provider=_provider(), cwd="/tmp"
        )
        assert not state_file.exists()
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_below_min_turns(self, tmp_path: Path) -> None:
        """run_count=1,2 都 < min_user_turns=3 → 不 spawn。"""
        manager = EvolutionManager(
            config=_cfg(tmp_path, min_turns=3, every_n=1), kongming_home=tmp_path
        )
        with patch("evolution.evolution_manager.EvolutionManager._run_review") as mock_review:
            for _ in range(2):
                await manager.notify_user_message(
                    thread_id="thread-aabbccddeeff", provider=_provider(), cwd="/tmp"
                )
            # 等一下后台 task（如果有的话）
            import asyncio

            await asyncio.sleep(0.05)
            mock_review.assert_not_called()
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_cadence_hit(self, tmp_path: Path) -> None:
        """run_count=5, every_n=5 → 第 5 次命中。"""
        manager = EvolutionManager(
            config=_cfg(tmp_path, every_n=5, min_turns=3), kongming_home=tmp_path
        )
        with patch.object(manager, "_run_review", new_callable=AsyncMock) as mock_review:
            for _ in range(5):
                await manager.notify_user_message(
                    thread_id="thread-aabbccddeeff", provider=_provider(), cwd="/tmp"
                )
            import asyncio

            await asyncio.sleep(0.05)
            assert mock_review.call_count == 1
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_cadence_miss(self, tmp_path: Path) -> None:
        """run_count=4, every_n=5 → 不命中。"""
        manager = EvolutionManager(
            config=_cfg(tmp_path, every_n=5, min_turns=3), kongming_home=tmp_path
        )
        with patch.object(manager, "_run_review", new_callable=AsyncMock) as mock_review:
            for _ in range(4):
                await manager.notify_user_message(
                    thread_id="thread-aabbccddeeff", provider=_provider(), cwd="/tmp"
                )
            import asyncio

            await asyncio.sleep(0.05)
            mock_review.assert_not_called()
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_cadence_double_hit(self, tmp_path: Path) -> None:
        """run_count=10, every_n=5 → 第 5 次 + 第 10 次都命中。"""
        manager = EvolutionManager(
            config=_cfg(tmp_path, every_n=5, min_turns=3), kongming_home=tmp_path
        )
        with patch.object(manager, "_run_review", new_callable=AsyncMock) as mock_review:
            for _ in range(10):
                await manager.notify_user_message(
                    thread_id="thread-aabbccddeeff", provider=_provider(), cwd="/tmp"
                )
            import asyncio

            await asyncio.sleep(0.05)
            assert mock_review.call_count == 2
        await manager.aclose()
