"""D6 namespace 隔离：不同频道 thread_id 在 state.json 共存不污染。"""

from __future__ import annotations

from pathlib import Path

import pytest

from evolution.state_store import EvolutionStateStore


class TestNamespaceIsolation:
    @pytest.mark.asyncio
    async def test_two_threads_independent(self, tmp_path: Path) -> None:
        store = EvolutionStateStore(tmp_path)

        s1 = await store.record_parent_run(session_id="thread-aaaaaaaaaaaa", user_turn_count=0)
        assert s1.run_count == 1

        s2 = await store.record_parent_run(session_id="thread-bbbbbbbbbbbb", user_turn_count=0)
        assert s2.run_count == 1

        s1b = await store.record_parent_run(session_id="thread-aaaaaaaaaaaa", user_turn_count=0)
        assert s1b.run_count == 2

        s2b = await store.record_parent_run(session_id="thread-bbbbbbbbbbbb", user_turn_count=0)
        assert s2b.run_count == 2

    @pytest.mark.asyncio
    async def test_mark_review_isolated(self, tmp_path: Path) -> None:
        store = EvolutionStateStore(tmp_path)

        await store.record_parent_run(session_id="thread-aaaaaaaaaaaa", user_turn_count=0)
        await store.mark_review_result(
            session_id="thread-aaaaaaaaaaaa",
            run_id="run-mock-thread-aaaaaaaaaaaa-1",
            status="written",
        )

        s2 = await store.record_parent_run(session_id="thread-bbbbbbbbbbbb", user_turn_count=0)
        assert s2.last_review_status == "idle"
