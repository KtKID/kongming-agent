"""EvolutionManager cadence 单测：5 case。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from core.agent_spec import AgentSpec
from core.message import Message
from core.result import Result
from core.session import InMemorySession
from evolution.evolution_manager import EvolutionManager
from evolution.models import EvolutionReviewTrigger, TranscriptMessage
from tests._helpers.mock_transcript_provider import MockTranscriptProvider


def _cfg(
    tmp_path: Path,
    *,
    enabled: bool = True,
    auto_trigger_enabled: bool = True,
    every_n: int = 5,
    min_turns: int = 3,
) -> object:
    from infrastructure.config import load_config

    cfg = load_config(None)
    return cfg.model_copy(
        update={
            "evolution": cfg.evolution.model_copy(
                update={
                    "learning": cfg.evolution.learning.model_copy(
                        update={
                            "enabled": enabled,
                            "auto_trigger_enabled": auto_trigger_enabled,
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


# ---------------------------------------------------------------------------
# runtime channel cadence（单一真源 = session manifest）
# ---------------------------------------------------------------------------

_THREAD = "thread-runtime-cadence"


@dataclass
class _FakeParentRuntime:
    """notify_runtime_run 的最小 parent runtime 替身。"""

    agent_spec: AgentSpec
    tools: dict[str, object] = field(default_factory=dict)
    event_sinks: tuple[Any, ...] = ()


def _parent_runtime() -> _FakeParentRuntime:
    return _FakeParentRuntime(
        agent_spec=AgentSpec(
            name="parent",
            instructions="",
            default_model="test-model",
            metadata={},
        ),
        tools={"evolution_write": object()},
    )


def _result_completed(run_count: int) -> Result:
    return Result(
        run_id=f"run-{_THREAD}-{run_count}",
        session_id=_THREAD,
        status="completed",
        final_message=Message.assistant("done"),
        turn_count=1,
    )


async def _session_with(run_count: int, user_turns: int) -> InMemorySession:
    """构造 manifest run_count=N、含 M 条 user 消息的 session。

    advance_run_index N 次 + append user 消息 M 次，模拟"已积累历史"的真实场景。
    """
    session = InMemorySession(session_id=_THREAD)
    for _ in range(user_turns):
        await session.append(Message.user("question"))
    for _ in range(run_count):
        await session.advance_run_index()
    return session


class TestRuntimeChannelSingleSource:
    """runtime channel cadence 必须读 session.get_run_count()，不得自维护副本。

    这组测试验证单一真源改造：cadence 判定用 session manifest 的 run_count，
    即使 evolution 刚开启（evolution.state.json 的 run_count 从 0 开始），也按
    session 真实历史轮次判定。
    """

    @pytest.mark.asyncio
    async def test_cadence_uses_session_manifest_not_self_counter(self, tmp_path: Path) -> None:
        """session run_count=6, every_n=3 → 命中（6%3==0），不依赖 state.json 计数。

        如果 cadence 仍用 EvolutionStateStore 自维护计数器（从 0 开始），第一次
        notify_runtime_run 时 state.run_count=1，1%3!=0 不命中——这就是改造前的 bug。
        """
        manager = EvolutionManager(
            config=_cfg(tmp_path, every_n=3, min_turns=3), kongming_home=tmp_path
        )
        session = await _session_with(run_count=6, user_turns=4)
        with patch.object(manager, "_run_review", new_callable=AsyncMock) as mock_review:
            await manager.notify_runtime_run(_parent_runtime(), session, _result_completed(6))
            import asyncio

            await asyncio.sleep(0.05)
            # session run_count=6, 6%3==0 → 命中
            assert mock_review.call_count == 1
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_cadence_miss_uses_session_manifest(self, tmp_path: Path) -> None:
        """session run_count=13, every_n=3 → 不命中（13%3==1）。复现目标 thread 现状。"""
        manager = EvolutionManager(
            config=_cfg(tmp_path, every_n=3, min_turns=3), kongming_home=tmp_path
        )
        session = await _session_with(run_count=13, user_turns=13)
        with patch.object(manager, "_run_review", new_callable=AsyncMock) as mock_review:
            await manager.notify_runtime_run(_parent_runtime(), session, _result_completed(13))
            import asyncio

            await asyncio.sleep(0.05)
            mock_review.assert_not_called()
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_runtime_channel_does_not_increment_state_run_count(self, tmp_path: Path) -> None:
        """runtime channel 不递增 evolution.state.json 的 run_count（双源消除核心）。

        改造前 record_parent_run 会 +1；改造后 update_session_meta 只刷 user_turn_count。
        """
        import json

        manager = EvolutionManager(
            config=_cfg(tmp_path, every_n=3, min_turns=3), kongming_home=tmp_path
        )
        session = await _session_with(run_count=5, user_turns=4)
        await manager.notify_runtime_run(_parent_runtime(), session, _result_completed(5))
        await manager.aclose()

        state_path = tmp_path / "evo" / "evolution.state.json"
        assert state_path.exists()
        with open(state_path) as f:
            state = json.load(f)
        session_state = state["sessions"][_THREAD]
        # run_count 必须保持 0（从未被 runtime channel 递增）
        assert session_state["run_count"] == 0
        # user_turn_count 旁路字段仍被记录
        assert session_state["user_turn_count"] == 4


class TestManualRuntimeReview:
    """手动请求与 runtime cadence 的合流状态机。"""

    @pytest.mark.asyncio
    async def test_manual_command_starts_child_reviewer_from_current_history(
        self,
        tmp_path: Path,
    ) -> None:
        """控制命令直接构造窗口并启动 child reviewer，不向 session 追加命令消息。"""
        manager = EvolutionManager(
            config=_cfg(tmp_path, auto_trigger_enabled=False),
            kongming_home=tmp_path,
        )
        session = InMemorySession(session_id=_THREAD)
        await session.append(Message.user("怎么修复缓存问题？"))
        await session.append(Message.assistant("清理旧缓存并重新加载。"))
        history_before = await session.history()

        with patch.object(manager, "_run_review", new_callable=AsyncMock) as mock_review:
            run_id = await manager.start_manual_command_review(
                parent_runtime=_parent_runtime(),
                session=session,
                thread_id=_THREAD,
            )
            import asyncio

            await asyncio.sleep(0.05)

        assert run_id.startswith(f"run-manual-command-{_THREAD}-")
        assert mock_review.call_count == 1
        kwargs = mock_review.call_args.kwargs
        assert kwargs["thread_id"] == _THREAD
        assert kwargs["run_id"] == run_id
        assert kwargs["trigger_reason"] is EvolutionReviewTrigger.MANUAL_COMMAND
        assert kwargs["focus"] is None
        assert kwargs["window"].session_id == _THREAD
        assert [message.content for message in kwargs["window"].messages] == [
            "怎么修复缓存问题？",
            "清理旧缓存并重新加载。",
        ]
        assert await session.history() == history_before
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_manual_command_rejects_empty_history(self, tmp_path: Path) -> None:
        """空线程没有可复盘证据时返回稳定错误，不创建 reviewer task。"""
        manager = EvolutionManager(
            config=_cfg(tmp_path, auto_trigger_enabled=False),
            kongming_home=tmp_path,
        )
        session = InMemorySession(session_id=_THREAD)

        with (
            patch.object(manager, "_run_review", new_callable=AsyncMock) as mock_review,
            pytest.raises(ValueError, match="no conversation history"),
        ):
            await manager.start_manual_command_review(
                parent_runtime=_parent_runtime(),
                session=session,
                thread_id=_THREAD,
            )

        mock_review.assert_not_called()
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_manual_command_user_tail_does_not_repeat_old_assistant(
        self,
        tmp_path: Path,
    ) -> None:
        """历史以 user 结尾时保持原序列，不把旧 assistant 追加成最终回答。"""
        manager = EvolutionManager(
            config=_cfg(tmp_path, auto_trigger_enabled=False),
            kongming_home=tmp_path,
        )
        session = InMemorySession(session_id=_THREAD)
        await session.append(Message.user("第一问"))
        await session.append(Message.assistant("第一答"))
        await session.append(Message.user("第二问"))

        with patch.object(manager, "_run_review", new_callable=AsyncMock) as mock_review:
            await manager.start_manual_command_review(
                parent_runtime=_parent_runtime(),
                session=session,
                thread_id=_THREAD,
            )
            import asyncio

            await asyncio.sleep(0.05)

        window = mock_review.call_args.kwargs["window"]
        assert [message.content for message in window.messages] == [
            "第一问",
            "第一答",
            "第二问",
        ]
        assert window.final_message is None
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_each_manual_command_starts_one_distinct_review(self, tmp_path: Path) -> None:
        """连续两次控制命令各启动一轮 reviewer，并使用不同 run id。"""
        manager = EvolutionManager(
            config=_cfg(tmp_path, auto_trigger_enabled=False),
            kongming_home=tmp_path,
        )
        session = InMemorySession(session_id=_THREAD)
        await session.append(Message.user("保留这段经验"))
        await session.append(Message.assistant("已完成当前任务"))

        with patch.object(manager, "_run_review", new_callable=AsyncMock) as mock_review:
            first_run_id = await manager.start_manual_command_review(
                parent_runtime=_parent_runtime(),
                session=session,
                thread_id=_THREAD,
            )
            second_run_id = await manager.start_manual_command_review(
                parent_runtime=_parent_runtime(),
                session=session,
                thread_id=_THREAD,
            )
            import asyncio

            await asyncio.sleep(0.05)

        assert first_run_id != second_run_id
        assert mock_review.call_count == 2
        assert {call.kwargs["run_id"] for call in mock_review.call_args_list} == {
            first_run_id,
            second_run_id,
        }
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_manual_request_bypasses_thresholds_and_keeps_final_answer(
        self,
        tmp_path: Path,
    ) -> None:
        manager = EvolutionManager(
            config=_cfg(
                tmp_path,
                auto_trigger_enabled=False,
                every_n=10,
                min_turns=10,
            ),
            kongming_home=tmp_path,
        )
        session = await _session_with(run_count=1, user_turns=1)
        result = _result_completed(1)
        await manager.queue_manual_review(
            session_id=result.session_id,
            run_id=result.run_id,
            focus="提炼错误恢复",
        )

        with patch.object(manager, "_run_review", new_callable=AsyncMock) as mock_review:
            await manager.notify_runtime_run(_parent_runtime(), session, result)
            import asyncio

            await asyncio.sleep(0.05)

        assert mock_review.call_count == 1
        kwargs = mock_review.call_args.kwargs
        assert kwargs["trigger_reason"] is EvolutionReviewTrigger.MANUAL_TOOL
        assert kwargs["focus"] == "提炼错误恢复"
        assert kwargs["window"].final_message == "done"
        assert kwargs["window"].messages[-1].content == "done"
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_manual_request_and_due_cadence_start_one_review(self, tmp_path: Path) -> None:
        manager = EvolutionManager(
            config=_cfg(tmp_path, every_n=1, min_turns=1),
            kongming_home=tmp_path,
        )
        session = await _session_with(run_count=1, user_turns=1)
        result = _result_completed(1)
        await manager.queue_manual_review(
            session_id=result.session_id,
            run_id=result.run_id,
            focus="first focus",
        )

        with patch.object(manager, "_run_review", new_callable=AsyncMock) as mock_review:
            await manager.notify_runtime_run(_parent_runtime(), session, result)
            import asyncio

            await asyncio.sleep(0.05)

        assert mock_review.call_count == 1
        assert mock_review.call_args.kwargs["trigger_reason"] is EvolutionReviewTrigger.MANUAL_TOOL
        await manager.aclose()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal_status", ["cancelled", "failed"])
    async def test_unsuccessful_run_consumes_manual_request_without_review(
        self,
        tmp_path: Path,
        terminal_status: str,
    ) -> None:
        manager = EvolutionManager(
            config=_cfg(tmp_path, auto_trigger_enabled=False, every_n=1, min_turns=1),
            kongming_home=tmp_path,
        )
        session = await _session_with(run_count=1, user_turns=1)
        completed = _result_completed(1)
        unsuccessful = Result(
            run_id=completed.run_id,
            session_id=completed.session_id,
            status=terminal_status,
            final_message=None,
            turn_count=1,
        )
        await manager.queue_manual_review(
            session_id=unsuccessful.session_id,
            run_id=unsuccessful.run_id,
            focus=None,
        )

        with patch.object(manager, "_run_review", new_callable=AsyncMock) as mock_review:
            await manager.notify_runtime_run(_parent_runtime(), session, unsuccessful)
            await manager.notify_runtime_run(_parent_runtime(), session, completed)
            import asyncio

            await asyncio.sleep(0.05)

        mock_review.assert_not_called()
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_automatic_trigger_switch_disables_both_cadence_paths(
        self,
        tmp_path: Path,
    ) -> None:
        manager = EvolutionManager(
            config=_cfg(tmp_path, auto_trigger_enabled=False, every_n=1, min_turns=1),
            kongming_home=tmp_path,
        )
        session = await _session_with(run_count=1, user_turns=1)

        with patch.object(manager, "_run_review", new_callable=AsyncMock) as mock_review:
            await manager.notify_user_message(
                thread_id=_THREAD,
                provider=_provider(_THREAD),
                cwd="/tmp",
            )
            assert not (tmp_path / "evo" / "evolution.state.json").exists()
            await manager.notify_runtime_run(
                _parent_runtime(),
                session,
                _result_completed(1),
            )
            import asyncio

            await asyncio.sleep(0.05)

        mock_review.assert_not_called()
        await manager.aclose()
