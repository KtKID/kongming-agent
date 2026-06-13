"""Deep Research 子 agent task log 单元测试。

本脚本验证 task.log.jsonl、subagent.json 索引和 workflow audit 事件。
作用是把 Deep Research 每个子 agent task 必须可审计落地的要求固定为可重复测试。
关键执行流程：创建 DeepResearchTaskLogWriter，调用 start/complete/fail，断言日志、索引和 audit payload。
关键函数：_AuditRecorder 收集事件，_read_jsonl 读取日志，test_* 覆盖完成和失败路径。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from application.agent_workflows.strategies.deep_research import DeepResearchTaskLogWriter


class _AuditRecorder:
    """测试用 audit writer，记录 workflow audit 事件。"""

    def __init__(self) -> None:
        """初始化 recorder，输入为空，输出为可记录事件的实例。"""
        self.events: list[dict[str, object]] = []

    def write_event(self, event: dict[str, object]) -> None:
        """记录 audit 事件，输入为事件 dict，输出为追加到内存。"""
        self.events.append(event)


def test_task_log_writer_records_start_and_completion(tmp_path: Path) -> None:
    """验证完成路径，输入为 task 上下文，输出为 task log、subagent.json 和 audit 索引。"""
    workflow_dir = tmp_path / "workflow-1"
    audit = _AuditRecorder()
    writer = DeepResearchTaskLogWriter(workflow_dir=workflow_dir, audit_writer=audit)

    task_log_path = writer.start_task(
        task_run_id="extractor-1",
        phase="extract",
        role="extractor",
        input_artifacts=["deep_research/sources.fetched.jsonl"],
        prompt_hash="sha256:abc",
        tool_allowlist=["read_file"],
        budget_snapshot={"max_turns": 3, "timeout_seconds": 120},
        child_session_log_path=str(tmp_path / "sessions" / "child.jsonl"),
    )
    writer.complete_task(
        task_run_id="extractor-1",
        phase="extract",
        role="extractor",
        output_artifacts=["deep_research/facts.raw.jsonl"],
    )

    events = _read_jsonl(task_log_path)
    subagent = json.loads((workflow_dir / "agents" / "extractor-1" / "subagent.json").read_text())
    actions = {str(event["action"]) for event in audit.events}

    assert [event["event"] for event in events] == ["started", "completed"]
    assert subagent["task_log_path"] == str(task_log_path)
    assert subagent["deep_research_phase"] == "extract"
    assert subagent["deep_research_role"] == "extractor"
    assert subagent["deep_research_task_status"] == "completed"
    assert "deep_research.subagent_task_started" in actions
    assert "deep_research.subagent_task_completed" in actions


def test_task_log_writer_records_failure(tmp_path: Path) -> None:
    """验证失败路径，输入为错误摘要，输出为 failed log 和 audit 事件。"""
    workflow_dir = tmp_path / "workflow-2"
    audit = _AuditRecorder()
    writer = DeepResearchTaskLogWriter(workflow_dir=workflow_dir, audit_writer=audit)

    writer.start_task(task_run_id="juror-1", phase="crosscheck", role="juror")
    writer.fail_task(
        task_run_id="juror-1",
        phase="crosscheck",
        role="juror",
        error_digest="model timeout",
    )

    events = _read_jsonl(workflow_dir / "agents" / "juror-1" / "task.log.jsonl")
    subagent = json.loads((workflow_dir / "agents" / "juror-1" / "subagent.json").read_text())
    actions = {str(event["action"]) for event in audit.events}

    assert events[-1]["event"] == "failed"
    assert events[-1]["error_digest"] == "model timeout"
    assert subagent["deep_research_task_status"] == "failed"
    assert "deep_research.subagent_task_failed" in actions


@pytest.mark.parametrize(
    "task_run_id",
    ["../escape", "nested/id", "nested\\id", "", ".", "phase-\x00x", "phase\nx", "phase x"],
)
def test_task_log_writer_rejects_unsafe_task_run_id(
    tmp_path: Path,
    task_run_id: str,
) -> None:
    """验证 task_run_id 路径边界，输入为不安全 ID，输出为拒绝写入。"""
    writer = DeepResearchTaskLogWriter(workflow_dir=tmp_path / "workflow-3")

    with pytest.raises(ValueError, match="task_run_id"):
        writer.start_task(task_run_id=task_run_id, phase="extract", role="extractor")


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    """读取 JSONL 日志，输入为路径，输出为对象列表。"""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
