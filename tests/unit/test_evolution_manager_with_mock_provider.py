"""D9 频道无关性验证：用 MockTranscriptProvider 跑完整 notify 流程。

本文件 grep 不到 "claude"/"codex"/"native" 任何字样（除本注释）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from evolution.evolution_manager import EvolutionManager
from evolution.models import TranscriptMessage
from tests._helpers.mock_transcript_provider import MockTranscriptProvider


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
                        }
                    )
                }
            )
        }
    )


class TestChannelAgnostic:
    @pytest.mark.asyncio
    async def test_full_flow_with_mock_provider(self, tmp_path: Path) -> None:
        """用 MockTranscriptProvider 走 notify_user_message 全流程。

        验证 EvolutionManager 只通过 TranscriptProvider Protocol 消费数据，
        不感知任何具体频道实现。
        """
        manager = EvolutionManager(config=_cfg(tmp_path), kongming_home=tmp_path)

        provider = MockTranscriptProvider(
            thread_id="thread-aabbccddeeff",
            _channel="mock",
            _messages=(
                TranscriptMessage(turn=1, role="user", content="question 1"),
                TranscriptMessage(turn=1, role="assistant", content="answer 1"),
            ),
        )

        with patch.object(manager, "_run_review", new_callable=AsyncMock) as mock_review:
            await manager.notify_user_message(
                thread_id="thread-aabbccddeeff",
                provider=provider,
                cwd="/tmp/test",
            )
            await asyncio.sleep(0.05)

            assert mock_review.call_count == 1
            call_kwargs = mock_review.call_args.kwargs
            assert call_kwargs["thread_id"] == "thread-aabbccddeeff"
            assert "mock" in call_kwargs["run_id"]
            window = call_kwargs["window"]
            assert len(window.messages) == 2
            assert window.messages[0].content == "question 1"

        await manager.aclose()
