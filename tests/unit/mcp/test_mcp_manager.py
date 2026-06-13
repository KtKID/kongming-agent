from __future__ import annotations

import asyncio
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from infrastructure.mcp import McpManager
from infrastructure.mcp.manager import _runtime_from_config

_FAKE_SERVER = Path(__file__).resolve().parents[2] / "fixtures" / "mcp" / "fake_mcp_server.py"


@dataclass(frozen=True)
class _ServerConfig:
    server_id: str
    command: str
    args: tuple[str, ...]
    enabled: bool = True
    env: dict[str, str] | None = None
    secret_env_keys: tuple[str, ...] = ()
    initialize_timeout_ms: int = 1_000
    call_timeout_ms: int = 1_000


def _fake_config(
    *,
    server_id: str = "fake",
    mode: str = "normal",
    initialize_timeout_ms: int = 1_000,
    call_timeout_ms: int = 1_000,
) -> _ServerConfig:
    return _ServerConfig(
        server_id=server_id,
        command=sys.executable,
        args=(str(_FAKE_SERVER), "--mode", mode),
        env={},
        initialize_timeout_ms=initialize_timeout_ms,
        call_timeout_ms=call_timeout_ms,
    )


@pytest.mark.unit
def test_runtime_env_keeps_process_env_over_config_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证进程环境优先，输入同名 config env，输出 runtime 使用进程值。"""
    monkeypatch.setenv("MINIMAX_API_HOST", "https://api.minimaxi.com")
    runtime = _runtime_from_config(
        _ServerConfig(
            server_id="minimax",
            command="uvx",
            args=("minimax-coding-plan-mcp", "-y"),
            env={"MINIMAX_API_HOST": "https://api.minimax.io"},
        )
    )

    assert runtime.env["MINIMAX_API_HOST"] == "https://api.minimaxi.com"


@pytest.mark.unit
async def test_start_all_initializes_and_lists_tools() -> None:
    manager = McpManager([_fake_config()])
    try:
        await manager.start_all()

        tools = await manager.list_tools("fake")
        assert len(tools) == 1
        assert tools[0].server_id == "fake"
        assert tools[0].name == "web_search"
        assert tools[0].title == "Fake Web Search"
        assert tools[0].input_schema["properties"]["query"]["type"] == "string"

        diagnostics = manager.diagnostics()
        assert diagnostics["servers"]["fake"]["status"] == "ready"
        assert diagnostics["servers"]["fake"]["tool_count"] == 1
    finally:
        await manager.aclose()


@pytest.mark.unit
async def test_call_tool_returns_structured_search_data() -> None:
    manager = McpManager([_fake_config()])
    try:
        await manager.start_all()

        result = await manager.call_tool("fake", "web_search", {"query": "kongming"})

        assert result.ok is True
        assert result.error_message is None
        assert "https://example.com/result" in result.content_text
        assert result.data["url"] == "https://example.com/result"
        assert result.data["title"] == "Fake Search Result"
        assert result.data["snippet"] == "Snippet for kongming"
        assert result.data["provider"] == "fake_mcp"
        assert result.diagnostics["server_id"] == "fake"
        assert result.diagnostics["tool_name"] == "web_search"
    finally:
        await manager.aclose()


@pytest.mark.unit
async def test_call_tool_from_different_event_loop_uses_owner_loop() -> None:
    manager = McpManager([_fake_config()])
    try:
        await manager.start_all()

        def _call_from_new_loop() -> object:
            return asyncio.run(manager.call_tool("fake", "web_search", {"query": "cross-loop"}))

        result = await asyncio.wait_for(asyncio.to_thread(_call_from_new_loop), timeout=5)

        assert result.ok is True
        assert result.data["url"] == "https://example.com/result"
        assert result.data["snippet"] == "Snippet for cross-loop"
    finally:
        await manager.aclose()


@pytest.mark.unit
async def test_call_timeout_records_diagnostics() -> None:
    manager = McpManager([_fake_config(mode="call-timeout", call_timeout_ms=100)])
    try:
        await manager.start_all()

        result = await manager.call_tool("fake", "web_search", {"query": "slow"})

        assert result.ok is False
        assert result.diagnostics["error_type"] == "call_timeout"
        diagnostics = manager.diagnostics()
        assert diagnostics["servers"]["fake"]["last_event"] == "call_timeout"
    finally:
        await manager.aclose()


@pytest.mark.unit
async def test_tools_list_timeout_records_diagnostics_and_closes_process() -> None:
    manager = McpManager([_fake_config(mode="list-timeout", initialize_timeout_ms=100)])
    try:
        await manager.start_all()

        diagnostics = manager.diagnostics()
        server_diag = diagnostics["servers"]["fake"]
        assert server_diag["last_event"] == "closed"
        assert server_diag["events"][-2]["type"] == "list_tools_timeout"
        assert server_diag["status"] == "closed"
    finally:
        await manager.aclose()


@pytest.mark.unit
async def test_json_rpc_error_records_diagnostics() -> None:
    manager = McpManager([_fake_config(mode="call-error")])
    try:
        await manager.start_all()

        result = await manager.call_tool("fake", "web_search", {"query": "boom"})

        assert result.ok is False
        assert result.diagnostics["error_type"] == "json_rpc_error"
        assert result.diagnostics["json_rpc_error"]["code"] == -32000
        diagnostics = manager.diagnostics()
        assert diagnostics["servers"]["fake"]["last_event"] == "json_rpc_error"
    finally:
        await manager.aclose()


@pytest.mark.unit
async def test_startup_failure_does_not_block_other_servers() -> None:
    manager = McpManager(
        [
            _ServerConfig(server_id="missing", command="definitely-missing-mcp-command", args=()),
            _fake_config(server_id="ready"),
        ]
    )
    try:
        await manager.start_all()

        tools = await manager.list_tools("ready")
        diagnostics = manager.diagnostics()

        assert len(tools) == 1
        assert diagnostics["servers"]["missing"]["last_event"] == "missing_command"
        assert diagnostics["servers"]["ready"]["status"] == "ready"
    finally:
        await manager.aclose()


@pytest.mark.unit
async def test_aclose_exits_fake_server_process() -> None:
    manager = McpManager([_fake_config()])
    await manager.start_all()
    diagnostics = manager.diagnostics()
    pid = diagnostics["servers"]["fake"]["pid"]

    await manager.aclose()

    closed = manager.diagnostics()["servers"]["fake"]
    assert closed["status"] == "closed"
    assert closed["exit_code"] is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, signal.SIG_DFL)
