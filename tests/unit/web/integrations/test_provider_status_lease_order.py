"""Claude/Codex provider 启动前的 thread-status lease 与可中断性测试。"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from hosts.web.integrations.claude_code.normalizer import ClaudeNormalizer
from hosts.web.integrations.claude_code.service import ClaudeCodeService
from hosts.web.integrations.codex.service import CodexService
from hosts.web.shared.session_manager import SessionManager
from hosts.web.websocket.thread_status_manager import ThreadStatusManager
from tests.unit.web.integrations.claude_code._approval_test_support import (
    build_test_approval_bridge,
)


class _Writer:
    """记录 provider 错误或终态帧。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        """保存一条出站帧。"""
        self.sent.append(payload)


class _BlockingClaudeClient:
    """把 connect 固定阻塞，暴露 provider 启动窗口。"""

    def __init__(self) -> None:
        self.connect_entered = asyncio.Event()

    async def connect(self) -> None:
        """记录进入并持续等待，直到外层 abort 取消 query task。"""
        self.connect_entered.set()
        await asyncio.Event().wait()

    async def disconnect(self) -> None:
        """测试清理占位。"""

    async def interrupt(self) -> None:
        """测试清理占位。"""


@pytest.mark.asyncio
async def test_claude_lease_and_abort_exist_before_client_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude client.connect 阻塞时，服务端已 active 且 Stop 能取消启动 task。"""
    status_manager = ThreadStatusManager()
    monkeypatch.setattr(
        "hosts.web.integrations.claude_code.service.get_thread_status_manager",
        lambda: status_manager,
    )
    sessions = SessionManager()
    normalizer = ClaudeNormalizer()
    client = _BlockingClaudeClient()
    service = ClaudeCodeService(
        normalizer,
        build_test_approval_bridge(normalizer, sessions),
        sessions,
        client_factory=lambda **_: client,
    )

    query_task = asyncio.create_task(
        service.query(
            "hello",
            {"sessionId": "sdk-pending"},
            _Writer(),
            register_id_override="thread-aabbccddeeff",
        )
    )
    await client.connect_entered.wait()

    active = status_manager.active_statuses["thread-aabbccddeeff"]
    assert active.phase == "responding"
    assert sessions.is_active("thread-aabbccddeeff") is True
    assert await service.abort("thread-aabbccddeeff") is True
    await query_task
    assert "thread-aabbccddeeff" not in status_manager.active_statuses


@pytest.mark.asyncio
async def test_codex_lease_and_abort_exist_before_process_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex process 创建阻塞时，服务端已 active 且 Stop 能取消启动 task。"""
    status_manager = ThreadStatusManager()
    monkeypatch.setattr(
        "hosts.web.integrations.codex.service.get_thread_status_manager",
        lambda: status_manager,
    )
    spawn_entered = asyncio.Event()

    async def _blocking_spawn(*args: object, **kwargs: object) -> object:
        """记录进入并持续等待，直到外层 abort 取消 query task。"""
        del args, kwargs
        spawn_entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    sessions = SessionManager()
    service = CodexService(sessions)
    with patch(
        "hosts.web.integrations.codex.service.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=_blocking_spawn),
    ):
        query_task = asyncio.create_task(
            service.query(
                session_id="codex-pending",
                command="hello",
                cwd="/tmp",
                kongming_thread_id="thread-aabbccddeeff",
                writer=_Writer(),
            )
        )
        await spawn_entered.wait()

        active = status_manager.active_statuses["thread-aabbccddeeff"]
        assert active.phase == "responding"
        assert sessions.is_active("codex-pending") is True
        assert await service.abort("codex-pending") is True
        await query_task
    assert "thread-aabbccddeeff" not in status_manager.active_statuses
