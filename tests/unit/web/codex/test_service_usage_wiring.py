"""CodexService usage wiring 单测：验证 record_run_usage 在 turn.completed 时被调用。

覆盖 3 个场景：
1. 完整流程——thread_manager 存在 + kongming_thread_id 提供 → 调 record_run_usage
2. thread_manager=None → 跳过
3. kongming_thread_id=None（无产品层 thread id）→ 跳过
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from web._shared.session_manager import SessionManager
from web.codex.service import CodexService


class _MockStdout:
    """模拟 asyncio.subprocess.PIPE 的异步行迭代。"""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self) -> Any:
        return self._iter()

    async def _iter(self) -> Any:
        for line in self._lines:
            yield line


class _MockStderr:
    def __init__(self, lines: list[bytes] | None = None) -> None:
        self._lines = lines or []

    def __aiter__(self) -> Any:
        return self._iter()

    async def _iter(self) -> Any:
        for line in self._lines:
            yield line


def _make_mock_proc(
    stdout_lines: list[bytes],
    stderr_lines: list[bytes] | None = None,
    exit_code: int = 0,
) -> MagicMock:
    proc = MagicMock()
    proc.stdout = _MockStdout(stdout_lines)
    proc.stderr = _MockStderr(stderr_lines)
    proc.wait = AsyncMock(return_value=exit_code)
    proc.returncode = exit_code
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc


class _FakeWriter:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)


def _make_fake_usage_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.record_run_usage = AsyncMock(return_value=MagicMock())
    return mgr


def _make_fake_thread_manager(usage_manager: MagicMock) -> MagicMock:
    tm = MagicMock()
    tm.usage_manager = usage_manager
    tm.list_threads = MagicMock(return_value=[])
    return tm


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_run_usage_called_on_turn_completed() -> None:
    """完整流程：有效 kongming_thread_id + thread_manager → record_run_usage 被调用。"""
    usage_data = {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 50}
    stdout_lines = [
        b'{"type":"thread.started","thread_id":"019dee"}\n',
        b'{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":20,"output_tokens":50}}\n',
    ]
    proc = _make_mock_proc(stdout_lines, exit_code=0)

    usage_mgr = _make_fake_usage_manager()
    thread_mgr = _make_fake_thread_manager(usage_mgr)

    session_mgr = SessionManager()
    svc = CodexService(session_mgr, thread_manager=thread_mgr)

    writer = _FakeWriter()
    with patch(
        "web.codex.service.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        await svc.query(
            session_id="pending-1",
            command="hi",
            cwd="/tmp",
            kongming_thread_id="thread-aabbccddeeff",
            writer=writer,
        )

    # 断言 record_run_usage 被调用
    usage_mgr.record_run_usage.assert_awaited_once()
    call_kwargs = usage_mgr.record_run_usage.call_args
    assert call_kwargs[0][0] == "thread-aabbccddeeff"
    assert call_kwargs[1]["channel"] == "openai"
    assert call_kwargs[1]["raw_payload"] == usage_data
    assert call_kwargs[1]["turn"] == 1
    assert call_kwargs[1]["run_id"] == "thread-aabbccddeeff-1"


@pytest.mark.asyncio
async def test_record_run_usage_skipped_when_thread_manager_none() -> None:
    """thread_manager=None → 不调 record_run_usage。"""
    stdout_lines = [
        b'{"type":"thread.started","thread_id":"019dee"}\n',
        b'{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}\n',
    ]
    proc = _make_mock_proc(stdout_lines, exit_code=0)

    session_mgr = SessionManager()
    svc = CodexService(session_mgr, thread_manager=None)

    writer = _FakeWriter()
    with patch(
        "web.codex.service.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        await svc.query(
            session_id="pending-1",
            command="hi",
            cwd="/tmp",
            kongming_thread_id="thread-aabbccddeeff",
            writer=writer,
        )

    # 测试能跑完 = 跳过逻辑正确（不抛 AttributeError）
    kinds = [m.get("kind") for m in writer.sent]
    assert "complete" in kinds


@pytest.mark.asyncio
async def test_record_run_usage_skipped_when_no_kongming_thread_id() -> None:
    """kongming_thread_id=None → 跳过 record_run_usage。"""
    stdout_lines = [
        b'{"type":"thread.started","thread_id":"019dee"}\n',
        b'{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}\n',
    ]
    proc = _make_mock_proc(stdout_lines, exit_code=0)

    usage_mgr = _make_fake_usage_manager()
    thread_mgr = _make_fake_thread_manager(usage_mgr)

    session_mgr = SessionManager()
    svc = CodexService(session_mgr, thread_manager=thread_mgr)

    writer = _FakeWriter()
    with patch(
        "web.codex.service.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        await svc.query(
            session_id="pending-1",
            command="hi",
            cwd="/tmp",
            kongming_thread_id=None,
            writer=writer,
        )

    usage_mgr.record_run_usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_run_usage_increments_run_counter() -> None:
    """多次 query 调用 run_index 递增。"""
    usage_mgr = _make_fake_usage_manager()
    thread_mgr = _make_fake_thread_manager(usage_mgr)

    session_mgr = SessionManager()
    svc = CodexService(session_mgr, thread_manager=thread_mgr)

    tid = "thread-aabbccddeeff"

    for i in range(2):
        stdout_lines = [
            b'{"type":"thread.started","thread_id":"019dee"}\n',
            b'{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}\n',
        ]
        proc = _make_mock_proc(stdout_lines, exit_code=0)
        writer = _FakeWriter()
        with patch(
            "web.codex.service.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await svc.query(
                session_id=f"pending-{i}",
                command="hi",
                cwd="/tmp",
                kongming_thread_id=tid,
                writer=writer,
            )

    assert usage_mgr.record_run_usage.await_count == 2
    first_call = usage_mgr.record_run_usage.call_args_list[0]
    assert first_call[1]["turn"] == 1
    assert first_call[1]["run_id"] == f"{tid}-1"
    second_call = usage_mgr.record_run_usage.call_args_list[1]
    assert second_call[1]["turn"] == 2
    assert second_call[1]["run_id"] == f"{tid}-2"


@pytest.mark.asyncio
async def test_record_run_usage_failure_does_not_break_main_flow() -> None:
    """record_run_usage 抛异常不影响主对话流。"""
    stdout_lines = [
        b'{"type":"thread.started","thread_id":"019dee"}\n',
        b'{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"hi"}}\n',
        b'{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}\n',
    ]
    proc = _make_mock_proc(stdout_lines, exit_code=0)

    usage_mgr = _make_fake_usage_manager()
    usage_mgr.record_run_usage = AsyncMock(side_effect=RuntimeError("db error"))
    thread_mgr = _make_fake_thread_manager(usage_mgr)

    session_mgr = SessionManager()
    svc = CodexService(session_mgr, thread_manager=thread_mgr)

    writer = _FakeWriter()
    with patch(
        "web.codex.service.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        await svc.query(
            session_id="pending-1",
            command="hi",
            cwd="/tmp",
            kongming_thread_id="thread-aabbccddeeff",
            writer=writer,
        )

    # 主流程正常完成
    kinds = [m.get("kind") for m in writer.sent]
    assert "session_created" in kinds
    assert "complete" in kinds
