"""unit:evolution 触发链错误分类器——全部阻断路径的日志分类与级别。

脚本功能:验证 EvolutionManager 各 early-return / 失败路径经
``trigger_diagnostics.log_trigger_block`` 输出统一格式日志:
- 真失败类(工具缺失 / 空窗口 / reviewer 写失败 / 超时未写入 / 异常)= ERROR
- 设计内跳过类(非 completed / reviewer 自环 / 冷启动 / 节律未到)= INFO
- ``enabled=false`` 全链静默(用户明确豁免,不产生任何分类日志)
- ``config/setting.yaml`` 的测试节律为 every_n_runs=3 / min_user_turns=3

关键辅助:
- ``_cfg``:构造隔离 Config(evolution root 指向 tmp_path)
- ``_blocked``:从 caplog 提取指定类别的 ``trigger blocked:`` 记录
- ``_FakeParentRuntime``:最小 parent runtime 替身(agent_spec / tools / event_sinks)
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from core.agent_spec import AgentSpec
from core.message import Message
from core.result import Result
from core.session import InMemorySession
from evolution.evolution_manager import EvolutionManager
from evolution.models import TranscriptMessage
from evolution.reviewer_runtime import ChildReviewOutcome
from tests._helpers.mock_transcript_provider import MockTranscriptProvider

_REPO_ROOT = Path(__file__).resolve().parents[2]
_THREAD = "thread-aabbccddeeff"


def _cfg(
    tmp_path: Path,
    *,
    enabled: bool = True,
    every_n: int = 3,
    min_turns: int = 3,
) -> Any:
    """构造隔离 Config:learning 开关 / 节律可控,evo 根目录指向 tmp_path。"""
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


def _provider(
    messages: tuple[TranscriptMessage, ...] = (
        TranscriptMessage(turn=1, role="user", content="q1"),
        TranscriptMessage(turn=1, role="assistant", content="a1"),
    ),
) -> MockTranscriptProvider:
    """构造预制窗口 provider;传空元组即得空证据窗口。"""
    return MockTranscriptProvider(thread_id=_THREAD, _messages=messages)


@dataclass
class _FakeParentRuntime:
    """notify_runtime_run 的最小 parent runtime 替身。

    tools 用 dict 满足 ``REVIEWER_TOOL_NAME in tools`` 判定;
    event_sinks 供 _do_notify_runtime_run 透传。
    """

    agent_spec: AgentSpec
    tools: dict[str, object] = field(default_factory=dict)
    event_sinks: tuple[Any, ...] = ()


def _parent(
    *,
    role: str | None = None,
    with_tool: bool = True,
) -> _FakeParentRuntime:
    """构造 parent 替身:role 控制自环标记,with_tool 控制是否含 evolution_write。"""
    metadata: dict[str, Any] = {"evolution_role": role} if role else {}
    tools: dict[str, object] = {"evolution_write": object()} if with_tool else {}
    return _FakeParentRuntime(
        agent_spec=AgentSpec(
            name="parent",
            instructions="",
            default_model="test-model",
            metadata=metadata,
        ),
        tools=tools,
    )


def _result(status: str = "completed") -> Result:
    """构造主 run 结果;status 可控用于验证 run_not_completed 分类。"""
    return Result(
        run_id=f"run-{_THREAD}-1",
        session_id=_THREAD,
        status=status,  # type: ignore[arg-type]
        final_message=Message.assistant("done"),
        turn_count=1,
    )


async def _session(user_turns: int) -> InMemorySession:
    """构造含 N 条 user 消息的 native session,驱动 count_user_turns。"""
    session = InMemorySession(session_id=_THREAD)
    for index in range(user_turns):
        await session.append(Message.user(f"u{index}"))
    return session


def _blocked(
    caplog: pytest.LogCaptureFixture,
    category: str,
) -> list[logging.LogRecord]:
    """提取指定类别的 trigger blocked 记录。"""
    return [
        record
        for record in caplog.records
        if "trigger blocked:" in record.getMessage()
        and f"category={category}" in record.getMessage()
    ]


class TestYamlTestCadence:
    def test_setting_yaml_uses_test_cadence_3_3(self) -> None:
        """D5:仓库 setting.yaml 的触发节律已改为 3/3 供实测。"""
        raw = yaml.safe_load((_REPO_ROOT / "config" / "setting.yaml").read_text())
        learning = raw["evolution"]["learning"]
        assert learning["every_n_runs"] == 3
        assert learning["min_user_turns"] == 3


class TestDisabledExemption:
    @pytest.mark.asyncio
    async def test_disabled_all_apis_silent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """D3:enabled=false 时两条 notify 线都不产生任何分类日志。"""
        manager = EvolutionManager(config=_cfg(tmp_path, enabled=False), kongming_home=tmp_path)
        with caplog.at_level(logging.DEBUG, logger="evolution"):
            await manager.notify_user_message(thread_id=_THREAD, provider=_provider(), cwd="/tmp")
            await manager.notify_runtime_run(
                _parent(with_tool=False), await _session(0), _result("cancelled")
            )
        assert not [r for r in caplog.records if "trigger blocked:" in r.getMessage()]
        await manager.aclose()


class TestNotifyException:
    @pytest.mark.asyncio
    async def test_notify_exception_is_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """D1:notify 触发链内部异常 → notify_exception ERROR(claude 线入口)。"""
        manager = EvolutionManager(config=_cfg(tmp_path), kongming_home=tmp_path)
        with (
            patch.object(manager, "_do_notify", side_effect=RuntimeError("probe")),
            caplog.at_level(logging.INFO, logger="evolution"),
        ):
            await manager.notify_user_message(thread_id=_THREAD, provider=_provider(), cwd="/tmp")
        records = _blocked(caplog, "notify_exception")
        assert len(records) == 1
        assert records[0].levelno == logging.ERROR
        assert "RuntimeError: probe" in records[0].getMessage()
        await manager.aclose()


class TestNativeLineSkips:
    @pytest.mark.asyncio
    async def test_run_not_completed_is_info(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """D1:cancelled / failed 的主 run 归设计内跳过,INFO。"""
        manager = EvolutionManager(config=_cfg(tmp_path), kongming_home=tmp_path)
        with caplog.at_level(logging.INFO, logger="evolution"):
            await manager.notify_runtime_run(_parent(), await _session(0), _result("cancelled"))
        records = _blocked(caplog, "run_not_completed")
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        assert "status=cancelled" in records[0].getMessage()
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_reviewer_self_loop_is_info(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """D1:reviewer 自己的 run 不复盘,INFO 防自环。"""
        manager = EvolutionManager(config=_cfg(tmp_path), kongming_home=tmp_path)
        with caplog.at_level(logging.INFO, logger="evolution"):
            await manager.notify_runtime_run(_parent(role="reviewer"), await _session(0), _result())
        records = _blocked(caplog, "reviewer_self_loop")
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_missing_evolution_write_tool_is_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """D1(核心):parent 工具表缺 evolution_write 必须 ERROR,不允许静默。"""
        manager = EvolutionManager(config=_cfg(tmp_path), kongming_home=tmp_path)
        with caplog.at_level(logging.INFO, logger="evolution"):
            await manager.notify_runtime_run(_parent(with_tool=False), await _session(3), _result())
        records = _blocked(caplog, "missing_evolution_write_tool")
        assert len(records) == 1
        assert records[0].levelno == logging.ERROR
        assert "EvolutionManager.register_runtime_tools" in records[0].getMessage()
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_below_min_user_turns_is_info(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """D2:native 线冷启动未达标,INFO。"""
        manager = EvolutionManager(config=_cfg(tmp_path, min_turns=3), kongming_home=tmp_path)
        with caplog.at_level(logging.INFO, logger="evolution"):
            await manager.notify_runtime_run(_parent(), await _session(1), _result())
        records = _blocked(caplog, "below_min_user_turns")
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        assert "user_turn_count=1" in records[0].getMessage()
        await manager.aclose()


class TestClaudeLineSkips:
    @pytest.mark.asyncio
    async def test_cadence_not_due_is_info(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """D2:claude 线节律未到,INFO 且带 next at 提示。"""
        manager = EvolutionManager(
            config=_cfg(tmp_path, every_n=3, min_turns=1), kongming_home=tmp_path
        )
        with caplog.at_level(logging.INFO, logger="evolution"):
            await manager.notify_user_message(thread_id=_THREAD, provider=_provider(), cwd="/tmp")
        records = _blocked(caplog, "cadence_not_due")
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        assert "next at 3" in records[0].getMessage()
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_empty_window_is_error_and_marks_state(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """D2:cadence 命中但证据窗口为空 → ERROR + state 标记 skipped_empty_window。"""
        manager = EvolutionManager(
            config=_cfg(tmp_path, every_n=1, min_turns=1), kongming_home=tmp_path
        )
        with caplog.at_level(logging.INFO, logger="evolution"):
            await manager.notify_user_message(
                thread_id=_THREAD, provider=_provider(messages=()), cwd="/tmp"
            )
        records = _blocked(caplog, "empty_transcript_window")
        assert len(records) == 1
        assert records[0].levelno == logging.ERROR
        state = json.loads((tmp_path / "evo" / "evolution.state.json").read_text())
        assert state["sessions"][_THREAD]["last_review_status"] == "skipped_empty_window"
        await manager.aclose()


class TestReviewerExecutionFailures:
    """触发成功后 reviewer 执行段的失败分类(经 stub 线驱动)。"""

    async def _drive_review(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        *,
        outcome: ChildReviewOutcome | None = None,
        side_effect: BaseException | None = None,
    ) -> EvolutionManager:
        """公共驱动:cadence 1/1 直接命中,mock 掉 stub runtime 与 run_child_review。"""
        manager = EvolutionManager(
            config=_cfg(tmp_path, every_n=1, min_turns=1), kongming_home=tmp_path
        )
        stub_runtime = AsyncMock()
        mock_review = AsyncMock(return_value=outcome, side_effect=side_effect)
        with (
            patch.object(manager, "_build_stub_parent_runtime", return_value=stub_runtime),
            patch("evolution.reviewer_runtime.run_child_review", mock_review),
            caplog.at_level(logging.INFO, logger="evolution"),
        ):
            await manager.notify_user_message(thread_id=_THREAD, provider=_provider(), cwd="/tmp")
            await asyncio.sleep(0.05)
            await manager.aclose()
        return manager

    @pytest.mark.asyncio
    async def test_reviewer_write_failed_is_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """D1/A2:reviewer 完成但 evolution_write 未成功 → ERROR。"""
        outcome = ChildReviewOutcome(
            result=Result(
                run_id="evo-review-run-1",
                session_id=f"evo-review-{_THREAD}",
                status="completed",
                final_message=None,
                turn_count=1,
            ),
            write_ok=False,
            write_status="error",
            write_error="boom",
        )
        await self._drive_review(tmp_path, caplog, outcome=outcome)
        records = _blocked(caplog, "reviewer_write_failed")
        assert len(records) == 1
        assert records[0].levelno == logging.ERROR
        assert "write_error=boom" in records[0].getMessage()

    @pytest.mark.asyncio
    async def test_reviewer_timeout_without_write_is_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A2:超时且超时前未写入 → reviewer_timeout_no_write ERROR。"""
        await self._drive_review(tmp_path, caplog, side_effect=TimeoutError())
        records = _blocked(caplog, "reviewer_timeout_no_write")
        assert len(records) == 1
        assert records[0].levelno == logging.ERROR

    @pytest.mark.asyncio
    async def test_reviewer_exception_is_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """D1:reviewer 执行异常 → reviewer_exception ERROR。"""
        await self._drive_review(tmp_path, caplog, side_effect=RuntimeError("kaput"))
        records = _blocked(caplog, "reviewer_exception")
        assert len(records) == 1
        assert records[0].levelno == logging.ERROR
        assert "RuntimeError: kaput" in records[0].getMessage()
