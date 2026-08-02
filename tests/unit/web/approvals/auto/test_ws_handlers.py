"""hosts.web.approvals.auto.ws_handlers 单测。

验证 toggle / query / state-msg 三个公共函数的行为，覆盖：

- toggle 成功 → 调 policy.set_enabled + 回 state 帧（字段对齐）
- toggle 缺 cwd / 空 cwd / 非 str cwd → 回 error 帧（字面值对齐）
- toggle / query policy 为 None → 回 error 帧（"auto_approval_policy not configured"）
- query 成功 → 回 state 帧（不写 set_enabled）
- query 缺 cwd → 回 error 帧
- build_auto_approval_state_msg → 字段 schema 与 v1 完全一致；timeoutMs fallback
  到 rule_set.default_timeout_ms

测试用 stub policy（实现 set_enabled / get_config / rule_set 三个最小成员），
不构造真 fastapi app，确保 handler 可独立单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from hosts.web.approvals.auto.ws_handlers import (
    build_auto_approval_state_msg,
    handle_auto_approval_query,
    handle_auto_approval_set_mode,
)
from safety.auto_approval.disposition import ApprovalDispositionMode

# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


@dataclass
class _StubRuleSet:
    default_timeout_ms: int = 10000


@dataclass
class _StubProjectConfig:
    cwd: str = ""
    mode: ApprovalDispositionMode = ApprovalDispositionMode.USER
    timeout_ms: int = 0
    rule_overrides: dict[str, bool] = field(default_factory=dict)


class _StubPolicy:
    """最小 policy 替身：实现 ws_handlers 用到的 3 个成员。"""

    def __init__(self, default_timeout_ms: int = 10000) -> None:
        self.rule_set = _StubRuleSet(default_timeout_ms=default_timeout_ms)
        self._cfgs: dict[str, _StubProjectConfig] = {}
        self.set_mode_calls: list[tuple[str, ApprovalDispositionMode]] = []

    def get_config(self, cwd: str) -> _StubProjectConfig:
        return self._cfgs.get(cwd, _StubProjectConfig(cwd=cwd))

    def set_mode(
        self,
        cwd: str,
        mode: ApprovalDispositionMode,
    ) -> _StubProjectConfig:
        self.set_mode_calls.append((cwd, mode))
        cfg = self._cfgs.setdefault(cwd, _StubProjectConfig(cwd=cwd))
        cfg.cwd = cwd
        cfg.mode = mode
        return cfg

    def set_mode_from_wire(self, cwd: str, raw_mode: str) -> _StubProjectConfig:
        """模拟 Web wire 枚举到 safety 枚举的转换。"""
        return self.set_mode(cwd, ApprovalDispositionMode(raw_mode))

    def state_for_wire(self, cwd: str) -> tuple[str, int, dict[str, bool]]:
        """返回 WS state 的扁平字段。"""
        cfg = self.get_config(cwd)
        return (
            cfg.mode.value,
            cfg.timeout_ms or self.rule_set.default_timeout_ms,
            cfg.rule_overrides,
        )


class _FakeWebSocket:
    """收集 send_json 调用的极简 WebSocket 替身。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)


# ---------------------------------------------------------------------------
# handle_auto_approval_set_mode
# ---------------------------------------------------------------------------


class TestHandleToggle:
    @pytest.mark.asyncio
    async def test_toggle_on_success_calls_policy_and_sends_state(self) -> None:
        ws = _FakeWebSocket()
        policy = _StubPolicy()
        data = {"frame_type": "auto-approval-set-mode", "cwd": "/proj/test", "mode": "llm"}

        await handle_auto_approval_set_mode(ws, data, policy)  # type: ignore[arg-type]

        assert policy.set_mode_calls == [("/proj/test", ApprovalDispositionMode.LLM)]
        # 2. 回了 1 帧 state，且字段对齐
        assert len(ws.sent) == 1
        msg = ws.sent[0]
        assert msg["frame_type"] == "auto_approval_state"
        assert msg["channel"] == "claude_code"
        assert msg["cwd"] == "/proj/test"
        assert msg["mode"] == "llm"
        assert msg["timeoutMs"] == 10000
        assert msg["ruleOverrides"] == {}

    @pytest.mark.asyncio
    async def test_toggle_off_persists(self) -> None:
        ws = _FakeWebSocket()
        policy = _StubPolicy()

        await handle_auto_approval_set_mode(
            ws,
            {"cwd": "/p", "mode": "full_trust"},
            policy,  # type: ignore[arg-type]
        )
        await handle_auto_approval_set_mode(
            ws,
            {"cwd": "/p", "mode": "user"},
            policy,  # type: ignore[arg-type]
        )

        assert policy.set_mode_calls == [
            ("/p", ApprovalDispositionMode.FULL_TRUST),
            ("/p", ApprovalDispositionMode.USER),
        ]
        assert ws.sent[-1]["mode"] == "user"

    @pytest.mark.asyncio
    async def test_toggle_missing_cwd_returns_error(self) -> None:
        ws = _FakeWebSocket()
        policy = _StubPolicy()

        await handle_auto_approval_set_mode(ws, {"mode": "llm"}, policy)  # type: ignore[arg-type]

        assert policy.set_mode_calls == []
        assert len(ws.sent) == 1
        msg = ws.sent[0]
        assert msg["frame_type"] == "error"
        assert msg["provider"] == "claude"
        # error 字面值与 claude_code/route.py 原版 100% 对齐
        assert msg["error"] == "auto-approval-set-mode.cwd required"

    @pytest.mark.asyncio
    async def test_toggle_empty_cwd_returns_error(self) -> None:
        ws = _FakeWebSocket()
        policy = _StubPolicy()

        await handle_auto_approval_set_mode(
            ws,
            {"cwd": "", "mode": "llm"},
            policy,  # type: ignore[arg-type]
        )

        assert ws.sent[0]["frame_type"] == "error"
        assert ws.sent[0]["error"] == "auto-approval-set-mode.cwd required"

    @pytest.mark.asyncio
    async def test_toggle_non_str_cwd_returns_error(self) -> None:
        ws = _FakeWebSocket()
        policy = _StubPolicy()

        await handle_auto_approval_set_mode(
            ws,
            {"cwd": 123, "mode": "llm"},
            policy,  # type: ignore[arg-type]
        )

        assert ws.sent[0]["frame_type"] == "error"

    @pytest.mark.asyncio
    async def test_toggle_policy_none_returns_error(self) -> None:
        ws = _FakeWebSocket()

        await handle_auto_approval_set_mode(ws, {"cwd": "/p", "mode": "llm"}, None)  # type: ignore[arg-type]

        assert len(ws.sent) == 1
        msg = ws.sent[0]
        assert msg["frame_type"] == "error"
        # error 字面值与 claude_code/route.py 原版 100% 对齐
        assert msg["error"] == "auto_approval_policy not configured"

    @pytest.mark.asyncio
    async def test_toggle_missing_mode_returns_error(self) -> None:
        """mode 缺省时拒绝请求，避免静默降级为错误处置模式。"""
        ws = _FakeWebSocket()
        policy = _StubPolicy()

        await handle_auto_approval_set_mode(ws, {"cwd": "/p"}, policy)  # type: ignore[arg-type]

        assert policy.set_mode_calls == []
        assert ws.sent[0]["frame_type"] == "error"


# ---------------------------------------------------------------------------
# handle_auto_approval_query
# ---------------------------------------------------------------------------


class TestHandleQuery:
    @pytest.mark.asyncio
    async def test_query_new_cwd_returns_default_state(self) -> None:
        ws = _FakeWebSocket()
        policy = _StubPolicy()

        await handle_auto_approval_query(ws, {"cwd": "/new"}, policy)  # type: ignore[arg-type]

        assert policy.set_mode_calls == []
        assert len(ws.sent) == 1
        msg = ws.sent[0]
        assert msg["frame_type"] == "auto_approval_state"
        assert msg["cwd"] == "/new"
        assert msg["mode"] == "user"
        assert msg["timeoutMs"] == 10000

    @pytest.mark.asyncio
    async def test_query_after_toggle_reflects_state(self) -> None:
        ws = _FakeWebSocket()
        policy = _StubPolicy()
        await handle_auto_approval_set_mode(
            ws,
            {"cwd": "/p", "mode": "llm"},
            policy,  # type: ignore[arg-type]
        )
        ws.sent.clear()

        await handle_auto_approval_query(ws, {"cwd": "/p"}, policy)  # type: ignore[arg-type]

        assert ws.sent[0]["mode"] == "llm"

    @pytest.mark.asyncio
    async def test_query_missing_cwd_returns_error(self) -> None:
        ws = _FakeWebSocket()
        policy = _StubPolicy()

        await handle_auto_approval_query(ws, {}, policy)  # type: ignore[arg-type]

        assert ws.sent[0]["frame_type"] == "error"
        # error 字面值与 claude_code/route.py 原版 100% 对齐
        assert ws.sent[0]["error"] == "auto-approval-query.cwd required"

    @pytest.mark.asyncio
    async def test_query_policy_none_returns_error(self) -> None:
        ws = _FakeWebSocket()

        await handle_auto_approval_query(ws, {"cwd": "/p"}, None)  # type: ignore[arg-type]

        assert ws.sent[0]["frame_type"] == "error"
        assert ws.sent[0]["error"] == "auto_approval_policy not configured"


# ---------------------------------------------------------------------------
# build_auto_approval_state_msg
# ---------------------------------------------------------------------------


class TestBuildStateMsg:
    def test_default_fields(self) -> None:
        policy = _StubPolicy(default_timeout_ms=12345)

        msg = build_auto_approval_state_msg(policy, "/proj")

        assert msg == {
            "frame_type": "auto_approval_state",
            "channel": "claude_code",
            "cwd": "/proj",
            "mode": "user",
            "timeoutMs": 12345,
            "ruleOverrides": {},
        }

    def test_per_cwd_timeout_overrides_default(self) -> None:
        policy = _StubPolicy(default_timeout_ms=10000)
        policy._cfgs["/p"] = _StubProjectConfig(
            cwd="/p",
            mode=ApprovalDispositionMode.LLM,
            timeout_ms=8000,
            rule_overrides={"rule_x": False},
        )

        msg = build_auto_approval_state_msg(policy, "/p")

        assert msg["timeoutMs"] == 8000
        assert msg["mode"] == "llm"
        assert msg["ruleOverrides"] == {"rule_x": False}

    def test_fallback_cwd_when_cfg_cwd_empty(self) -> None:
        """cfg.cwd 为空 → 用传入的 cwd 作 fallback（原版 ``cfg.cwd or cwd``）。"""
        policy = _StubPolicy()
        policy._cfgs["/lookup"] = _StubProjectConfig(
            cwd="",
            mode=ApprovalDispositionMode.LLM,
        )

        msg = build_auto_approval_state_msg(policy, "/lookup")

        assert msg["cwd"] == "/lookup"

    def test_rule_overrides_is_copy_not_alias(self) -> None:
        """返回 ``dict(cfg.rule_overrides)`` 副本，外部改 msg 不污染 cfg。"""
        policy = _StubPolicy()
        policy._cfgs["/p"] = _StubProjectConfig(
            cwd="/p",
            rule_overrides={"r1": True},
        )

        msg = build_auto_approval_state_msg(policy, "/p")
        msg["ruleOverrides"]["r1"] = False

        assert policy._cfgs["/p"].rule_overrides == {"r1": True}


# ---------------------------------------------------------------------------
# v0.5 task #5：channel="generic_chat" 参数化
# ---------------------------------------------------------------------------


class TestGenericChatChannel:
    """``channel="generic_chat"`` 通道下 state / error 帧字段对齐。

    覆盖 ``/ws/threads`` 调用方传入 generic_chat 通道时 ws_handlers 行为：
    - state 帧 ``channel`` 字段切到 ``"generic_chat"``，其余 schema 不变
    - error 帧 ``provider`` 字段切到 ``"generic"``（映射），同步追加
      ``channel="generic_chat"`` 字段供前端 / 测试直接断言
    - claude_code 默认通道行为保持不变（已在前面 TestHandleToggle 系列覆盖）
    """

    def test_build_state_msg_generic_chat_channel_field(self) -> None:
        """generic_chat 通道：state 帧 channel 字段切换 + 其他 schema 不变。"""
        policy = _StubPolicy(default_timeout_ms=10000)

        msg = build_auto_approval_state_msg(policy, "/p", channel="generic_chat")

        assert msg == {
            "frame_type": "auto_approval_state",
            "channel": "generic_chat",
            "cwd": "/p",
            "mode": "user",
            "timeoutMs": 10000,
            "ruleOverrides": {},
        }

    @pytest.mark.asyncio
    async def test_toggle_generic_chat_state_carries_channel(self) -> None:
        """generic_chat toggle 成功 → state 帧 channel == "generic_chat"。"""
        ws = _FakeWebSocket()
        policy = _StubPolicy()

        await handle_auto_approval_set_mode(
            ws,
            {"cwd": "/proj/test", "mode": "llm"},
            policy,  # type: ignore[arg-type]
            channel="generic_chat",
        )

        assert policy.set_mode_calls == [("/proj/test", ApprovalDispositionMode.LLM)]
        assert len(ws.sent) == 1
        msg = ws.sent[0]
        assert msg["frame_type"] == "auto_approval_state"
        assert msg["channel"] == "generic_chat"
        assert msg["mode"] == "llm"

    @pytest.mark.asyncio
    async def test_toggle_generic_chat_error_carries_channel_and_provider(
        self,
    ) -> None:
        """generic_chat toggle policy=None → error 帧 provider/channel 字段。"""
        ws = _FakeWebSocket()

        await handle_auto_approval_set_mode(
            ws,
            {"cwd": "/p", "mode": "llm"},
            None,  # type: ignore[arg-type]
            channel="generic_chat",
        )

        assert len(ws.sent) == 1
        msg = ws.sent[0]
        assert msg["frame_type"] == "error"
        # provider 按 _PROVIDER_BY_CHANNEL 映射到 "generic"
        assert msg["provider"] == "generic"
        # channel 字段独立暴露，前端 / 测试可直接断言通道
        assert msg["channel"] == "generic_chat"
        assert msg["error"] == "auto_approval_policy not configured"

    @pytest.mark.asyncio
    async def test_query_generic_chat_missing_cwd_error_channel(self) -> None:
        """generic_chat query 缺 cwd → error 帧 channel + provider 都标对。"""
        ws = _FakeWebSocket()
        policy = _StubPolicy()

        await handle_auto_approval_query(
            ws,
            {},
            policy,  # type: ignore[arg-type]
            channel="generic_chat",
        )

        msg = ws.sent[0]
        assert msg["frame_type"] == "error"
        assert msg["provider"] == "generic"
        assert msg["channel"] == "generic_chat"
        assert msg["error"] == "auto-approval-query.cwd required"

    @pytest.mark.asyncio
    async def test_query_generic_chat_success_state_channel(self) -> None:
        """generic_chat query 成功 → state 帧 channel == "generic_chat"。"""
        ws = _FakeWebSocket()
        policy = _StubPolicy()
        await handle_auto_approval_set_mode(
            ws,
            {"cwd": "/p", "mode": "llm"},
            policy,  # type: ignore[arg-type]
            channel="generic_chat",
        )
        ws.sent.clear()

        await handle_auto_approval_query(
            ws,
            {"cwd": "/p"},
            policy,  # type: ignore[arg-type]
            channel="generic_chat",
        )

        assert ws.sent[0]["frame_type"] == "auto_approval_state"
        assert ws.sent[0]["channel"] == "generic_chat"
        assert ws.sent[0]["mode"] == "llm"
