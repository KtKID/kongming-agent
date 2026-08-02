"""E2E（集成层）：cron trust 模式下 hard_block 仍永远不可绕。

DoD#9 验收锚之二：trust 是"信任 explicit_consent"，但 ``hard_block`` 是**安全
不变量**——即使任务声明 ``approval_mode='trust'``，命中 hard_block 规则的工具
调用也必须被拒绝，且**不写** ``run_approval_auto_allow`` audit（hard_block 路径
不进 trust 自动放行分支）。

退化说明：与 :file:`test_cron_trust_mode_e2e.py` 同样的集成层退化——真 Store + 真
bridge + 真 wrapper + ``FakeInner`` 模拟最底层。本测试关键断言：

1. wrapper.decide → outcome 仍为 ``rejected``（不被 trust 改写）
2. metadata 不带 ``cron_trust_mode``（说明走的不是 trust 分支）
3. ``audits.jsonl`` 物理文件**不含** ``run_approval_auto_allow`` 行
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
        return "thread-aaaaaaaaaaaa"

    async def delete_thread(self, thread_id: str, *, keep_history: bool = False) -> None:
        del thread_id, keep_history


def _build_schedule_tool(store: Store) -> ScheduleTool:
    """构造带 scheduled-thread provisioner 的真实 schedule tool。"""
    return build_schedule_tool(store, thread_provisioner=_FakeThreadProvisioner())


@dataclass
class FakeInner:
    """模拟最底层 :class:`core.contracts.ApprovalProvider`，固定 decision_class。"""

    decision_class: str
    decision_source: str = ""
    outcome: str = "rejected"
    matched_rule: str = "shell.hard_block.rm_rf"
    reason: str = "destructive command blocked"

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        metadata: dict[str, Any] = {"decision_class": self.decision_class}
        if self.decision_source:
            metadata["decision_source"] = self.decision_source
        if self.matched_rule:
            metadata["matched_rule"] = self.matched_rule
        return ApprovalDecision(outcome=self.outcome, reason=self.reason, metadata=metadata)


def _make_dangerous_request() -> ApprovalRequest:
    """模拟一个命中 hard_block 的 shell 调用（如 ``rm -rf /``）。"""
    return ApprovalRequest(
        run_id="r-hardblock-e2e",
        session_id="s-hardblock-e2e",
        turn=1,
        call_id="c-1",
        tool_name="run_shell",
        arguments={"cmd": "rm -rf /tmp/abc"},
    )


def _ctx() -> ToolContext:
    return ToolContext(
        run_id="run-tool-hb",
        session_id="sess-tool-hb",
        turn=1,
        call_id="call-1",
    )


def _build_real_bridge(
    *,
    store: Store,
    inner: FakeInner,
    base_config: object | None = None,
) -> ExecutionBridge:
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
        base_config=base_config,
    )


def _config_with_global_mode(mode: str) -> object:
    """构造最小 Config，把 scheduler.approval.mode 设为 mode。供需要显式覆盖兜底的测试用。"""
    from infrastructure.config.models import (
        Config,
        ModelSelectionConfig,
        SchedulerApprovalConfig,
        SchedulerConfig,
    )

    return Config(
        model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"),
        scheduler=SchedulerConfig(
            approval=SchedulerApprovalConfig(mode=mode),  # type: ignore[arg-type]
        ),
    )


@pytest.mark.asyncio
async def test_hard_block_in_trust_still_rejected_no_audit(tmp_path: Path) -> None:
    """trust 模式 + hard_block → 仍 rejected + 不写 run_approval_auto_allow。

    步骤：
    1. 创建 ``approval_mode='trust'`` 任务
    2. 模拟 inner 给 ``hard_block`` decision_class
    3. wrapper.decide → 透传 rejected（cron_trust_mode 标记不出现）
    4. audits.jsonl 物理文件不含 ``run_approval_auto_allow`` 行
    """
    store = Store(home_dir=tmp_path / "cron")
    tool = _build_schedule_tool(store)
    result = await execute_prepared_tool(
        tool,
        {
            "action": "create",
            "name": "trust hardblock e2e",
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
    assert task.policy.approval_mode is ApprovalMode.TRUST

    inner = FakeInner(decision_class="hard_block", outcome="rejected")
    bridge = _build_real_bridge(store=store, inner=inner)
    wrapper = bridge._build_approval_wrapper(task)
    # 即便是 trust 模式，wrapper.mode 解析仍为 TRUST，但 hard_block 走透传分支
    assert wrapper.mode is ApprovalMode.TRUST

    decision = await wrapper.decide(_make_dangerous_request())

    # hard_block 永远透传 rejected——安全不变量
    assert decision.outcome == "rejected"
    # 不能带 trust 元数据（说明没走 trust 分支）
    assert decision.metadata.get("cron_trust_mode") is not True
    assert "cron_task_id" not in decision.metadata  # 透传 inner decision

    # 验证：audit.jsonl 物理文件不能出现 run_approval_auto_allow
    audits = store.list_audits(task_id=task_id)
    auto_allow_audits = [a for a in audits if a["action"] == "run_approval_auto_allow"]
    assert auto_allow_audits == [], (
        f"hard_block path must NOT write run_approval_auto_allow audit, got {auto_allow_audits}"
    )

    # 直接读 jsonl 文件，进一步确认（防 list_audits 实现 bug 误判）
    # Store 把 audits.jsonl 直接放在 home_dir 根下（见 src/scheduler/store.py:400）
    audits_path = tmp_path / "cron" / "audits.jsonl"
    if audits_path.exists():
        lines = audits_path.read_text(encoding="utf-8").strip().split("\n")
        raw_records = [json.loads(line) for line in lines if line.strip()]
        auto_allow_raw = [r for r in raw_records if r.get("action") == "run_approval_auto_allow"]
        assert auto_allow_raw == [], (
            f"hard_block path leaked run_approval_auto_allow into audits.jsonl: {auto_allow_raw}"
        )


@pytest.mark.asyncio
async def test_fail_closed_opt_in_via_global_config_still_works(
    tmp_path: Path,
) -> None:
    """对照：用户能 opt-in 严格模式——显式 cfg.scheduler.approval.mode='fail_closed' 仍可用。

    v0.5 调整后默认 trust（cron 即用户预批准任务），但保留 fail_closed 给少数
    需要严格审批的部署。本测试证明：显式构造 base_config 设 fail_closed 后，
    缺省 approval_mode 的任务仍走 fail_closed 路径（consent → rejected）。
    """
    store = Store(home_dir=tmp_path / "cron")
    tool = _build_schedule_tool(store)
    result = await execute_prepared_tool(
        tool,
        {
            "action": "create",
            "name": "no mode task",
            "schedule": "every 60s",
            "agent": "default",
            "input": "noop",
        },
        _ctx(),
    )
    assert result.ok
    task_id = result.data["task_id"]
    task = store.get_task(task_id)
    assert task is not None
    assert task.policy.approval_mode is None  # 缺省

    inner = FakeInner(
        decision_class="explicit_consent",
        decision_source="standard",
        outcome="rejected",
        matched_rule="default:ask",
    )
    # 显式构造 base_config 设 fail_closed（opt-in 严格模式）
    bridge = _build_real_bridge(
        store=store,
        inner=inner,
        base_config=_config_with_global_mode("fail_closed"),
    )
    wrapper = bridge._build_approval_wrapper(task)
    assert wrapper.mode is ApprovalMode.FAIL_CLOSED

    decision = await wrapper.decide(
        ApprovalRequest(
            run_id="r",
            session_id="s",
            turn=1,
            call_id="c",
            tool_name="run_shell",
            arguments={"cmd": "echo hi"},
        )
    )
    # fail_closed 路径：consent 转 rejected + cron_fail_closed metadata
    assert decision.outcome == "rejected"
    assert decision.metadata.get("cron_fail_closed") is True

    # 不写 run_approval_auto_allow
    audits = store.list_audits(task_id=task_id)
    auto_allow_audits = [a for a in audits if a["action"] == "run_approval_auto_allow"]
    assert auto_allow_audits == []


@pytest.mark.asyncio
async def test_default_trust_consent_auto_allowed_when_no_config(
    tmp_path: Path,
) -> None:
    """v0.5 新设计：缺省 approval_mode + 无 base_config → 兜底 trust → consent 自动放行。

    cron 任务的本质就是用户预批准的自动执行；不强迫用户每次都显式声明 'trust'。
    HardBlock 仍兜底真高危（见同文件 test_hard_block_in_trust_still_rejected_no_audit）。
    """
    store = Store(home_dir=tmp_path / "cron")
    tool = _build_schedule_tool(store)
    result = await execute_prepared_tool(
        tool,
        {
            "action": "create",
            "name": "no mode task",
            "schedule": "every 60s",
            "agent": "default",
            "input": "noop",
        },
        _ctx(),
    )
    assert result.ok
    task = store.get_task(result.data["task_id"])
    assert task is not None
    assert task.policy.approval_mode is None  # 缺省

    inner = FakeInner(
        decision_class="explicit_consent",
        decision_source="standard",
        outcome="rejected",
        matched_rule="default:ask",
    )
    bridge = _build_real_bridge(store=store, inner=inner, base_config=None)
    wrapper = bridge._build_approval_wrapper(task)
    # base_config=None 兜底 trust（v0.5 调整）
    assert wrapper.mode is ApprovalMode.TRUST

    decision = await wrapper.decide(
        ApprovalRequest(
            run_id="r",
            session_id="s",
            turn=1,
            call_id="c",
            tool_name="run_shell",
            arguments={"cmd": "echo hi"},
        )
    )
    assert decision.outcome == "approved"
    assert decision.metadata["cron_trust_mode"] is True
