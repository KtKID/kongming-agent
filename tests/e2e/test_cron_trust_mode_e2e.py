"""E2E（集成层）：cron 任务 trust 模式自动放行 explicit_consent 链路。

DoD#9 验收锚之一：验证从 ``schedule(action='create', approval_mode='trust')``
落盘 task → :class:`ExecutionBridge._build_approval_wrapper` 解析 effective
mode → :class:`ScheduleApprovalProvider` trust 自动放行 → 真实 audit.jsonl 物
理文件写入 ``run_approval_auto_allow`` 行。

退化说明：跑真实 LLM + 真实 ticker 的 live e2e 需要本地模型 + KONGMING_E2E_REAL_MODEL=1，
本测试退化为"集成测试" —— 用真 :class:`scheduler.store.Store`（写实际文件）+ 真
:class:`ExecutionBridge._build_approval_wrapper`（真实装配）+ 真
:class:`ScheduleApprovalProvider`（真决策）+ ``FakeInner`` 模拟最底层 inner provider
给定的 decision_class。覆盖范围：

1. ``schedule_tool.create(approval_mode='trust')`` 落盘 task.policy.approval_mode == TRUST
2. ``bridge._build_approval_wrapper(task)`` 解析 effective_mode == TRUST
3. wrapper 拦截 explicit_consent → 转 approved（outcome 改写、metadata 标 cron_trust_mode）
4. wrapper emit ``approval.cron.auto_allow`` event → ``_CronAuditWriterSink`` 转写
   → ``store.append_audit(action="run_approval_auto_allow")`` → 实际写入
   ``<home>/scheduler/audits.jsonl`` 文件
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from application.scheduled_runs.execution_bridge import ExecutionBridge
from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    ToolContext,
)
from scheduler.domain import ApprovalMode
from scheduler.store import Store
from tests.support.tool_calls import execute_prepared_tool
from tools.builtin.schedule_tool import ScheduleTool, build_schedule_tool


class _FakeThreadProvisioner:
    """为 schedule create 提供确定性的专属 thread。"""

    async def create_scheduled_task_thread(
        self,
        *,
        task_id: str,
        name: str,
        preset_id: str,
        cwd: str = "",
    ) -> str:
        del task_id, name, preset_id, cwd
        return "thread-bbbbbbbbbbbb"

    async def delete_thread(self, thread_id: str, *, keep_history: bool = False) -> None:
        del thread_id, keep_history


def _build_schedule_tool(store: Store) -> ScheduleTool:
    """构造带 scheduled-thread provisioner 的真实 schedule tool。"""
    return build_schedule_tool(store, thread_provisioner=_FakeThreadProvisioner())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeInner:
    """模拟最底层 :class:`core.contracts.ApprovalProvider`。

    返回固定 ``decision_class`` 的 :class:`ApprovalDecision`，让上层 wrapper
    走 hard_block / explicit_consent / silent_allow 不同分支。
    """

    decision_class: str
    decision_source: str = ""
    outcome: str = "rejected"
    matched_rule: str = "default:ask"
    reason: str = "test"

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        metadata: dict[str, Any] = {"decision_class": self.decision_class}
        if self.decision_source:
            metadata["decision_source"] = self.decision_source
        if self.matched_rule:
            metadata["matched_rule"] = self.matched_rule
        return ApprovalDecision(outcome=self.outcome, reason=self.reason, metadata=metadata)


def _make_request(tool_name: str = "run_shell") -> ApprovalRequest:
    return ApprovalRequest(
        run_id="r-trust-e2e",
        session_id="s-trust-e2e",
        turn=1,
        call_id="c-1",
        tool_name=tool_name,
        arguments={"cmd": "echo hello"},
    )


def _ctx() -> ToolContext:
    return ToolContext(
        run_id="run-tool-e2e",
        session_id="sess-tool-e2e",
        turn=1,
        call_id="call-1",
    )


def _build_real_bridge(*, store: Store, inner: FakeInner) -> ExecutionBridge:
    """真实 :class:`ExecutionBridge`，只有 runner/llm/session_factory/agent_spec
    用 ``MagicMock`` —— 因为我们只调 ``_build_approval_wrapper`` + wrapper.decide。
    """
    return ExecutionBridge(
        runtime=MagicMock(),
        llm=MagicMock(),
        tools={},
        enabled_tool_names=(),
        inner_approval=inner,
        session_factory=lambda sid: MagicMock(),
        event_sinks=[],
        agent_spec=MagicMock(),
        store=store,
        base_config=None,
    )


# ---------------------------------------------------------------------------
# E2E 用例
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trust_create_then_wrapper_auto_allows_consent_and_writes_audit(
    tmp_path: Path,
) -> None:
    """主路径：trust 任务 → wrapper 自动放行 explicit_consent → audit.jsonl 落盘。

    步骤：
    1. 用 schedule_tool 创建 ``approval_mode='trust'`` 任务（真 Store 写盘）
    2. 从 store 反读 task 验证 ``task.policy.approval_mode is ApprovalMode.TRUST``
    3. 真实 ExecutionBridge._build_approval_wrapper(task) → effective_mode == TRUST
    4. wrapper.decide(consent request) → outcome 改写为 approved + cron_trust_mode metadata
    5. 直接读 audits.jsonl 物理文件验证 ``run_approval_auto_allow`` 行已写入
    """
    # 步骤 1: schedule_tool create approval_mode=trust
    store = Store(home_dir=tmp_path / "cron")
    tool = _build_schedule_tool(store)
    result = await execute_prepared_tool(
        tool,
        {
            "action": "create",
            "name": "trust e2e task",
            "schedule": "every 60s",
            "agent": "default",
            "input": "noop",
            "approval_mode": "trust",
        },
        _ctx(),
    )
    assert result.ok is True
    assert result.data is not None
    task_id = result.data["task_id"]
    assert result.data["approval_mode"] == "trust"

    # 步骤 2: 真 store 反读，验证 task.policy.approval_mode 落盘
    task = store.get_task(task_id)
    assert task is not None
    assert task.policy.approval_mode is ApprovalMode.TRUST

    # 步骤 3+4: 真实 bridge 装配 wrapper + 真实决策
    inner = FakeInner(
        decision_class="explicit_consent",
        decision_source="standard",
        outcome="rejected",
        matched_rule="default:ask",
    )
    bridge = _build_real_bridge(store=store, inner=inner)
    wrapper = bridge._build_approval_wrapper(task)
    # effective_mode 应被解析为 TRUST（task 显式声明）
    assert wrapper.mode is ApprovalMode.TRUST

    decision = await wrapper.decide(_make_request())

    # wrapper 自动放行：inner rejected → approved
    assert decision.outcome == "approved"
    assert decision.metadata.get("cron_trust_mode") is True
    assert decision.metadata.get("decision_source") == "cron_trust"
    assert decision.metadata.get("cron_task_id") == task_id
    # 原始决策痕迹保留
    assert decision.metadata.get("original_decision_class") == "explicit_consent"
    assert decision.metadata.get("original_outcome") == "rejected"

    # 步骤 5: 验证 audit.jsonl 物理文件有 run_approval_auto_allow 行
    audits = store.list_audits(task_id=task_id)
    auto_allow_audits = [a for a in audits if a["action"] == "run_approval_auto_allow"]
    assert len(auto_allow_audits) == 1, (
        f"expected 1 run_approval_auto_allow audit, got {len(auto_allow_audits)}; "
        f"all audits: {[a['action'] for a in audits]}"
    )
    payload = auto_allow_audits[0]["payload"]
    assert payload["tool_name"] == "run_shell"
    assert payload["original_decision_class"] == "explicit_consent"
    assert payload["original_decision_source"] == "standard"
    assert payload["matched_rule"] == "default:ask"
    assert isinstance(payload["arguments_digest"], str)
    assert payload["arguments_digest"].startswith("sha256:")

    # 直接读底层 jsonl 文件，验证审计是真的落盘了（不只是 list_audits API 假数据）
    # Store 把 audits.jsonl 直接放在 home_dir 根下（见 src/scheduler/store.py:400）
    audits_path = tmp_path / "cron" / "audits.jsonl"
    assert audits_path.exists(), f"audits.jsonl missing at {audits_path}"
    lines = audits_path.read_text(encoding="utf-8").strip().split("\n")
    raw_records = [json.loads(line) for line in lines if line.strip()]
    auto_allow_raw = [r for r in raw_records if r.get("action") == "run_approval_auto_allow"]
    assert len(auto_allow_raw) == 1


@pytest.mark.asyncio
async def test_trust_mode_silent_allow_passthrough_no_audit(tmp_path: Path) -> None:
    """旁路验证：trust 模式下 silent_allow 透传，不写 audit（避免误报）。

    确保只有 explicit_consent 自动放行才写 audit，其他 decision_class 透传时
    不污染审计流。
    """
    store = Store(home_dir=tmp_path / "cron")
    tool = _build_schedule_tool(store)
    result = await execute_prepared_tool(
        tool,
        {
            "action": "create",
            "name": "trust silent",
            "schedule": "every 60s",
            "agent": "default",
            "input": "noop",
            "approval_mode": "trust",
        },
        _ctx(),
    )
    assert result.ok
    task_id = result.data["task_id"]
    task = store.get_task(task_id)
    assert task is not None

    inner = FakeInner(decision_class="silent_allow", outcome="approved")
    bridge = _build_real_bridge(store=store, inner=inner)
    wrapper = bridge._build_approval_wrapper(task)

    decision = await wrapper.decide(_make_request())
    # silent_allow 透传不进 trust 分支
    assert decision.outcome == "approved"
    assert decision.metadata.get("cron_trust_mode") is not True

    audits = store.list_audits(task_id=task_id)
    auto_allow_audits = [a for a in audits if a["action"] == "run_approval_auto_allow"]
    assert auto_allow_audits == []
