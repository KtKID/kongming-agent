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
async def test_get_thread_usage_called_on_turn_completed() -> None:
    """**v2**：turn.completed 时调 manager.get_thread_usage 派生（不再调
    v1 record_run_usage）。"""
    stdout_lines = [
        b'{"type":"thread.started","thread_id":"019dee"}\n',
        b'{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":20,"output_tokens":50}}\n',
    ]
    proc = _make_mock_proc(stdout_lines, exit_code=0)

    usage_mgr = _make_fake_usage_manager()
    fake_dto = MagicMock()
    fake_dto.model_dump = MagicMock(return_value={"provider": "openai"})
    usage_mgr.get_thread_usage = AsyncMock(return_value=fake_dto)
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

    # v2 get_thread_usage 被调；v1 record_run_usage 不再调
    usage_mgr.get_thread_usage.assert_awaited()
    usage_mgr.record_run_usage.assert_not_awaited()


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
async def test_usage_summary_broadcast_after_turn_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**v2**：turn.completed 后 broadcaster.broadcast 被调，payload 含 v2 usage DTO。"""
    stdout_lines = [
        b'{"type":"thread.started","thread_id":"019dee"}\n',
        b'{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":20,"output_tokens":50}}\n',
    ]
    proc = _make_mock_proc(stdout_lines, exit_code=0)

    usage_mgr = _make_fake_usage_manager()
    fake_dto = MagicMock()
    fake_dto.model_dump = MagicMock(
        return_value={"provider": "openai", "total": {"input_tokens": 100}}
    )
    usage_mgr.get_thread_usage = AsyncMock(return_value=fake_dto)
    thread_mgr = _make_fake_thread_manager(usage_mgr)

    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()
    mock_broadcaster.emit = AsyncMock()
    monkeypatch.setattr("web.codex.service.get_broadcaster", lambda: mock_broadcaster)

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

    # 断言 broadcaster.broadcast 被调用，且参数包含 usage_summary_updated（v2 字段名）
    broadcast_calls = [
        c
        for c in mock_broadcaster.broadcast.call_args_list
        if isinstance(c[0][0], dict) and c[0][0].get("type") == "usage_summary_updated"
    ]
    assert len(broadcast_calls) == 1
    payload = broadcast_calls[0][0][0]
    assert payload["threadId"] == "thread-aabbccddeeff"
    # v2: 字段名是 "usage"（含 provider discriminator），不是 "usage_summary"
    assert "usage" in payload


@pytest.mark.asyncio
async def test_multiple_queries_each_call_get_thread_usage() -> None:
    """**v2**：多次 query 每次 turn.completed 都调一次 get_thread_usage（v1
    record_run_usage 全程不调）。"""
    usage_mgr = _make_fake_usage_manager()
    fake_dto = MagicMock()
    fake_dto.model_dump = MagicMock(return_value={"provider": "openai"})
    usage_mgr.get_thread_usage = AsyncMock(return_value=fake_dto)
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

    # v2 get_thread_usage 被调 2 次；v1 record_run_usage 全程不调
    assert usage_mgr.get_thread_usage.await_count == 2
    usage_mgr.record_run_usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_run_usage_failure_does_not_break_main_flow() -> None:
    """record_run_usage 抛异常不影响主对话流（向后兼容；v2 实际走 get_thread_usage）。"""
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
