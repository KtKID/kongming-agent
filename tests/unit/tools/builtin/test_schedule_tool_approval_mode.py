"""Unit tests for schedule_tool approval_mode 入参（v0.5）。

3 用例：

- T1: create 时传 ``approval_mode='trust'`` → 落盘 ``task.policy.approval_mode == TRUST``
- T2: create 时缺省 ``approval_mode`` → ``task.policy.approval_mode is None``（走全局兜底）
- T3: create 时传 ``approval_mode='invalid'`` → ``ToolResult.ok=False`` + error_message 含 invalid

测试约束：
- 用 ``tmp_path`` 隔离 cron home（参考 tests/unit/test_schedule_tool.py 风格）
- 不调真 LLM；不依赖 :mod:`safety.*`
"""

from __future__ import annotations

from pathlib import Path

from core.contracts import ToolContext, ToolResult
from scheduler.domain import ApprovalMode
from scheduler.store import Store
from tools.builtin.schedule_tool import ScheduleTool, build_schedule_tool


def _ctx() -> ToolContext:
    return ToolContext(
        run_id="run-approval-mode",
        session_id="sess-approval-mode",
        turn=1,
        call_id="call-1",
    )


class _FakeThreadProvisioner:
    """测试用 provisioner：让 create 路径满足专属 thread 合同。"""

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


def _make_tool(tmp_path: Path) -> tuple[ScheduleTool, Store]:
    store = Store(home_dir=tmp_path / "cron")
    tool = build_schedule_tool(store, thread_provisioner=_FakeThreadProvisioner())
    return tool, store


# ---------------------------------------------------------------------------
# T1 / T2 / T3
# ---------------------------------------------------------------------------


async def test_t1_create_with_trust_persists_approval_mode(tmp_path: Path) -> None:
    """T1: schedule(action=create, approval_mode='trust') 落盘 task.policy.approval_mode == TRUST。"""
    tool, store = _make_tool(tmp_path)
    r: ToolResult = await tool.execute(
        {
            "action": "create",
            "name": "trust task",
            "schedule": "every 30s",
            "agent": "default",
            "input": "noop",
            "approval_mode": "trust",
        },
        _ctx(),
    )
    assert r.ok is True, r.content
    assert r.data is not None
    task_id = r.data["task_id"]
    # ToolResult.data 暴露 approval_mode 字符串
    assert r.data["approval_mode"] == "trust"
    # content 文本里也带这一行
    assert "approval_mode: trust" in r.content

    # 落盘验证：store 中读出的 task.policy.approval_mode 是 ApprovalMode.TRUST
    task = store.get_task(task_id)
    assert task is not None
    assert task.policy.approval_mode is ApprovalMode.TRUST

    # audit create 行 payload 带 approval_mode='trust'
    audits = store.list_audits(task_id=task_id)
    create_audits = [a for a in audits if a["action"] == "create"]
    assert len(create_audits) == 1
    assert create_audits[0]["payload"].get("approval_mode") == "trust"

    # list 输出也带 approval_mode
    list_result = await tool.execute({"action": "list"}, _ctx())
    assert list_result.ok
    assert list_result.data is not None
    listed = [t for t in list_result.data["tasks"] if t["task_id"] == task_id]
    assert len(listed) == 1
    assert listed[0]["approval_mode"] == "trust"


async def test_t2_create_without_approval_mode_defaults_to_none(tmp_path: Path) -> None:
    """T2: 缺省 approval_mode → task.policy.approval_mode is None（走全局）。"""
    tool, store = _make_tool(tmp_path)
    r = await tool.execute(
        {
            "action": "create",
            "name": "no mode task",
            "schedule": "every 30s",
            "agent": "default",
            "input": "noop",
        },
        _ctx(),
    )
    assert r.ok is True, r.content
    assert r.data is not None
    task_id = r.data["task_id"]
    # data 里 approval_mode 字段存在但是 None
    assert r.data["approval_mode"] is None
    # content 不带 approval_mode 行（不显式声明就不打印，避免噪音）
    assert "approval_mode:" not in r.content

    task = store.get_task(task_id)
    assert task is not None
    assert task.policy.approval_mode is None

    # audit create 行 payload 的 approval_mode = None
    audits = store.list_audits(task_id=task_id)
    create_audits = [a for a in audits if a["action"] == "create"]
    assert len(create_audits) == 1
    assert create_audits[0]["payload"].get("approval_mode") is None


async def test_t3_create_with_invalid_approval_mode_returns_error(tmp_path: Path) -> None:
    """T3: approval_mode='invalid' → ToolResult.ok=False + error_message 含 invalid。"""
    tool, store = _make_tool(tmp_path)
    r = await tool.execute(
        {
            "action": "create",
            "name": "bad mode task",
            "schedule": "every 30s",
            "agent": "default",
            "input": "noop",
            "approval_mode": "invalid",
        },
        _ctx(),
    )
    assert r.ok is False
    assert r.error_message is not None
    assert "invalid" in r.error_message
    # 不应有任何 task 被写入
    assert store.list_tasks(include_disabled=True) == []
