"""ApprovalBridge × smart-approval-v1 集成测。

覆盖：
- policy=None 走老行为（前面 test_approval.py 已覆盖，这里只兜底)
- policy + cwd disabled → emit 无 autoApproveAtMs，无倒计时
- policy + cwd enabled + safe Bash → emit 带 autoApproveAtMs，timeout 自动 allow
- policy + cwd enabled + dangerous Bash → blocked_by_rule 非空，不倒计时
- elevated 永不自动
- audit: 同一调用先 pending request，再 final decision；channel 字段对
- timeout 超时分支 → outcome=allowed_auto_timeout, decided_by=timeout
- 用户提前 resolve → outcome=allowed_manual + 取消 timer
- 命中规则 + 用户拒 → outcome=blocked_then_manual
- fake clock：autoApproveAtMs ≈ now + timeout_ms
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from web._shared.session_manager import SessionManager
from web.auto_approval import (
    AuditLogger,
    AutoApprovalPolicy,
    ConfigStore,
    load_default_rules,
)
from web.claude_code.approval import ApprovalBridge
from web.claude_code.normalizer import ClaudeNormalizer


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


@pytest.fixture()
def store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "auto_approval")


@pytest.fixture()
def policy(store: ConfigStore) -> AutoApprovalPolicy:
    # 用一个短超时（200ms）保持测试快
    rule_set = load_default_rules()
    # 替换 default_timeout_ms 为 200ms
    from dataclasses import replace

    fast_rule_set = replace(rule_set, default_timeout_ms=200)
    return AutoApprovalPolicy(fast_rule_set, store)


@pytest.fixture()
def audit(tmp_path: Path) -> AuditLogger:
    return AuditLogger(tmp_path / "audit.jsonl")


def _make_bridge(
    policy: AutoApprovalPolicy | None = None,
    audit: AuditLogger | None = None,
    cwd: str = "/proj",
    thread_id: str = "thread-aaaaaa111111",
) -> tuple[ApprovalBridge, _FakeWriter]:
    bridge = ApprovalBridge(
        ClaudeNormalizer(),
        SessionManager(),
        policy=policy,
        audit=audit,
        cwd=cwd,
        thread_id=thread_id,
        channel="claude_code",
    )
    writer = _FakeWriter()
    bridge.set_active_writer(writer)
    return bridge, writer


# ============================================================
# permission_request 帧字段
# ============================================================


class TestPermissionRequestFields:
    async def test_disabled_policy_no_auto_approve_at(
        self,
        policy: AutoApprovalPolicy,
    ) -> None:
        """policy 未启用：autoApproveAtMs=None，blockedByRule=None，channel=claude_code。"""
        bridge, writer = _make_bridge(policy=policy)

        async def resolver() -> None:
            await asyncio.sleep(0.02)
            bridge.resolve("toolu_x", {"allow": True})

        asyncio.create_task(resolver())  # noqa: RUF006
        await bridge.can_use_tool("Bash", {"command": "ls"}, _make_ctx("toolu_x"))

        assert len(writer.sent) == 1
        msg = writer.sent[0]
        assert msg["channel"] == "claude_code"
        assert msg["autoApproveAtMs"] is None
        assert msg["blockedByRule"] is None

    async def test_enabled_safe_bash_has_auto_approve_at(
        self,
        policy: AutoApprovalPolicy,
    ) -> None:
        """policy 启用 + safe Bash：autoApproveAtMs ≈ now + 200ms。"""
        policy.set_enabled("/proj", True)
        bridge, writer = _make_bridge(policy=policy)

        before = int(time.time() * 1000)

        async def resolver() -> None:
            await asyncio.sleep(0.02)
            bridge.resolve("toolu_x", {"allow": True})

        asyncio.create_task(resolver())  # noqa: RUF006
        await bridge.can_use_tool("Bash", {"command": "ls"}, _make_ctx("toolu_x"))

        after = int(time.time() * 1000)

        msg = writer.sent[0]
        assert msg["autoApproveAtMs"] is not None
        # 应在 before+200 .. after+200 之间
        assert before + 180 <= msg["autoApproveAtMs"] <= after + 220
        assert msg["blockedByRule"] is None

    async def test_enabled_dangerous_bash_blocked(
        self,
        policy: AutoApprovalPolicy,
    ) -> None:
        """policy 启用 + dangerous Bash：blockedByRule 非空 + 不倒计时。"""
        policy.set_enabled("/proj", True)
        bridge, writer = _make_bridge(policy=policy)

        async def resolver() -> None:
            await asyncio.sleep(0.02)
            bridge.resolve("toolu_x", {"allow": False, "message": "no"})

        asyncio.create_task(resolver())  # noqa: RUF006
        await bridge.can_use_tool("Bash", {"command": "rm -rf /tmp/x"}, _make_ctx("toolu_x"))

        msg = writer.sent[0]
        # v1.0.1: bash_rm_any 排在 bash_rm_recursive 之前，先命中先返回
        assert msg["blockedByRule"] == "bash_rm_any"
        assert msg["autoApproveAtMs"] is None  # 危险不自动


# ============================================================
# 超时自动通过
# ============================================================


class TestAutoApproveTimeout:
    async def test_timeout_auto_allows(
        self,
        policy: AutoApprovalPolicy,
        audit: AuditLogger,
    ) -> None:
        """无人响应 → 200ms 后自动 allow（PermissionResultAllow）。"""
        policy.set_enabled("/proj", True)
        bridge, _writer = _make_bridge(policy=policy, audit=audit)

        start = time.monotonic()
        result = await bridge.can_use_tool("Bash", {"command": "ls"}, _make_ctx("toolu_t"))
        elapsed_ms = (time.monotonic() - start) * 1000

        assert isinstance(result, PermissionResultAllow)
        assert 150 <= elapsed_ms <= 400, f"timeout drift: {elapsed_ms}ms"

        # audit 应该有 2 行：pending request + auto_timeout decision
        records = audit.read_all()
        assert len(records) == 2
        assert records[0]["decision"]["outcome"] == "pending"
        assert records[1]["decision"]["outcome"] == "allowed_auto_timeout"
        assert records[1]["decision"]["decided_by"] == "timeout"

    async def test_user_resolve_before_timeout_cancels_timer(
        self,
        policy: AutoApprovalPolicy,
        audit: AuditLogger,
    ) -> None:
        """用户提前 resolve → outcome=allowed_manual，不走 timeout 分支。"""
        policy.set_enabled("/proj", True)
        bridge, _writer = _make_bridge(policy=policy, audit=audit)

        async def resolver() -> None:
            await asyncio.sleep(0.05)  # 远早于 200ms timeout
            bridge.resolve("toolu_m", {"allow": True})

        asyncio.create_task(resolver())  # noqa: RUF006
        start = time.monotonic()
        result = await bridge.can_use_tool("Bash", {"command": "ls"}, _make_ctx("toolu_m"))
        elapsed_ms = (time.monotonic() - start) * 1000

        assert isinstance(result, PermissionResultAllow)
        assert elapsed_ms < 150, f"user resolve must precede timeout: {elapsed_ms}ms"

        records = audit.read_all()
        assert records[-1]["decision"]["outcome"] == "allowed_manual"
        assert records[-1]["decision"]["decided_by"] == "user"

    async def test_dangerous_rule_auto_reject_on_timeout(
        self,
        policy: AutoApprovalPolicy,
        audit: AuditLogger,
    ) -> None:
        """v1.0.1: 命中规则 + 无人响应 → 200ms 后自动 reject (fail-closed)。"""
        policy.set_enabled("/proj", True)
        bridge, writer = _make_bridge(policy=policy, audit=audit)

        start = time.monotonic()
        result = await bridge.can_use_tool(
            "Bash",
            {"command": "rm -rf /tmp/x"},
            _make_ctx("toolu_ar"),
        )
        elapsed_ms = (time.monotonic() - start) * 1000

        assert isinstance(result, PermissionResultDeny)
        assert 150 <= elapsed_ms <= 400, f"timeout drift: {elapsed_ms}ms"

        # permission_request 帧应有 autoRejectAtMs，无 autoApproveAtMs
        msg = writer.sent[0]
        assert msg["autoRejectAtMs"] is not None
        assert msg["autoApproveAtMs"] is None
        assert msg["blockedByRule"] == "bash_rm_any"

        # audit: 2 行（pending + rejected_auto_timeout）
        records = audit.read_all()
        assert len(records) == 2
        assert records[0]["decision"]["outcome"] == "pending"
        assert records[1]["decision"]["outcome"] == "rejected_auto_timeout"
        assert records[1]["decision"]["decided_by"] == "timeout"
        assert records[1]["rule_evaluation"]["matched"] == "bash_rm_any"

    async def test_dangerous_rule_user_accept_before_timeout(
        self,
        policy: AutoApprovalPolicy,
        audit: AuditLogger,
    ) -> None:
        """v1.0.1: 命中规则 + 用户在 timeout 前手动允许 → outcome=allowed_manual (取消 timer)。"""
        policy.set_enabled("/proj", True)
        bridge, _writer = _make_bridge(policy=policy, audit=audit)

        async def resolver() -> None:
            await asyncio.sleep(0.05)  # 远早于 200ms timeout
            bridge.resolve("toolu_acc", {"allow": True})

        asyncio.create_task(resolver())  # noqa: RUF006
        start = time.monotonic()
        result = await bridge.can_use_tool(
            "Bash",
            {"command": "rm -rf /tmp/x"},
            _make_ctx("toolu_acc"),
        )
        elapsed_ms = (time.monotonic() - start) * 1000

        assert isinstance(result, PermissionResultAllow)
        assert elapsed_ms < 150, f"user accept must precede timeout: {elapsed_ms}ms"

        records = audit.read_all()
        assert records[-1]["decision"]["outcome"] == "allowed_manual"
        assert records[-1]["decision"]["decided_by"] == "user"

    async def test_dangerous_rule_user_reject_before_timeout(
        self,
        policy: AutoApprovalPolicy,
        audit: AuditLogger,
    ) -> None:
        """v1.0.1: 命中规则 + 用户在 timeout 前手动拒绝 → outcome=blocked_then_manual。"""
        policy.set_enabled("/proj", True)
        bridge, _writer = _make_bridge(policy=policy, audit=audit)

        async def resolver() -> None:
            await asyncio.sleep(0.05)  # 远早于 200ms timeout
            bridge.resolve("toolu_d", {"allow": False, "message": "blocked"})

        asyncio.create_task(resolver())  # noqa: RUF006
        result = await bridge.can_use_tool(
            "Bash",
            {"command": "rm -rf /"},
            _make_ctx("toolu_d"),
        )
        assert isinstance(result, PermissionResultDeny)

        records = audit.read_all()
        assert records[-1]["decision"]["outcome"] == "blocked_then_manual"
        assert records[-1]["decision"]["decided_by"] == "user"


# ============================================================
# Audit 日志完整性
# ============================================================


class TestAuditCompleteness:
    async def test_request_then_decision_two_lines(
        self,
        policy: AutoApprovalPolicy,
        audit: AuditLogger,
    ) -> None:
        policy.set_enabled("/proj", True)
        bridge, _ = _make_bridge(policy=policy, audit=audit)

        async def resolver() -> None:
            await asyncio.sleep(0.02)
            bridge.resolve("toolu_a", {"allow": True})

        asyncio.create_task(resolver())  # noqa: RUF006
        await bridge.can_use_tool("Bash", {"command": "ls"}, _make_ctx("toolu_a"))

        records = audit.read_all()
        assert len(records) == 2
        # 共享字段一致
        assert records[0]["channel"] == records[1]["channel"] == "claude_code"
        assert records[0]["cwd"] == records[1]["cwd"] == "/proj"
        assert records[0]["tool_name"] == records[1]["tool_name"] == "Bash"
        assert records[0]["arguments"] == records[1]["arguments"] == {"command": "ls"}
        # rule_evaluation 一致
        assert records[0]["rule_evaluation"]["matched"] is None
        assert records[1]["rule_evaluation"]["matched"] is None

    async def test_audit_channel_field_present(
        self,
        policy: AutoApprovalPolicy,
        audit: AuditLogger,
    ) -> None:
        """v1 留位的 channel 字段必须真正写入。"""
        policy.set_enabled("/proj", True)
        bridge, _ = _make_bridge(policy=policy, audit=audit)

        async def resolver() -> None:
            await asyncio.sleep(0.02)
            bridge.resolve("toolu_c", {"allow": True})

        asyncio.create_task(resolver())  # noqa: RUF006
        await bridge.can_use_tool("Bash", {"command": "ls"}, _make_ctx("toolu_c"))

        # 直接读文件验证（不依赖 read_all）
        lines = audit.path.read_text().splitlines()
        for line in lines:
            rec = json.loads(line)
            assert rec["channel"] == "claude_code"

    async def test_audit_failure_does_not_block(
        self,
        policy: AutoApprovalPolicy,
        tmp_path: Path,
    ) -> None:
        """audit 写盘失败时主流程不应被影响。"""
        policy.set_enabled("/proj", True)

        class BrokenAudit(AuditLogger):
            def log_request(self, **kwargs: Any) -> None:  # type: ignore[override]
                raise OSError("disk full")

            def log_decision(self, **kwargs: Any) -> None:  # type: ignore[override]
                raise OSError("disk full")

        broken = BrokenAudit(tmp_path / "audit.jsonl")
        bridge, _ = _make_bridge(policy=policy, audit=broken)

        async def resolver() -> None:
            await asyncio.sleep(0.02)
            bridge.resolve("toolu_x", {"allow": True})

        asyncio.create_task(resolver())  # noqa: RUF006
        result = await bridge.can_use_tool("Bash", {"command": "ls"}, _make_ctx("toolu_x"))
        assert isinstance(result, PermissionResultAllow)  # 没被 audit 坏掉


# ============================================================
# set_active_cwd 透传
# ============================================================


class TestSetActiveCwd:
    async def test_cwd_override_at_call_time(
        self,
        policy: AutoApprovalPolicy,
    ) -> None:
        """service.query 通过 set_active_cwd 切换 cwd → policy 用新 cwd 决策。"""
        policy.set_enabled("/proj-a", True)
        # /proj-b 不启用

        bridge, writer = _make_bridge(policy=policy, cwd="/proj-a")

        async def resolver() -> None:
            await asyncio.sleep(0.02)
            bridge.resolve("toolu_p", {"allow": True})

        asyncio.create_task(resolver())  # noqa: RUF006
        await bridge.can_use_tool("Bash", {"command": "ls"}, _make_ctx("toolu_p"))
        # /proj-a 启用 → 有 autoApproveAtMs
        assert writer.sent[0]["autoApproveAtMs"] is not None

        # 现在切到 /proj-b（未启用）
        bridge.set_active_cwd("/proj-b")
        writer.sent.clear()

        async def resolver2() -> None:
            await asyncio.sleep(0.02)
            bridge.resolve("toolu_q", {"allow": True})

        asyncio.create_task(resolver2())  # noqa: RUF006
        await bridge.can_use_tool("Bash", {"command": "ls"}, _make_ctx("toolu_q"))
        # /proj-b 未启用 → 无 autoApproveAtMs
        assert writer.sent[0]["autoApproveAtMs"] is None


# ============================================================
# 兼容性：policy=None 走 v0.1 行为
# ============================================================


class TestNoPolicyBackwardsCompat:
    async def test_no_policy_no_extra_audit_no_timeout(self) -> None:
        """无 policy / 无 audit：等同 v0.1 老行为，emit 不带 autoApproveAtMs。"""
        bridge, writer = _make_bridge(policy=None, audit=None)

        async def resolver() -> None:
            await asyncio.sleep(0.02)
            bridge.resolve("toolu_x", {"allow": True})

        asyncio.create_task(resolver())  # noqa: RUF006
        await bridge.can_use_tool("Bash", {"command": "ls"}, _make_ctx("toolu_x"))

        msg = writer.sent[0]
        assert msg["autoApproveAtMs"] is None
        assert msg["blockedByRule"] is None
        assert msg["channel"] == "claude_code"
