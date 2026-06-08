"""ApprovalBridge 单测：can_use_tool 触发 + Future 等待 + allow_list 短路 + Bash 通配。"""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from web.integrations.claude_code.approval import ApprovalBridge
from web.integrations.claude_code.normalizer import ClaudeNormalizer
from web.shared.session_manager import SessionManager


class _FakeWriter:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)


def _make_ctx(tool_use_id: str = "toolu_test") -> ToolPermissionContext:
    return ToolPermissionContext(
        signal=None,
        suggestions=[],
        tool_use_id=tool_use_id,
        agent_id=None,
    )


# ---------------------------------------------------------------------------
# _matches 静态方法
# ---------------------------------------------------------------------------


def test_matches_exact_name() -> None:
    assert ApprovalBridge._matches("Read", "Read", {}) is True
    assert ApprovalBridge._matches("Read", "Write", {}) is False


def test_matches_bash_prefix_dict_input() -> None:
    assert ApprovalBridge._matches("Bash(npm:*)", "Bash", {"command": "npm install"}) is True
    assert ApprovalBridge._matches("Bash(npm:*)", "Bash", {"command": "  npm test  "}) is True
    assert ApprovalBridge._matches("Bash(npm:*)", "Bash", {"command": "git status"}) is False


def test_matches_bash_prefix_str_input() -> None:
    assert ApprovalBridge._matches("Bash(ls:*)", "Bash", "ls -la") is True


def test_matches_empty_inputs() -> None:
    assert ApprovalBridge._matches("", "Bash", {}) is False
    assert ApprovalBridge._matches("Bash(npm:*)", "", {"command": "npm install"}) is False
    assert ApprovalBridge._matches("Bash(npm:*)", "Bash", {}) is False
    assert ApprovalBridge._matches("Bash(npm:*)", "Bash", {"command": ""}) is False


def test_matches_bash_pattern_only_for_bash() -> None:
    """Bash(prefix:*) 只在 toolName='Bash' 时生效。"""
    assert ApprovalBridge._matches("Bash(npm:*)", "Read", {"command": "npm install"}) is False


# ---------------------------------------------------------------------------
# can_use_tool 主流程
# ---------------------------------------------------------------------------


async def test_can_use_tool_allow_list_short_circuit() -> None:
    """allow_list 命中：直接 PermissionResultAllow，不发 permission_request。"""
    bridge = ApprovalBridge(ClaudeNormalizer(), SessionManager())
    bridge._allow_list.append("Read")
    writer = _FakeWriter()
    bridge.set_active_writer(writer)

    result = await bridge.can_use_tool("Read", {"path": "/tmp"}, _make_ctx())
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == {"path": "/tmp"}
    assert writer.sent == []  # 没发 permission_request


async def test_can_use_tool_emits_permission_request_and_resolves_allow() -> None:
    """allow 路径：emit permission_request → resolve(allow) → PermissionResultAllow。"""
    bridge = ApprovalBridge(ClaudeNormalizer(), SessionManager())
    writer = _FakeWriter()
    bridge.set_active_writer(writer)

    async def resolver() -> None:
        # 给 can_use_tool 一点时间发出 permission_request 并挂上 Future
        await asyncio.sleep(0.02)
        bridge.resolve("toolu_test", {"allow": True, "updatedInput": {"new": "input"}})

    asyncio.create_task(resolver())  # noqa: RUF006

    result = await bridge.can_use_tool("Bash", {"command": "ls"}, _make_ctx("toolu_test"))
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == {"new": "input"}
    assert len(writer.sent) == 1
    assert writer.sent[0]["frame_type"] == "permission_request"
    assert writer.sent[0]["requestId"] == "toolu_test"
    assert writer.sent[0]["toolName"] == "Bash"


async def test_can_use_tool_resolve_deny_calls_normalizer_add_pending_deny() -> None:
    """deny 路径：resolve(allow=False) → PermissionResultDeny + normalizer.add_pending_deny。"""
    normalizer = ClaudeNormalizer()
    bridge = ApprovalBridge(normalizer, SessionManager())
    bridge.set_active_writer(_FakeWriter())

    async def resolver() -> None:
        await asyncio.sleep(0.02)
        bridge.resolve("toolu_x", {"allow": False, "message": "no way"})

    asyncio.create_task(resolver())  # noqa: RUF006
    result = await bridge.can_use_tool("Bash", {"command": "rm -rf /"}, _make_ctx("toolu_x"))
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "no way"
    assert "toolu_x" in normalizer._pending_deny


async def test_can_use_tool_remember_entry_appended_to_allow_list() -> None:
    bridge = ApprovalBridge(ClaudeNormalizer(), SessionManager())
    bridge.set_active_writer(_FakeWriter())

    async def resolver() -> None:
        await asyncio.sleep(0.02)
        bridge.resolve(
            "toolu_y",
            {"allow": True, "rememberEntry": "Bash(npm:*)"},
        )

    asyncio.create_task(resolver())  # noqa: RUF006
    await bridge.can_use_tool("Bash", {"command": "npm test"}, _make_ctx("toolu_y"))
    assert "Bash(npm:*)" in bridge.allow_list

    # 第二次同前缀命令直接走短路（不需要再 resolve）
    result2 = await bridge.can_use_tool(
        "Bash",
        {"command": "npm install"},
        _make_ctx("toolu_z"),
    )
    assert isinstance(result2, PermissionResultAllow)


async def test_can_use_tool_no_active_writer_denies() -> None:
    """没 active writer 时直接 deny 兜底。"""
    bridge = ApprovalBridge(ClaudeNormalizer(), SessionManager())
    # 不调 set_active_writer
    result = await bridge.can_use_tool("Read", {}, _make_ctx())
    assert isinstance(result, PermissionResultDeny)


async def test_resolve_unknown_request_id_returns_false() -> None:
    bridge = ApprovalBridge(ClaudeNormalizer(), SessionManager())
    assert bridge.resolve("nope", {"allow": True}) is False


async def test_clear_active_writer() -> None:
    bridge = ApprovalBridge(ClaudeNormalizer(), SessionManager())
    bridge.set_active_writer(_FakeWriter())
    bridge.clear_active_writer()
    # writer 清空后再调 can_use_tool 走 deny 兜底
    result = await bridge.can_use_tool("Read", {}, _make_ctx())
    assert isinstance(result, PermissionResultDeny)


async def test_writer_send_failure_denies() -> None:
    """writer.send_json 抛错时 deny 兜底。"""

    class BrokenWriter:
        async def send_json(self, msg: dict[str, Any]) -> None:
            raise ConnectionError("disconnected")

    bridge = ApprovalBridge(ClaudeNormalizer(), SessionManager())
    bridge.set_active_writer(BrokenWriter())
    result = await bridge.can_use_tool("Read", {}, _make_ctx())
    assert isinstance(result, PermissionResultDeny)


async def test_can_use_tool_cancelled_propagates() -> None:
    """can_use_tool 被 cancel 时清 pending Future 并向上抛 CancelledError。"""
    bridge = ApprovalBridge(ClaudeNormalizer(), SessionManager())
    bridge.set_active_writer(_FakeWriter())

    async def call() -> Any:
        return await bridge.can_use_tool("Read", {}, _make_ctx("toolu_cancel"))

    task = asyncio.create_task(call())
    await asyncio.sleep(0.05)  # 让 future 挂上
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # pending 集合应当已清
    assert "toolu_cancel" not in bridge._pending
