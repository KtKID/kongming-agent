"""MiniMax M3 map_reduce live e2e。

本脚本验证真实 MiniMax M3 子 agent 能通过 map_reduce 模式完成最小分片分析。
作用是覆盖 `AgentWorkflowManager.run_workflow_payload(mode="map_reduce")` 到 mapper 子 agent、
scoped file tool、mapper JSON 校验和 deterministic reducer 的真实链路。
关键执行流程：创建两个临时 Python 文件，按单文件分片派发两个 mapper 子 agent，
要求 mapper 读取各自 materialized input 并输出 code_findings JSON，最后断言 reducer 产物和审计记录。
关键函数：test_minimax_m3_map_reduce_runs_two_real_mapper_subagents 执行真实 e2e。

运行方式：
    KONGMING_E2E_REAL_MODEL=1 uv run pytest tests/e2e/test_map_reduce_live_minimax.py -v -s
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from application.agent_roles import AgentRoleManager
from application.agent_workflows.manager import AgentWorkflowManager
from core.agent_spec import AgentSpec
from hosts.cli.main import _apply_model_preset_or_exit
from infrastructure.config import load_config
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from runtime_assembly.session_engine import SessionEngine
from tests.support.workflow_agent_tree import bind_workflow_agent_tree
from tools import AutoAllowApproval, ToolRegistry, build_file_tools

pytestmark = pytest.mark.skipif(
    os.getenv("KONGMING_E2E_REAL_MODEL") != "1",
    reason="set KONGMING_E2E_REAL_MODEL=1 to run real MiniMax M3 map_reduce e2e",
)


def _write_live_source_tree(workspace_root: Path) -> None:
    """写入 live e2e 输入文件，输入为工作区根目录，输出为两个可分析 Python 文件。"""
    input_dir = workspace_root / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "alpha.py").write_text(
        "\n".join(
            [
                "def divide_total(total: int, count: int) -> float:",
                "    # count 来自外部调用，可能为 0。",
                "    return total / count",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (input_dir / "beta.py").write_text(
        "\n".join(
            [
                "def pick_first(items: list[str]) -> str:",
                "    # items 可能为空。",
                "    return items[0]",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _payload() -> dict[str, Any]:
    """构造 live map_reduce payload，输入为空，输出为两个文件两个 shard 的最小任务。"""
    return {
        "mode": "map_reduce",
        "objective": (
            "对每个 shard 的 Python 文件做运行时风险检查。"
            "重点识别除零、空列表下标这类直接异常风险。"
            "必须读取输入文件映射中的 materialized_path，并只输出 code_findings JSON。"
        ),
        "input_source": {
            "kind": "path_glob",
            "root_dir": "input",
            "include": ["*.py"],
            "exclude": [],
            "files": [],
            "index_provider": "rg",
            "input_digest": None,
        },
        "shard_strategy": {
            "kind": "by_file_count",
            "max_files_per_shard": 1,
            "max_estimated_tokens_per_shard": 2000,
            "min_shards": 2,
            "max_shards": 2,
            "preserve_directory_boundary": True,
            "prefer_dependency_cohesion": False,
        },
        "mapper": {
            "name_prefix": "live-map",
            "prompt_template": "code_findings_v0_1",
            "tool_names": ["read_file", "list_dir"],
            "skill_names": [],
            "permission_mode": "scoped_workdir",
            "max_turns": 4,
            "max_output_chars": 12000,
        },
        "reducer": {
            "kind": "deterministic",
            "dedupe_strategy": "exact_dedupe_key",
            "ranking_strategy": "severity_first",
            "max_findings": 10,
            "include_failed_shards": True,
            "reducer_prompt_template": None,
        },
        "limits": {
            "max_concurrency": 2,
            "workflow_timeout_seconds": 180,
            "mapper_timeout_seconds": 90,
            "reducer_timeout_seconds": 30,
            "mapper_retries": 0,
            "validation_repair_retries": 0,
        },
        "output_contract": "code_findings",
        "audit_tags": ["e2e", "minimax-m3", "map_reduce"],
    }


def _audit_records(workflow_dir: Path) -> list[dict[str, Any]]:
    """读取 workflow 审计记录，输入为 workflow 目录，输出为 JSON 行列表。"""
    return [
        json.loads(line)
        for line in (workflow_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def _approval_records(workflow_dir: Path) -> list[dict[str, Any]]:
    """读取子 agent 审批记录，输入为 workflow 目录，输出为审批 payload 列表。"""
    return [
        record["payload"]
        for record in _audit_records(workflow_dir)
        if record["action"] == "subagent_approval_decided"
    ]


@pytest.mark.asyncio
async def test_minimax_m3_map_reduce_runs_two_real_mapper_subagents(tmp_path: Path) -> None:
    """验证 MiniMax M3 真实 map_reduce，输入为两个临时文件，输出为完整 reducer 产物。"""
    base_cfg = load_config(Path("config/setting.yaml"))
    if not os.getenv("MINIMAX_API_KEY"):
        pytest.skip("MINIMAX_API_KEY is required for minimax-m3 preset")

    _write_live_source_tree(tmp_path)
    cfg = _apply_model_preset_or_exit(base_cfg, "minimax-m3")
    cfg = cfg.model_copy(
        update={
            "runner": cfg.runner.model_copy(update={"max_turns": 8}),
            "session": cfg.session.model_copy(
                update={"backend": "memory", "file_store_path": str(tmp_path / "sessions")}
            ),
            "trace": cfg.trace.model_copy(
                update={"raw_llm": False, "output_path": str(tmp_path / "trace.jsonl")}
            ),
            "scheduler": cfg.scheduler.model_copy(update={"enabled": False}),
        }
    )
    registry = ToolRegistry(build_file_tools())
    resolved_model = ModelCatalogManager().resolve_runtime(cfg.model)
    runtime = SessionEngine.build(
        cfg,
        approval=AutoAllowApproval(),
        tools=registry,
        enabled_tool_names=["read_file", "list_dir"],
        agent_spec=AgentSpec(
            name="map-reduce-live-parent",
            instructions="你是 map_reduce live e2e 的父 agent。",
            default_model=resolved_model.name,
            tool_names=(),
            max_turns=8,
            reasoning_effort=cfg.model.reasoning_effort,
        ),
    )
    binding = bind_workflow_agent_tree(
        runtime,
        parent_session_id="live-minimax-map-reduce",
    )
    manager = AgentWorkflowManager(
        runtime=runtime,
        agent_manager=binding.manager,
        config=cfg,
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )

    try:
        result = await manager.run_workflow_payload(
            mode="map_reduce",
            parent_session_id="live-minimax-map-reduce",
            parent_agent=binding.parent_agent,
            payload=_payload(),
        )
    finally:
        await binding.aclose()
        await runtime.aclose()

    reducer_result_path = result.workflow_dir / "map_reduce" / "reducer" / "result.json"
    reducer_result = json.loads(reducer_result_path.read_text(encoding="utf-8"))
    mapper_index = json.loads(
        (result.workflow_dir / "map_reduce" / "mappers" / "index.json").read_text(encoding="utf-8")
    )
    approvals = _approval_records(result.workflow_dir)
    read_approvals = [
        record
        for record in approvals
        if record["tool_name"] == "read_file" and record["decision"] == "approved"
    ]

    assert result.mode == "map_reduce"
    assert result.completed is True
    assert len(result.runs) == 2
    assert reducer_result["status"] == "completed"
    assert reducer_result["total_shards"] == 2
    assert reducer_result["completed_shards"] == 2
    assert reducer_result["failed_shards"] == 0
    assert mapper_index["mapper_count"] == 2
    assert all(mapper["validation_valid"] is True for mapper in mapper_index["mappers"])
    assert {tuple(run.task.metadata["map_reduce_files"]) for run in result.runs} == {
        ("alpha.py",),
        ("beta.py",),
    }
    assert len({record["task_run_id"] for record in read_approvals}) == 2

    print(f"workflow_dir={result.workflow_dir}")
    print(f"reducer_result_path={reducer_result_path}")
    print(f"top_findings={len(reducer_result['top_findings'])}")
    print("mapper_read_approvals=")
    for record in read_approvals:
        print(
            json.dumps(
                {
                    "task_run_id": record["task_run_id"],
                    "tool_name": record["tool_name"],
                    "target_path": record["target_path"],
                    "decision_source": record["decision_source"],
                },
                ensure_ascii=False,
            )
        )
