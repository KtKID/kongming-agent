"""Deep Research FactBoard 单元测试。

本脚本验证 DeepResearchArtifactWriter 的 artifact 目录、JSONL 追加、phase summary、stats 和 report 文件。
作用是固定 deep_research/report.md、stats.json、phase_summaries.jsonl 等 viewer 和 e2e 依赖的产物合同。
关键执行流程：创建 writer，写入 plan/source/fact/group/ruling/report/stats，读取磁盘文件做结构断言。
关键函数：_writer 构造 artifact writer，_jsonl 读取 JSONL，test_* 覆盖白板产物。
"""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path
from typing import Any


def test_artifact_writer_creates_deep_research_board_and_jsonl_files(tmp_path: Path) -> None:
    """验证 artifact writer，输入为一组确定性事实链，输出为 deep_research 目录文件。"""
    writer = _writer(tmp_path / "wf-1")

    plan_path = writer.write_plan(
        {
            "topic": "Kongming Deep Research",
            "objective": "Produce a cited report.",
            "lines": [
                {
                    "line_id": "L-001",
                    "topic": "architecture",
                    "query": "kongming deep research workflow",
                    "why": "cover workflow structure",
                    "angle": "overview",
                }
            ],
            "created_by": "planner-1",
        }
    )
    writer.append_source(
        {
            "source_id": "S-001",
            "canonical_url": "https://example.com/research",
            "url": "https://example.com/research",
            "title": "Research source",
            "tier": "primary",
            "fetch_status": "fetched",
            "excerpt": "Deep Research writes cited artifacts.",
            "error": None,
        },
        bucket="selected",
    )
    writer.append_fact(
        {
            "fact_id": "F-001",
            "statement": "Deep Research writes cited artifacts.",
            "excerpt": "Deep Research writes cited artifacts.",
            "source_id": "S-001",
            "source_url": "https://example.com/research",
            "source_tier": "primary",
            "weight": "key",
            "confidence_hint": "high",
        },
        bucket="raw",
    )
    writer.append_fact(
        {
            "fact_id": "F-001",
            "statement": "Deep Research writes cited artifacts.",
            "excerpt": "Deep Research writes cited artifacts.",
            "source_id": "S-001",
            "source_url": "https://example.com/research",
            "source_tier": "primary",
            "weight": "key",
            "confidence_hint": "high",
        },
        bucket="top",
    )
    writer.append_group(
        {
            "group_id": "G-001",
            "canonical_statement": "Deep Research writes cited artifacts.",
            "member_fact_ids": ["F-001"],
            "source_ids": ["S-001"],
            "best_excerpt": "Deep Research writes cited artifacts.",
            "support_count": 1,
        }
    )
    writer.append_ruling(
        {
            "ruling_id": "R-001",
            "group_id": "G-001",
            "juror_id": "J-001",
            "reject": False,
            "abstain": False,
            "reason": "source supports statement",
            "contradicting_evidence": [],
            "source_coverage": "covered",
        }
    )
    writer.append_checked_group(
        {
            "group_id": "G-001",
            "status": "upheld",
            "cast_count": 2,
            "reject_count": 0,
            "abstain_count": 1,
            "tally": "2-0",
            "decision_reason": "upheld",
        }
    )
    writer.append_phase_summary(
        {
            "phase": "crosscheck",
            "status": "completed",
            "artifact_paths": ["deep_research/groups.checked.jsonl"],
            "stats_delta": {"upheld_count": 1},
        }
    )
    stats_path = writer.write_stats(
        {
            "search_line_count": 1,
            "raw_hit_count": 1,
            "selected_source_count": 1,
            "duplicate_source_count": 0,
            "overflow_source_count": 0,
            "fetched_source_count": 1,
            "raw_fact_count": 1,
            "top_fact_count": 1,
            "group_count": 1,
            "jury_task_count": 3,
            "abstain_count": 1,
            "upheld_count": 1,
            "rejected_count": 0,
        }
    )
    report_path = writer.write_report(
        {
            "answer": "Deep Research artifacts are complete.",
            "findings": [
                {
                    "statement": "Deep Research writes cited artifacts.",
                    "sources": ["S-001"],
                    "tally": "2-0",
                }
            ],
            "rejected": [],
            "limitations": [],
            "open_questions": [],
            "stats": {"upheld_count": 1},
        },
        markdown="# Deep Research\n\n- Finding [S-001], tally 2-0\n",
    )

    board_dir = tmp_path / "wf-1" / "deep_research"
    assert plan_path == board_dir / "plan.json"
    assert stats_path == board_dir / "stats.json"
    assert report_path == board_dir / "report.md"
    assert (board_dir / "spec.json").is_file()
    assert json.loads(plan_path.read_text(encoding="utf-8"))["lines"][0]["line_id"] == "L-001"
    assert _jsonl(board_dir / "sources.selected.jsonl")[0]["source_id"] == "S-001"
    assert _jsonl(board_dir / "facts.raw.jsonl")[0]["fact_id"] == "F-001"
    assert _jsonl(board_dir / "facts.top.jsonl")[0]["fact_id"] == "F-001"
    assert _jsonl(board_dir / "groups.jsonl")[0]["group_id"] == "G-001"
    assert _jsonl(board_dir / "rulings.jsonl")[0]["ruling_id"] == "R-001"
    assert _jsonl(board_dir / "groups.checked.jsonl")[0]["status"] == "upheld"
    assert _jsonl(board_dir / "phase_summaries.jsonl")[0]["phase"] == "crosscheck"
    assert json.loads((board_dir / "stats.json").read_text(encoding="utf-8"))["top_fact_count"] == 1
    assert "tally 2-0" in (board_dir / "report.md").read_text(encoding="utf-8")
    assert (
        json.loads((board_dir / "report.json").read_text(encoding="utf-8"))["findings"][0]["tally"]
        == "2-0"
    )


def _writer(workflow_dir: Path) -> Any:
    """构造 DeepResearchArtifactWriter，输入为 workflow 目录，输出为 writer。"""
    module = import_module("application.agent_workflows.strategies.deep_research.fact_board")
    writer_cls = getattr(module, "DeepResearchArtifactWriter")
    return writer_cls(workflow_dir=workflow_dir)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL 文件，输入为路径，输出为对象列表。"""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
