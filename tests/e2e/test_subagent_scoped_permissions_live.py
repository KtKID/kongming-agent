"""Live e2e for scoped sub-agent file permissions.

Opt in with:

    KONGMING_E2E_REAL_MODEL=1 uv run pytest tests/e2e/test_subagent_scoped_permissions_live.py -v -s
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from cli.main import _apply_model_preset_or_exit
from config_loader import load_config
from core.agent_spec import AgentSpec
from executors.agent_runtime.agent_workflow_manager import AgentWorkflowManager
from executors.agent_runtime.native_runtime import NativeRuntime
from executors.agent_runtime.subagent_manager import SubAgentManager, SubAgentTask
from executors.agent_runtime.subagent_permissions import SubAgentPermissionSpec
from tools import AutoAllowApproval, ToolRegistry, build_file_tools

pytestmark = pytest.mark.skipif(
    os.getenv("KONGMING_E2E_REAL_MODEL") != "1",
    reason="set KONGMING_E2E_REAL_MODEL=1 to run real MiniMax M3 e2e",
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _audit_records(workflow_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (workflow_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def _approval_records(workflow_dir: Path) -> list[dict[str, Any]]:
    return [
        record["payload"]
        for record in _audit_records(workflow_dir)
        if record["action"] == "subagent_approval_decided"
    ]


@pytest.mark.asyncio
async def test_minimax_m3_scoped_subagent_allows_workdir_read_write_and_rejects_escape(
    tmp_path: Path,
) -> None:
    base_cfg = load_config(Path("config/setting.yaml"))
    if not os.getenv("MINIMAX_API_KEY"):
        pytest.skip("MINIMAX_API_KEY is required for minimax-m3 preset")

    cfg = _apply_model_preset_or_exit(base_cfg, "minimax-m3")
    session_root = os.getenv("KONGMING_E2E_SESSION_FILE_STORE_PATH")
    file_store_path = Path(session_root).resolve() if session_root else tmp_path / "sessions"
    workspace_root = _PROJECT_ROOT if session_root else tmp_path
    cfg = cfg.model_copy(
        update={
            "session": cfg.session.model_copy(update={"file_store_path": str(file_store_path)}),
            "trace": cfg.trace.model_copy(update={"raw_llm": False}),
        }
    )
    registry = ToolRegistry(build_file_tools())
    runtime = NativeRuntime.build(
        cfg,
        approval=AutoAllowApproval(),
        tools=registry,
        enabled_tool_names=["read_file", "write_file", "list_dir"],
        agent_spec=AgentSpec(
            name="parent-e2e",
            instructions="You are a test parent agent.",
            default_model=cfg.model.name,
            tool_names=(),
            max_turns=5,
            reasoning_effort=cfg.model.reasoning_effort,
        ),
    )
    manager = AgentWorkflowManager(
        subagents=SubAgentManager(runtime),
        config=cfg,
        workspace_root=workspace_root,
    )

    try:
        result = await manager.run_parallel(
            parent_session_id="live-minimax-scoped-permissions",
            tasks=[
                SubAgentTask(
                    task_id="inside-rw",
                    task_name="inside read write",
                    prompt=(
                        "你必须使用工具完成验证。"
                        "第一步调用 write_file，在相对路径 inside.txt 写入 EXACT-INSIDE-OK。"
                        "第二步调用 read_file 读取相对路径 inside.txt。"
                        "工具完成后，用一句话报告读到的内容。"
                    ),
                    tool_names=("write_file", "read_file"),
                    permission=SubAgentPermissionSpec(mode="scoped_workdir"),
                ),
                SubAgentTask(
                    task_id="outside-deny",
                    task_name="outside denied",
                    prompt=(
                        "你正在执行路径处理回归用例。"
                        "请按顺序调用工具并报告工具返回结果，不要根据路径字符串提前推断结果。"
                        "第一步调用 read_file，path 使用 ../outside-read.txt。"
                        "第二步调用 write_file，path 使用 ../escape.txt，content 使用 SHOULD-NOT-EXIST。"
                        "两个工具返回后，用一句话报告实际工具结果。"
                    ),
                    tool_names=("read_file", "write_file"),
                    permission=SubAgentPermissionSpec(mode="scoped_workdir"),
                ),
            ],
        )
    finally:
        await runtime.aclose()

    inside_run = next(run for run in result.runs if run.task.task_id == "inside-rw")
    outside_run = next(run for run in result.runs if run.task.task_id == "outside-deny")
    inside_workdir = Path(str(inside_run.task.metadata["working_dir"]))
    outside_task_dir = Path(str(outside_run.task.metadata["task_run_dir"]))

    assert result.workflow_dir.is_dir()
    assert result.completed is True
    assert (inside_workdir / "inside.txt").read_text(encoding="utf-8") == "EXACT-INSIDE-OK"
    assert not (outside_task_dir / "escape.txt").exists()

    approvals = _approval_records(result.workflow_dir)
    approved = [record for record in approvals if record["decision"] == "approved"]
    rejected = [record for record in approvals if record["decision"] == "rejected"]

    assert {
        (record["tool_name"], record["decision_source"])
        for record in approved
        if record["task_run_id"] == "001-inside-rw"
    } >= {("write_file", "grant_allow"), ("read_file", "grant_allow")}
    print(f"workflow_dir={result.workflow_dir}")
    print(f"inside_workdir={inside_workdir}")
    print(f"inside_file={inside_workdir / 'inside.txt'}")
    print(f"outside_escape_exists={(outside_task_dir / 'escape.txt').exists()}")
    print("approval_records=")
    for record in approvals:
        print(
            json.dumps(
                {
                    "task_run_id": record["task_run_id"],
                    "tool_name": record["tool_name"],
                    "target_path": record["target_path"],
                    "resolved_path": record["resolved_path"],
                    "decision": record["decision"],
                    "decision_source": record["decision_source"],
                    "reason": record["reason"],
                },
                ensure_ascii=False,
            )
        )

    assert any(
        record["target_path"] in {"../outside-read.txt", "../escape.txt"}
        and record["decision_source"] == "scope_deny"
        and "outside subagent working_dir" in record["reason"]
        for record in rejected
    ), outside_run.content
