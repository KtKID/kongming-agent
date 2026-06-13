"""InboxEventSink 单元测试。

覆盖：
- emit_approval_required：转 pending → payload → 注册 callback → emit_add
- emit_approval_removed：调 broadcaster.emit_remove（via emit_remove_safe）
- 异常吞掉：emit_add / emit_remove 抛 → sink 不向上抛
- Protocol 形态：满足 ApprovalEventSink Protocol（duck typing）
- 阶段 1 折中：emit_approval_removed 不做 per-request unregister
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from safety.approval.manager import ApprovalManager, _PendingApproval
from safety.inbox.event_sink import InboxEventSink


def _make_pending(
    *,
    request_id: str = "r1",
    channel: str = "generic_chat",
    thread_id: str = "t1",
    cwd: str = "/tmp",
    tool_name: str = "Bash",
    tool_input: dict | None = None,
    severity: str = "standard",
    matched_rule: str | None = None,
    auto_approve_at_ms: int | None = None,
    auto_reject_at_ms: int | None = None,
    arrived_at_ms: int = 1234,
    timeout_ms: int | None = 60_000,
) -> _PendingApproval:
    """构造 _PendingApproval fixture。

    sink 不读 ``future``，所以直接塞 ``MagicMock()`` 兼容 dataclass 字段要求。
    ``timeout_ms`` 默认 60_000 与 manager.request 默认 actual_timeout_ms 行为一致；
    测试用例可显式传 None 验证兜底分支。
    """
    return _PendingApproval(
        request_id=request_id,
        channel=channel,
        thread_id=thread_id,
        cwd=cwd,
        tool_name=tool_name,
        tool_input=tool_input or {"command": "ls"},
        metadata={},
        severity=severity,
        matched_rule=matched_rule,
        auto_approve_at_ms=auto_approve_at_ms,
        auto_reject_at_ms=auto_reject_at_ms,
        future=MagicMock(spec=asyncio.Future),
        arrived_at_ms=arrived_at_ms,
        timeout_ms=timeout_ms,
    )


class TestEmitApprovalRequired:
    """emit_approval_required：转 pending → payload → 注册 callback → emit_add。"""

    async def test_calls_broadcaster_with_correct_payload(self) -> None:
        """payload 含 generic_chat 字段；register_resolve_target 用 manager.resolve；emit_add 被调一次。"""
        broadcaster = AsyncMock()
        broadcaster.register_resolve_target = MagicMock()  # 同步方法

        manager = MagicMock(spec=ApprovalManager)
        manager.resolve = MagicMock(return_value=True)

        sink = InboxEventSink(broadcaster=broadcaster, manager=manager)
        pending = _make_pending(
            request_id="r-x",
            thread_id="t-y",
            tool_name="Bash",
            tool_input={"command": "ls"},
            cwd="/work",
            arrived_at_ms=5000,
        )

        await sink.emit_approval_required(pending=pending)

        # register_resolve_target 注册 manager.resolve
        broadcaster.register_resolve_target.assert_called_once_with("t-y", manager.resolve)
        # emit_add 被 await 一次，payload 内字段对齐 inbox helpers schema
        broadcaster.emit_add.assert_awaited_once()
        payload = broadcaster.emit_add.call_args.args[0]
        assert payload["requestId"] == "r-x"
        assert payload["threadId"] == "t-y"
        assert payload["channel"] == "generic_chat"
        assert payload["toolName"] == "Bash"
        assert payload["toolInput"] == {"command": "ls"}
        assert payload["cwd"] == "/work"
        assert payload["arrivedAtMs"] == 5000
        assert payload["isElevated"] is False  # standard severity
        assert payload["autoApproveAtMs"] is None
        assert payload["autoRejectAtMs"] is None
        assert payload["blockedByRule"] is None
        assert "kind" not in payload  # kind 由 broadcaster 自动补

    async def test_elevated_severity_maps_to_is_elevated_true(self) -> None:
        """severity='elevated' → payload.isElevated=True；matched_rule → blockedByRule。"""
        broadcaster = AsyncMock()
        broadcaster.register_resolve_target = MagicMock()
        manager = MagicMock(spec=ApprovalManager)

        sink = InboxEventSink(broadcaster=broadcaster, manager=manager)
        pending = _make_pending(
            severity="elevated",
            matched_rule="Bash(rm:*)",
            auto_reject_at_ms=30_000,
        )

        await sink.emit_approval_required(pending=pending)

        payload = broadcaster.emit_add.call_args.args[0]
        assert payload["isElevated"] is True
        assert payload["blockedByRule"] == "Bash(rm:*)"
        assert payload["autoRejectAtMs"] == 30_000

    async def test_payload_timeout_ms_from_pending(self) -> None:
        """pending.timeout_ms 透传到 broadcaster.emit_add 的 payload.timeoutMs。

        覆盖 smart-approval-generic-chat-autoallow 任务 #1 P0 约束：
        sink 从 _PendingApproval.timeout_ms 取实际超时值并以 camelCase
        ``timeoutMs`` 写入 add 帧 payload，与前端 fallback 倒计时契约对齐。
        """
        broadcaster = AsyncMock()
        broadcaster.register_resolve_target = MagicMock()
        manager = MagicMock(spec=ApprovalManager)

        sink = InboxEventSink(broadcaster=broadcaster, manager=manager)
        pending = _make_pending(timeout_ms=45_000)

        await sink.emit_approval_required(pending=pending)

        payload = broadcaster.emit_add.call_args.args[0]
        assert payload["timeoutMs"] == 45_000
        assert isinstance(payload["timeoutMs"], int)

    async def test_payload_timeout_ms_fallback_when_none(self) -> None:
        """pending.timeout_ms=None 时 sink 兜底 60_000（防御未来直构 _PendingApproval）。"""
        broadcaster = AsyncMock()
        broadcaster.register_resolve_target = MagicMock()
        manager = MagicMock(spec=ApprovalManager)

        sink = InboxEventSink(broadcaster=broadcaster, manager=manager)
        pending = _make_pending(timeout_ms=None)

        await sink.emit_approval_required(pending=pending)

        payload = broadcaster.emit_add.call_args.args[0]
        assert payload["timeoutMs"] == 60_000

    async def test_emit_add_exception_swallowed(self) -> None:
        """broadcaster.emit_add 抛 → sink 不向上抛，只 logger.exception。"""
        broadcaster = AsyncMock()
        broadcaster.register_resolve_target = MagicMock()
        broadcaster.emit_add.side_effect = RuntimeError("ws closed")
        manager = MagicMock(spec=ApprovalManager)

        sink = InboxEventSink(broadcaster=broadcaster, manager=manager)
        pending = _make_pending()

        # 不应抛
        await sink.emit_approval_required(pending=pending)
        broadcaster.emit_add.assert_awaited_once()

    async def test_register_resolve_target_exception_swallowed(self) -> None:
        """register_resolve_target 抛 → sink 吞异常，emit_add 不会被调（早抛在前面）。"""
        broadcaster = AsyncMock()
        broadcaster.register_resolve_target = MagicMock(side_effect=RuntimeError("registry locked"))
        manager = MagicMock(spec=ApprovalManager)

        sink = InboxEventSink(broadcaster=broadcaster, manager=manager)
        pending = _make_pending()

        # 不应抛
        await sink.emit_approval_required(pending=pending)
        broadcaster.emit_add.assert_not_awaited()


class TestEmitApprovalRemoved:
    """emit_approval_removed：转 reason → broadcaster.emit_remove。"""

    async def test_calls_emit_remove_with_correct_args(self) -> None:
        """emit_remove 被以 request_id + reason 正确调用。"""
        broadcaster = AsyncMock()
        manager = MagicMock(spec=ApprovalManager)

        sink = InboxEventSink(broadcaster=broadcaster, manager=manager)
        await sink.emit_approval_removed(request_id="r-z", reason="user_decided")

        broadcaster.emit_remove.assert_awaited_once_with("r-z", "user_decided")

    async def test_emit_remove_exception_swallowed_by_safe_helper(self) -> None:
        """emit_remove 抛 → emit_remove_safe helper 吞，sink 不向上抛。"""
        broadcaster = AsyncMock()
        broadcaster.emit_remove.side_effect = ConnectionError("broken pipe")
        manager = MagicMock(spec=ApprovalManager)

        sink = InboxEventSink(broadcaster=broadcaster, manager=manager)
        # 不应抛
        await sink.emit_approval_removed(request_id="r-q", reason="timeout")
        broadcaster.emit_remove.assert_awaited_once()

    async def test_does_not_unregister_resolve_target_stage1(self) -> None:
        """阶段 1 折中：emit_approval_removed 不做 per-request unregister。

        阶段 2 ApprovalBridge 接入时按 thread 生命周期统一处理；
        本阶段留 broadcaster._resolve_targets 内 callback 残留无害
        （manager.resolve 在 pending done 时返回 False，幂等）。
        """
        broadcaster = AsyncMock()
        broadcaster.unregister_resolve_target = MagicMock()
        manager = MagicMock(spec=ApprovalManager)

        sink = InboxEventSink(broadcaster=broadcaster, manager=manager)
        await sink.emit_approval_removed(request_id="r", reason="user_decided")

        # 折中验证：unregister 不在 remove 路径调
        broadcaster.unregister_resolve_target.assert_not_called()


class TestProtocolCompliance:
    """InboxEventSink 满足 ApprovalEventSink Protocol（duck typing 验证）。"""

    def test_has_required_methods(self) -> None:
        """ApprovalEventSink Protocol 要求 emit_approval_required + emit_approval_removed。"""
        broadcaster = AsyncMock()
        manager = MagicMock()
        sink = InboxEventSink(broadcaster=broadcaster, manager=manager)
        assert callable(sink.emit_approval_required)
        assert callable(sink.emit_approval_removed)

    def test_accepts_keyword_only_arguments(self) -> None:
        """Protocol 签名是 kw-only（``*, pending=`` / ``*, request_id=, reason=``）。

        positional 调用必须抛 TypeError 才能保证阶段 4 加 CLIEventSink 时
        签名不会被 positional 绑定意外锁死。
        """
        import inspect

        sig_required = inspect.signature(InboxEventSink.emit_approval_required)
        # self + pending → 'pending' 必须是 KEYWORD_ONLY
        params = list(sig_required.parameters.values())
        assert params[1].name == "pending"
        assert params[1].kind == inspect.Parameter.KEYWORD_ONLY

        sig_removed = inspect.signature(InboxEventSink.emit_approval_removed)
        params2 = list(sig_removed.parameters.values())
        # self + request_id + reason 都必须 kw-only
        assert params2[1].name == "request_id"
        assert params2[1].kind == inspect.Parameter.KEYWORD_ONLY
        assert params2[2].name == "reason"
        assert params2[2].kind == inspect.Parameter.KEYWORD_ONLY
