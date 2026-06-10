"""roundtable_review workflow 集成单元测试。

本脚本验证 Multi-Agent Roundtable Review 策略注册、fake reviewer/arbiter 运行、
ReviewBoard 产物和 run_agent_workflow tool 分流。
作用是用可重复 pytest 覆盖圆桌评审第一版 runtime 集成边界，避免依赖真实模型。
关键执行流程：创建临时源码，构造 roundtable_review payload，用 fake SubAgentManager
返回 claims/comments/report，断言 claims.jsonl、rebuttals.jsonl、final_report.md 和 tool data。
关键函数：_payload 构造 workflow 输入，_FakeRoundtableSubAgentManager 模拟子 agent 输出。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from application.agent_workflows.manager import AgentWorkflowManager
from application.subagents.manager import SubAgentRun
from core.contracts import ToolContext
from infrastructure.config.models import Config, ModelConfig
from tools.agent_workflow_tool import AgentWorkflowHandle, build_run_agent_workflow_tool


class _FakeRoundtableSubAgentManager:
    """测试用子 agent manager，按 roundtable_stage 返回结构化输出。"""

    def __init__(self) -> None:
        """初始化 fake manager，输入为空，输出为记录任务的实例。"""
        self.tasks: list[Any] = []

    async def run_task(
        self,
        *,
        workflow_id: str,
        parent_session_id: str,
        task: Any,
        audit_writer: Any,
    ) -> SubAgentRun:
        """运行 fake 子 agent，输入为任务 metadata，输出为 completed SubAgentRun。"""
        del workflow_id, parent_session_id, audit_writer
        self.tasks.append(task)
        stage = str(task.metadata.get("roundtable_stage"))
        agent = str(task.metadata.get("roundtable_agent"))
        if stage == "independent":
            content = json.dumps(
                {
                    "agent": agent,
                    "findings": [
                        {
                            "severity": "P1",
                            "claim": f"{agent} 发现模块边界风险",
                            "evidence": [
                                {
                                    "type": "code",
                                    "path": "input/source/src/sample.py",
                                    "lines": "1-3",
                                }
                            ],
                            "risk": "后续职责扩展会放大耦合",
                            "suggestion": "拆分门户和内部实现",
                            "confidence": 0.8,
                        }
                    ],
                },
                ensure_ascii=False,
            )
        elif stage == "rebuttal":
            content = json.dumps(
                {
                    "agent": agent,
                    "comments": [
                        {
                            "type": "support",
                            "target_claim_id": "C-001",
                            "comment": f"{agent} 支持 C-001",
                            "evidence": [
                                {
                                    "type": "code",
                                    "path": "input/source/src/sample.py",
                                    "lines": "1-3",
                                }
                            ],
                            "severity_adjustment": None,
                            "confidence": 0.7,
                        }
                    ],
                },
                ensure_ascii=False,
            )
        else:
            content = "\n".join(
                [
                    "# Final Review",
                    "",
                    "## 1. 共识问题",
                    "- C-001 有明确证据。",
                    "",
                    "## 2. 主要分歧",
                    "- 无。",
                    "",
                    "## 3. 高优先级风险",
                    "- P1 模块边界风险。",
                    "",
                    "## 4. 建议修改方案",
                    "- 拆分门户和内部实现。",
                    "",
                    "## 5. 需要人工确认的问题",
                    "- 无。",
                    "",
                    "## 6. 可直接交给开发 Agent 的任务清单",
                    "- 修复 C-001。",
                ]
            )
        return SubAgentRun(
            task=task,
            session_id=f"child-{task.task_id}",
            run_id=f"run-{task.task_id}",
            status="completed",
            content=content,
            error_message=None,
            turn_count=1,
        )


def _config(tmp_path: Path) -> Config:
    """构造测试配置，输入为临时目录，输出为 file session 配置。"""
    cfg = Config(
        model=ModelConfig(
            name="fake-model",
            base_url="http://127.0.0.1:1234/v1",
            api_key="",
        )
    )
    cfg.session.file_store_path = str(tmp_path / "sessions")
    return cfg


def _payload() -> dict[str, Any]:
    """构造 roundtable_review payload，输入为空，输出为 v0.1 JSON。"""
    return {
        "topic": "Session 模块设计是否合理",
        "input_source": {
            "root_dir": ".",
            "paths": ["src"],
            "include": [],
            "exclude": ["__pycache__/**"],
            "max_files": 10,
            "max_bytes_per_file": 80000,
        },
        "limits": {
            "total_child_token_budget": 50000,
            "discussion_rounds": 2,
            "max_discussion_rounds": 6,
            "max_concurrency": 5,
            "agent_timeout_seconds": 30,
        },
        "audit_tags": ["unit", "roundtable"],
    }


def _write_sample_source(tmp_path: Path) -> None:
    """写入临时源码，输入为目录，输出为 sample.py 文件。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "sample.py").write_text(
        "class SessionManager:\n    def run(self):\n        return 'ok'\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_roundtable_review_strategy_writes_review_board(tmp_path: Path) -> None:
    """验证 roundtable_review 产物，输入为 fake 子 agent，输出为白板断言。"""
    _write_sample_source(tmp_path)
    fake = _FakeRoundtableSubAgentManager()
    manager = AgentWorkflowManager(
        subagents=fake,  # type: ignore[arg-type]
        config=_config(tmp_path),
        workspace_root=tmp_path,
    )

    result = await manager.run_workflow_payload(
        mode="roundtable_review",
        parent_session_id="parent-session",
        payload=_payload(),
    )

    assert result.completed is True
    assert result.mode == "roundtable_review"
    assert (
        len([task for task in fake.tasks if task.metadata["roundtable_stage"] == "independent"])
        == 5
    )
    board = result.workflow_dir / "review_board"
    claims = (board / "claims.jsonl").read_text(encoding="utf-8").strip().splitlines()
    rebuttals = (board / "rebuttals.jsonl").read_text(encoding="utf-8").strip().splitlines()
    final_report = (board / "final_report.md").read_text(encoding="utf-8")
    assert len(claims) == 5
    assert len(rebuttals) == 5
    assert "Final Review" in final_report
    assert (board / "context.md").exists()
    assert (board / "sources.md").exists()
    assert (board / "consensus.md").exists()
    assert result.data is not None
    assert result.data["roundtable_review"]["claim_count"] == 5  # type: ignore[index]


def test_agent_workflow_manager_registers_roundtable_review(tmp_path: Path) -> None:
    """验证默认策略目录包含 roundtable_review，输入为 manager，输出为 catalog 断言。"""
    manager = AgentWorkflowManager(
        subagents=object(),  # type: ignore[arg-type]
        config=_config(tmp_path),
        workspace_root=tmp_path,
    )

    catalog = manager.list_workflow_strategies()
    assert [entry.mode for entry in catalog] == [
        "map_reduce",
        "parallel",
        "roundtable_review",
    ]
    description = manager.describe_workflow_strategy("roundtable_review")
    assert description.runnable is True
    assert description.inputs[0].name == "topic"


@pytest.mark.asyncio
async def test_run_agent_workflow_tool_dispatches_roundtable_review(tmp_path: Path) -> None:
    """验证 tool 入口分流 roundtable_review，输入为 payload，输出为 data 路径断言。"""
    _write_sample_source(tmp_path)
    manager = AgentWorkflowManager(
        subagents=_FakeRoundtableSubAgentManager(),  # type: ignore[arg-type]
        config=_config(tmp_path),
        workspace_root=tmp_path,
    )
    handle = AgentWorkflowHandle()
    handle.bind(manager)
    tool = build_run_agent_workflow_tool(handle)

    result = await tool.execute(
        {"mode": "roundtable_review", "payload": _payload()},
        ToolContext(run_id="r", session_id="parent-session", turn=1, call_id="c"),
    )

    assert result.ok is True
    assert "roundtable_final_report" in result.content
    assert result.data is not None
    assert result.data["mode"] == "roundtable_review"
    board = result.data["roundtable_review"]["review_board"]  # type: ignore[index]
    assert Path(board["final_report_path"]).exists()
