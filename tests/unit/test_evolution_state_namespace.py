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


class TestUpdateSessionMeta:
    """update_session_meta 是 runtime channel 专用：只刷旁路字段，不递增 run_count。

    与 record_parent_run（claude channel 用，会 +1）形成对比——确保双源误差被消除。
    """

    @pytest.mark.asyncio
    async def test_does_not_increment_run_count(self, tmp_path: Path) -> None:
        """update_session_meta 多次调用，run_count 始终保持初始值（0 或既有值）。"""
        store = EvolutionStateStore(tmp_path)

        # 首次：新 session，run_count 应为 0（不递增）
        s1 = await store.update_session_meta(session_id="thread-runtime-aaa", user_turn_count=5)
        assert s1.run_count == 0
        assert s1.user_turn_count == 5

        # 再次调用：run_count 仍为 0，user_turn_count 更新
        s2 = await store.update_session_meta(session_id="thread-runtime-aaa", user_turn_count=7)
        assert s2.run_count == 0
        assert s2.user_turn_count == 7

    @pytest.mark.asyncio
    async def test_preserves_run_count_from_record_parent_run(self, tmp_path: Path) -> None:
        """update_session_meta 不覆盖 record_parent_run 已积累的 run_count。

        模拟 claude channel 先用 record_parent_run 积累 3 次，runtime channel 再
        调 update_session_meta 刷旁路字段——run_count 应保持 3 不变。
        """
        store = EvolutionStateStore(tmp_path)

        for _ in range(3):
            await store.record_parent_run(session_id="thread-mixed", user_turn_count=0)

        # runtime channel 刷旁路字段
        s = await store.update_session_meta(session_id="thread-mixed", user_turn_count=10)
        assert s.run_count == 3  # record_parent_run 的积累不被覆盖
        assert s.user_turn_count == 10
