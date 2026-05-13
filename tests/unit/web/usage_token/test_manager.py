"""测试 ``UsageTokenManager`` 6 个公共方法。

每个方法 ≥3 测试用例（含正常路径 + 边界 + 错误）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.unit.web.usage_token._fixtures import (
    InMemoryThreadMetadataIO,
    anthropic_raw,
    openai_raw,
)
from web.usage_token import UsageTokenManager

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def io() -> InMemoryThreadMetadataIO:
    return InMemoryThreadMetadataIO()


@pytest.fixture
def manager(tmp_path: Path, io: InMemoryThreadMetadataIO) -> UsageTokenManager:
    return UsageTokenManager(
        home=tmp_path,
        thread_metadata_io=io,
        model_context_windows={
            "claude-opus-4": 1_000_000,
            "claude-sonnet-4": 200_000,
            "gpt-4o": 128_000,
            "gemma-4-e4b-it": 8_192,
        },
    )


THREAD_ID = "thread-aaaaaaaaaaaa"


# =============================================================================
# record_run_usage
# =============================================================================


class TestRecordRunUsage:
    @pytest.mark.asyncio
    async def test_anthropic_first_run_writes_cumulative(
        self, manager: UsageTokenManager, io: InMemoryThreadMetadataIO
    ) -> None:
        snap = await manager.record_run_usage(
            THREAD_ID,
            channel="anthropic",
            raw_payload=anthropic_raw(
                input_tokens=100,
                cache_read_input_tokens=80,
                cache_creation_input_tokens=10,
                output_tokens=20,
            ),
            turn=1,
            run_id="run-1",
        )
        assert snap.channel == "anthropic"
        assert snap.input_tokens == 100
        assert snap.output_tokens == 20
        assert snap.extras == {
            "cache_read_input_tokens": 80,
            "cache_creation_input_tokens": 10,
        }
        # context_usage 派生：input + cache_read + cache_creation
        assert snap.context_usage == 100 + 80 + 10
        # 落盘
        assert io.store[THREAD_ID] == {
            "channel": "anthropic",
            "input_tokens": 100,
            "cache_read_input_tokens": 80,
            "cache_creation_input_tokens": 10,
            "output_tokens": 20,
        }

    @pytest.mark.asyncio
    async def test_openai_first_run_writes_cumulative(
        self, manager: UsageTokenManager, io: InMemoryThreadMetadataIO
    ) -> None:
        snap = await manager.record_run_usage(
            THREAD_ID,
            channel="openai",
            raw_payload=openai_raw(
                input_tokens=100,
                cached_input_tokens=80,
                output_tokens=20,
                reasoning_output_tokens=15,
            ),
            turn=1,
            run_id="run-1",
        )
        assert snap.channel == "openai"
        assert snap.context_usage == 100  # OpenAI 已含 cache，不加
        assert io.store[THREAD_ID]["channel"] == "openai"
        assert io.store[THREAD_ID]["cached_input_tokens"] == 80
        assert io.store[THREAD_ID]["reasoning_output_tokens"] == 15

    @pytest.mark.asyncio
    async def test_cumulative_accumulates_across_turns(
        self, manager: UsageTokenManager, io: InMemoryThreadMetadataIO
    ) -> None:
        await manager.record_run_usage(
            THREAD_ID,
            channel="anthropic",
            raw_payload=anthropic_raw(input_tokens=100, output_tokens=20),
            turn=1,
            run_id="run-1",
        )
        await manager.record_run_usage(
            THREAD_ID,
            channel="anthropic",
            raw_payload=anthropic_raw(input_tokens=50, output_tokens=10),
            turn=2,
            run_id="run-2",
        )
        # 累计 = 100+50, 20+10
        assert io.store[THREAD_ID]["input_tokens"] == 150
        assert io.store[THREAD_ID]["output_tokens"] == 30

    @pytest.mark.asyncio
    async def test_unknown_channel_raises(self, manager: UsageTokenManager) -> None:
        with pytest.raises(ValueError, match="unknown channel"):
            await manager.record_run_usage(
                THREAD_ID,
                channel="google",  # type: ignore[arg-type]
                raw_payload={},
                turn=1,
                run_id="run-1",
            )

    @pytest.mark.asyncio
    async def test_channel_mismatch_resets_cumulative(
        self, manager: UsageTokenManager, io: InMemoryThreadMetadataIO
    ) -> None:
        """已有 anthropic 累计，本轮提交 openai：视为异常，重置为 openai 起点。"""
        # 先写 anthropic
        await manager.record_run_usage(
            THREAD_ID,
            channel="anthropic",
            raw_payload=anthropic_raw(input_tokens=100, output_tokens=20),
            turn=1,
            run_id="run-1",
        )
        # 再写 openai（不应该发生，但管理器要容错）
        snap = await manager.record_run_usage(
            THREAD_ID,
            channel="openai",
            raw_payload=openai_raw(input_tokens=50, output_tokens=10),
            turn=2,
            run_id="run-2",
        )
        assert snap.channel == "openai"
        assert io.store[THREAD_ID]["channel"] == "openai"
        assert io.store[THREAD_ID]["input_tokens"] == 50  # 重置后

    @pytest.mark.asyncio
    async def test_concurrent_records_same_thread_no_race(
        self, manager: UsageTokenManager, io: InMemoryThreadMetadataIO
    ) -> None:
        """并发 record 同一 thread：per-thread Lock 保证累加正确。"""
        # 启 10 个并发 record，各加 10 input
        await asyncio.gather(
            *[
                manager.record_run_usage(
                    THREAD_ID,
                    channel="anthropic",
                    raw_payload=anthropic_raw(input_tokens=10, output_tokens=1),
                    turn=i,
                    run_id=f"run-{i}",
                )
                for i in range(10)
            ]
        )
        # 累计 = 10 * 10 = 100（无竞争丢失）
        assert io.store[THREAD_ID]["input_tokens"] == 100
        assert io.store[THREAD_ID]["output_tokens"] == 10


# =============================================================================
# get_thread_summary
# =============================================================================


class TestGetThreadSummary:
    @pytest.mark.asyncio
    async def test_empty_thread_returns_zero_summary(self, manager: UsageTokenManager) -> None:
        summary = await manager.get_thread_summary("thread-fresh-aaaaa")
        assert summary.cumulative_input_tokens == 0
        assert summary.cumulative_output_tokens == 0
        assert summary.context_usage_pct is None

    @pytest.mark.asyncio
    async def test_anthropic_thread_returns_anthropic_extras(
        self, manager: UsageTokenManager
    ) -> None:
        await manager.record_run_usage(
            THREAD_ID,
            channel="anthropic",
            raw_payload=anthropic_raw(
                input_tokens=100, cache_read_input_tokens=80, output_tokens=20
            ),
            turn=1,
            run_id="run-1",
        )
        summary = await manager.get_thread_summary(THREAD_ID)
        assert summary.channel == "anthropic"
        assert summary.cumulative_input_tokens == 100
        assert summary.cumulative_output_tokens == 20
        assert summary.extras["cache_read_input_tokens"] == 80
        assert summary.extras["cache_creation_input_tokens"] == 0
        # last_run_context_usage = input + cache_read + cache_creation = 100+80+0
        assert summary.last_run_context_usage == 180

    @pytest.mark.asyncio
    async def test_openai_thread_returns_openai_extras(self, manager: UsageTokenManager) -> None:
        await manager.record_run_usage(
            THREAD_ID,
            channel="openai",
            raw_payload=openai_raw(
                input_tokens=100, cached_input_tokens=80, reasoning_output_tokens=10
            ),
            turn=1,
            run_id="run-1",
        )
        summary = await manager.get_thread_summary(THREAD_ID)
        assert summary.channel == "openai"
        assert summary.extras["cached_input_tokens"] == 80
        assert summary.extras["reasoning_output_tokens"] == 10
        assert summary.last_run_context_usage == 100  # OpenAI context = input

    @pytest.mark.asyncio
    async def test_context_usage_pct_when_model_set(self, manager: UsageTokenManager) -> None:
        manager.set_thread_model(THREAD_ID, "claude-opus-4")
        await manager.record_run_usage(
            THREAD_ID,
            channel="anthropic",
            raw_payload=anthropic_raw(
                input_tokens=100_000, cache_read_input_tokens=400_000, output_tokens=200
            ),
            turn=1,
            run_id="run-1",
        )
        summary = await manager.get_thread_summary(THREAD_ID)
        assert summary.model_name == "claude-opus-4"
        assert summary.model_context_window == 1_000_000
        # context = 100K + 400K = 500K, pct = 50.0
        assert summary.context_usage_pct == 50.0


# =============================================================================
# get_last_run_snapshot
# =============================================================================


class TestGetLastRunSnapshot:
    @pytest.mark.asyncio
    async def test_returns_none_before_any_record(self, manager: UsageTokenManager) -> None:
        assert await manager.get_last_run_snapshot("thread-unknown-aa") is None

    @pytest.mark.asyncio
    async def test_returns_last_snapshot_after_record(self, manager: UsageTokenManager) -> None:
        await manager.record_run_usage(
            THREAD_ID,
            channel="anthropic",
            raw_payload=anthropic_raw(input_tokens=100, output_tokens=20),
            turn=1,
            run_id="run-1",
        )
        snap = await manager.get_last_run_snapshot(THREAD_ID)
        assert snap is not None
        assert snap.input_tokens == 100
        assert snap.turn == 1
        assert snap.run_id == "run-1"

    @pytest.mark.asyncio
    async def test_returns_latest_when_multiple_records(self, manager: UsageTokenManager) -> None:
        await manager.record_run_usage(
            THREAD_ID,
            channel="anthropic",
            raw_payload=anthropic_raw(input_tokens=100, output_tokens=20),
            turn=1,
            run_id="run-1",
        )
        await manager.record_run_usage(
            THREAD_ID,
            channel="anthropic",
            raw_payload=anthropic_raw(input_tokens=50, output_tokens=10),
            turn=2,
            run_id="run-2",
        )
        snap = await manager.get_last_run_snapshot(THREAD_ID)
        assert snap is not None
        # snapshot 是"本轮"的 delta，不是累计
        assert snap.input_tokens == 50
        assert snap.turn == 2


# =============================================================================
# to_ws_frame
# =============================================================================


class TestToWsFrame:
    @pytest.mark.asyncio
    async def test_no_snapshot_raises(self, manager: UsageTokenManager) -> None:
        with pytest.raises(KeyError, match="no snapshot"):
            await manager.to_ws_frame("thread-fresh-aa", turn=1, run_id="run-1")

    @pytest.mark.asyncio
    async def test_frame_kind_is_usage(self, manager: UsageTokenManager) -> None:
        await manager.record_run_usage(
            THREAD_ID,
            channel="anthropic",
            raw_payload=anthropic_raw(input_tokens=100, output_tokens=20),
            turn=1,
            run_id="run-1",
        )
        frame = await manager.to_ws_frame(THREAD_ID, turn=1, run_id="run-1")
        assert frame.kind == "usage"
        assert frame.turn == 1
        assert frame.run_id == "run-1"

    @pytest.mark.asyncio
    async def test_frame_embeds_last_snapshot(self, manager: UsageTokenManager) -> None:
        await manager.record_run_usage(
            THREAD_ID,
            channel="anthropic",
            raw_payload=anthropic_raw(input_tokens=100, output_tokens=20),
            turn=1,
            run_id="run-1",
        )
        frame = await manager.to_ws_frame(THREAD_ID, turn=1, run_id="run-1")
        assert frame.snapshot.input_tokens == 100
        assert frame.snapshot.output_tokens == 20


# =============================================================================
# context_window
# =============================================================================


class TestContextWindow:
    def test_exact_match(self, manager: UsageTokenManager) -> None:
        assert manager.context_window("claude-opus-4") == 1_000_000

    def test_prefix_match(self, manager: UsageTokenManager) -> None:
        # "claude-opus-4-20250514" → 前缀 "claude-opus-4" → 1M
        assert manager.context_window("claude-opus-4-20250514") == 1_000_000

    def test_unknown_returns_none(self, manager: UsageTokenManager) -> None:
        assert manager.context_window("unknown-model") is None

    def test_empty_string_returns_none(self, manager: UsageTokenManager) -> None:
        assert manager.context_window("") is None

    def test_prefix_match_longest_wins(self, tmp_path: Path) -> None:
        """两个 prefix 都能匹配时，取最长那个。"""
        mgr = UsageTokenManager(
            home=tmp_path,
            thread_metadata_io=InMemoryThreadMetadataIO(),
            model_context_windows={
                "claude": 200_000,
                "claude-opus-4": 1_000_000,
            },
        )
        # "claude-opus-4-20250514" 跟 "claude-opus-4" 和 "claude" 都匹配；取长的
        assert mgr.context_window("claude-opus-4-20250514") == 1_000_000


# =============================================================================
# context_usage_pct
# =============================================================================


class TestContextUsagePct:
    @pytest.mark.asyncio
    async def test_returns_none_without_snapshot(self, manager: UsageTokenManager) -> None:
        manager.set_thread_model(THREAD_ID, "claude-opus-4")
        # 没 record 过
        assert manager.context_usage_pct(THREAD_ID) is None

    @pytest.mark.asyncio
    async def test_returns_none_without_model(self, manager: UsageTokenManager) -> None:
        await manager.record_run_usage(
            THREAD_ID,
            channel="anthropic",
            raw_payload=anthropic_raw(input_tokens=100, output_tokens=20),
            turn=1,
            run_id="run-1",
        )
        # 没设 model
        assert manager.context_usage_pct(THREAD_ID) is None

    @pytest.mark.asyncio
    async def test_correct_percentage(self, manager: UsageTokenManager) -> None:
        manager.set_thread_model(THREAD_ID, "claude-opus-4")  # 1M
        await manager.record_run_usage(
            THREAD_ID,
            channel="anthropic",
            raw_payload=anthropic_raw(
                input_tokens=100_000, cache_read_input_tokens=400_000, output_tokens=200
            ),
            turn=1,
            run_id="run-1",
        )
        # context = 500K / 1M = 50.0%
        assert manager.context_usage_pct(THREAD_ID) == 50.0

    @pytest.mark.asyncio
    async def test_returns_none_when_model_unknown(self, manager: UsageTokenManager) -> None:
        manager.set_thread_model(THREAD_ID, "unknown-model")
        await manager.record_run_usage(
            THREAD_ID,
            channel="anthropic",
            raw_payload=anthropic_raw(input_tokens=100, output_tokens=20),
            turn=1,
            run_id="run-1",
        )
        assert manager.context_usage_pct(THREAD_ID) is None
