from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.contracts import ApprovalDecision, ApprovalRequest, ToolContext
from core.message import Message, ToolCall
from core.run_state import RunState
from executors.agent_runtime.subagent_permissions import (
    SCOPED_WORKDIR_MODE,
    ScopedFileTool,
    SubAgentApprovalProvider,
    SubAgentGrant,
    SubAgentToolAuditHook,
)
from tools.file_tools import build_file_tools


class _Writer:
    def __init__(self, tmp_path: Path) -> None:
        self.audit_log_path = tmp_path / "audit.jsonl"
        self.events: list[dict[str, Any]] = []

    def write_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def write_subagent_creation(self, record: object) -> None:
        raise AssertionError("not needed in this test")


class _UpstreamApproval:
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(outcome="approved", reason="upstream")


def _grant(tmp_path: Path) -> SubAgentGrant:
    task_run_dir = tmp_path / "agents" / "001-test"
    working_dir = task_run_dir / "work"
    working_dir.mkdir(parents=True)
    return SubAgentGrant(
        grant_id="grant-test",
        workflow_id="wf-test",
        parent_session_id="parent",
        task_id="agent-1",
        task_run_id="001-test",
        task_name="test",
        session_id="child",
        task_run_dir=task_run_dir,
        working_dir=working_dir,
        workflow_dir=tmp_path,
        allowed_tools=frozenset({"read_file", "write_file", "list_dir"}),
        allowed_skills=frozenset(),
        created_at="2026-06-06T00:00:00+00:00",
    )


def _wrapped_tools(grant: SubAgentGrant) -> dict[str, ScopedFileTool]:
    return {tool.name: ScopedFileTool(inner_tool=tool, grant=grant) for tool in build_file_tools()}


@pytest.mark.asyncio
async def test_scoped_file_tool_rewrites_relative_paths_for_read_write_list(
    tmp_path: Path,
) -> None:
    grant = _grant(tmp_path)
    tools = _wrapped_tools(grant)
    ctx = ToolContext(run_id="run", session_id="child", turn=1, call_id="call")

    written = await tools["write_file"].execute(
        {"path": "result.txt", "content": "ok"},
        ctx,
    )
    assert written.ok is True
    assert (grant.working_dir / "result.txt").read_text(encoding="utf-8") == "ok"

    read = await tools["read_file"].execute({"path": "result.txt"}, ctx)
    assert read.ok is True
    assert read.content == "ok"
    assert read.data is not None
    assert read.data["path"] == str(grant.working_dir / "result.txt")

    listed = await tools["list_dir"].execute({"path": "."}, ctx)
    assert listed.ok is True
    assert listed.data is not None
    assert listed.data["path"] == str(grant.working_dir)
    assert [entry["name"] for entry in listed.data["entries"]] == ["result.txt"]


@pytest.mark.asyncio
async def test_scoped_file_tool_rejects_parent_and_symlink_escape(tmp_path: Path) -> None:
    grant = _grant(tmp_path)
    tools = _wrapped_tools(grant)
    ctx = ToolContext(run_id="run", session_id="child", turn=1, call_id="call")

    parent_escape = await tools["write_file"].execute(
        {"path": "../outside.txt", "content": "bad"},
        ctx,
    )
    assert parent_escape.ok is False
    assert not (grant.task_run_dir / "outside.txt").exists()

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (grant.working_dir / "link").symlink_to(outside_dir, target_is_directory=True)
    symlink_escape = await tools["write_file"].execute(
        {"path": "link/escape.txt", "content": "bad"},
        ctx,
    )
    assert symlink_escape.ok is False
    assert not (outside_dir / "escape.txt").exists()


@pytest.mark.asyncio
async def test_subagent_approval_provider_audits_approved_and_rejected_paths(
    tmp_path: Path,
) -> None:
    grant = _grant(tmp_path)
    writer = _Writer(tmp_path)
    approval = SubAgentApprovalProvider(
        grant=grant,
        audit_writer=writer,
        upstream=_UpstreamApproval(),
    )

    approved = await approval.decide(
        ApprovalRequest(
            run_id="run",
            session_id="child",
            turn=1,
            call_id="call-ok",
            tool_name="write_file",
            arguments={"path": "result.txt", "content": "ok"},
        )
    )
    rejected = await approval.decide(
        ApprovalRequest(
            run_id="run",
            session_id="child",
            turn=1,
            call_id="call-bad",
            tool_name="write_file",
            arguments={"path": "../outside.txt", "content": "bad"},
        )
    )

    assert approved.outcome == "approved"
    assert rejected.outcome == "rejected"
    payloads = [event["payload"] for event in writer.events]
    assert [payload["decision"] for payload in payloads] == ["approved", "rejected"]
    assert [payload["decision_source"] for payload in payloads] == [
        "grant_allow",
        "scope_deny",
    ]
    assert all(payload["action"] == "subagent_approval_decided" for payload in payloads)


@pytest.mark.asyncio
async def test_subagent_tool_audit_hook_records_not_registered_raw_args(
    tmp_path: Path,
) -> None:
    grant = _grant(tmp_path)
    writer = _Writer(tmp_path)
    hook = SubAgentToolAuditHook(grant=grant, audit_writer=writer)
    state = RunState(run_id="run-child-1", session_id="child", turn=1)
    call = ToolCall(
        call_id="call-hallucinated",
        tool_name="missing_tool",
        arguments={"path": "../outside.txt", "x": 1},
    )

    await hook.before_tool(state, call)
    await hook.after_tool(
        state,
        call,
        Message.tool_result(
            "call-hallucinated",
            json.dumps({"error": "missing"}),
            name="missing_tool",
            metadata={
                "ok": False,
                "error_message": "tool 'missing_tool' not registered",
            },
        ),
    )

    assert len(writer.events) == 1
    payload = writer.events[0]["payload"]
    assert payload["decision"] == "rejected"
    assert payload["decision_source"] == "not_registered"
    assert payload["raw_args"] == {"path": "../outside.txt", "x": 1}
    assert payload["target_path"] == "../outside.txt"


def test_scoped_permission_mode_constant_is_stable() -> None:
    assert SCOPED_WORKDIR_MODE == "scoped_workdir"
