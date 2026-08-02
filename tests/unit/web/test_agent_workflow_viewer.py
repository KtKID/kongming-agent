"""Agent Workflow Viewer 后端只读投影测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hosts.web.errors import KongmingWebError, kongming_error_handler
from hosts.web.routers.agent_workflows import router
from hosts.web.workflow_viewer.artifact_reader import encode_artifact_id
from hosts.web.workflow_viewer.manager import WorkflowRunViewerManager
from infrastructure.config.models import Config, ModelSelectionConfig


def _cfg(tmp_path: Path) -> Config:
    cfg = Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"))
    cfg.session.file_store_path = str(tmp_path / "sessions")
    return cfg


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        "utf-8",
    )


def _create_roundtable_fixture(tmp_path: Path) -> tuple[Config, str, str, str]:
    cfg = _cfg(tmp_path)
    session_root = Path(cfg.session.file_store_path)
    thread_id = "thread-aaaaaaaaaaaa"
    workflow_id = "wf-20260611T045925-e5392d77"
    workflow_desc = "梳理现有 thread/session 路径与顶部工具栏结构"
    task_run_id = "001-reviewer"
    child_session_id = "subagent-thread-roundtable-reviewer"
    workflow_dir = session_root / thread_id / "agent-workflows" / workflow_id
    _write_json(
        workflow_dir / "workflow.json",
        {
            "workflow_id": workflow_id,
            "desc": workflow_desc,
            "mode": "roundtable_review",
            "parent_session_id": thread_id,
            "started_at": "2026-06-11T01:00:00+00:00",
            "finished_at": "2026-06-11T01:01:00+00:00",
            "status": "completed",
            "assigned_agents": [{"task_run_id": task_run_id, "task_id": "reviewer"}],
        },
    )
    _write_jsonl(
        workflow_dir / "audit.jsonl",
        [
            {
                "ts": "2026-06-11T01:00:00+00:00",
                "action": "roundtable_review_started",
                "payload": {"workflow_id": workflow_id},
            },
            {
                "ts": "2026-06-11T01:00:01+00:00",
                "action": "subagent_created",
                "payload": {
                    "workflow_id": workflow_id,
                    "task_id": "reviewer",
                    "task_run_id": task_run_id,
                    "task_name": "Reviewer",
                    "session_id": child_session_id,
                    "working_dir": str(workflow_dir / "agents" / task_run_id),
                    "subagent_json_path": str(
                        workflow_dir / "agents" / task_run_id / "subagent.json"
                    ),
                },
            },
            {
                "ts": "2026-06-11T01:00:02+00:00",
                "action": "agent_completed",
                "payload": {
                    "workflow_id": workflow_id,
                    "task_id": "reviewer",
                    "task_run_id": task_run_id,
                    "session_id": child_session_id,
                    "run_id": "run-1",
                    "status": "completed",
                },
            },
            {
                "ts": "2026-06-11T01:00:03+00:00",
                "action": "subagent_reported",
                "payload": {
                    "workflow_id": workflow_id,
                    "task_id": "reviewer",
                    "task_run_id": task_run_id,
                    "session_id": child_session_id,
                    "run_id": "run-1",
                    "status": "completed",
                    "report_path": str(workflow_dir / "reports" / f"{task_run_id}.json"),
                },
            },
            {
                "ts": "2026-06-11T01:01:00+00:00",
                "action": "roundtable_review_completed",
                "payload": {"workflow_id": workflow_id},
            },
        ],
    )
    usage = {
        "input_tokens": 10,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 2,
        "output_tokens": 5,
        "total_tokens": 20,
    }
    _write_json(
        workflow_dir / "result.json",
        {
            "workflow_id": workflow_id,
            "desc": workflow_desc,
            "mode": "roundtable_review",
            "completed": True,
            "roundtable_review": {
                "topic": "圆桌审查",
                "claim_count": 1,
                "rebuttal_count": 0,
                "child_agent_usages": [
                    {
                        "task_id": "reviewer",
                        "task_name": "Reviewer",
                        "session_id": child_session_id,
                        "run_id": "run-1",
                        "status": "completed",
                        "stage": "independent",
                        "agent": "reviewer",
                        "usage": usage,
                    }
                ],
                "child_agent_usage_totals": usage,
            },
        },
    )
    _write_json(
        workflow_dir / "reports" / "index.json",
        {
            "workflow_id": workflow_id,
            "desc": workflow_desc,
            "mode": "roundtable_review",
            "status": "completed",
            "reports": [
                {
                    "display_order": 1,
                    "task_id": "reviewer",
                    "task_name": "Reviewer",
                    "status": "completed",
                    "summary": "done",
                    "report_path": str(workflow_dir / "reports" / f"{task_run_id}.json"),
                    "session_id": child_session_id,
                    "run_id": "run-1",
                    "usage": usage,
                }
            ],
        },
    )
    _write_json(
        workflow_dir / "reports" / f"{task_run_id}.json",
        {
            "task_id": "reviewer",
            "task_name": "Reviewer",
            "status": "completed",
            "summary": "done",
            "content": "report content",
            "session_id": child_session_id,
            "run_id": "run-1",
            "usage": usage,
        },
    )
    _write_json(
        workflow_dir / "agents" / task_run_id / "subagent.json",
        {
            "workflow_id": workflow_id,
            "task_run_id": task_run_id,
            "session_id": child_session_id,
            "task_id": "reviewer",
            "task_name": "Reviewer",
            "child_session_log_path": str(tmp_path / "outside" / f"{child_session_id}.jsonl"),
            "usage": usage,
        },
    )
    _write_jsonl(
        session_root / child_session_id / f"{child_session_id}.jsonl",
        [
            {
                "schema_version": "0.1.2",
                "session_id": child_session_id,
                "message_id": "m1",
                "created_at": 1.0,
                "message": {"role": "user", "content": "任务", "tool_calls": None},
            },
            {
                "schema_version": "0.1.2",
                "session_id": child_session_id,
                "message_id": "m2",
                "created_at": 2.0,
                "message": {"role": "assistant", "content": "结论", "tool_calls": []},
                "usage": usage,
            },
        ],
    )
    (workflow_dir / "review_board").mkdir(parents=True, exist_ok=True)
    (workflow_dir / "review_board" / "context.md").write_text("context", "utf-8")
    (workflow_dir / "review_board" / "sources.md").write_text("sources", "utf-8")
    _write_jsonl(
        workflow_dir / "review_board" / "claims.jsonl",
        [{"claim_id": "C-001", "claim": "claim"}],
    )
    _write_jsonl(workflow_dir / "review_board" / "rebuttals.jsonl", [])
    (workflow_dir / "review_board" / "consensus.md").write_text("consensus", "utf-8")
    (workflow_dir / "review_board" / "final_report.md").write_text("final", "utf-8")
    return cfg, thread_id, workflow_id, task_run_id


def test_workflow_viewer_projects_roundtable_and_usage(tmp_path: Path) -> None:
    cfg, thread_id, workflow_id, _ = _create_roundtable_fixture(tmp_path)
    manager = WorkflowRunViewerManager(config=cfg, workspace_root=tmp_path)

    listing = manager.list_workflows(thread_id)
    assert [item.workflow_id for item in listing.workflows] == [workflow_id]
    assert listing.workflows[0].mode == "roundtable_review"
    assert listing.workflows[0].desc == "梳理现有 thread/session 路径与顶部工具栏结构"
    assert listing.workflows[0].title == "梳理现有 thread/session 路径与顶部工具栏结构"
    assert listing.workflows[0].usage.totals["total_tokens"] == 20
    assert listing.workflows[0].usage.provider_totals["claude"]["cache_read_input_tokens"] == 3

    detail = manager.get_workflow_detail(thread_id, workflow_id)
    assert detail.item.title == "梳理现有 thread/session 路径与顶部工具栏结构"
    assert any(panel.kind == "review_board" for panel in detail.panels)
    assert detail.reports[0].conversation_available is True
    assert detail.reports[0].snapshot["task_id"] == "reviewer"
    assert [event.source_action for event in detail.reports[0].activity_events] == [
        "subagent_created",
        "agent_completed",
        "subagent_reported",
    ]
    assert detail.timeline[-1].action == "roundtable_review_completed"


def test_workflow_viewer_conversation_uses_session_id_fallback(tmp_path: Path) -> None:
    cfg, thread_id, workflow_id, task_run_id = _create_roundtable_fixture(tmp_path)
    manager = WorkflowRunViewerManager(config=cfg, workspace_root=tmp_path)

    conversation = manager.load_conversation(
        thread_id=thread_id,
        workflow_id=workflow_id,
        task_run_id=task_run_id,
    )

    assert [message.role for message in conversation.messages] == ["user", "assistant"]
    assert conversation.messages[1].usage is not None
    assert any(item.code == "conversation.session_id_fallback" for item in conversation.diagnostics)


def test_workflow_viewer_activity_matches_legacy_fallback_fields(tmp_path: Path) -> None:
    cfg, thread_id, workflow_id, task_run_id = _create_roundtable_fixture(tmp_path)
    workflow_dir = Path(cfg.session.file_store_path) / thread_id / "agent-workflows" / workflow_id
    _write_jsonl(
        workflow_dir / "audit.jsonl",
        [
            {
                "ts": "2026-06-11T01:00:01+00:00",
                "action": "subagent_created",
                "payload": {
                    "subagent_json_path": str(
                        workflow_dir / "agents" / task_run_id / "subagent.json"
                    )
                },
            },
            {
                "ts": "2026-06-11T01:00:02+00:00",
                "action": "agent_completed",
                "payload": {"run_id": "run-1", "status": "completed"},
            },
            {
                "ts": "2026-06-11T01:00:03+00:00",
                "action": "subagent_reported",
                "payload": {
                    "session_id": "subagent-thread-roundtable-reviewer",
                    "report_path": str(workflow_dir / "reports" / f"{task_run_id}.json"),
                },
            },
        ],
    )
    manager = WorkflowRunViewerManager(config=cfg, workspace_root=tmp_path)

    detail = manager.get_workflow_detail(thread_id, workflow_id)
    report = detail.reports[0]

    assert [event.activity_type for event in report.activity_events] == [
        "created",
        "completed",
        "reported",
    ]
    assert {item.code for item in report.diagnostics} >= {"subagent_activity.fallback_match"}


def test_workflow_viewer_activity_empty_when_no_matching_events(tmp_path: Path) -> None:
    cfg, thread_id, workflow_id, _ = _create_roundtable_fixture(tmp_path)
    workflow_dir = Path(cfg.session.file_store_path) / thread_id / "agent-workflows" / workflow_id
    _write_jsonl(
        workflow_dir / "audit.jsonl",
        [
            {
                "ts": "2026-06-11T01:00:01+00:00",
                "action": "other_agent_completed",
                "payload": {"task_run_id": "999-other"},
            }
        ],
    )
    manager = WorkflowRunViewerManager(config=cfg, workspace_root=tmp_path)

    detail = manager.get_workflow_detail(thread_id, workflow_id)

    assert detail.reports[0].activity_events == []


def test_workflow_viewer_reports_malformed_audit_diagnostic(tmp_path: Path) -> None:
    cfg, thread_id, workflow_id, _ = _create_roundtable_fixture(tmp_path)
    workflow_dir = Path(cfg.session.file_store_path) / thread_id / "agent-workflows" / workflow_id
    with open(workflow_dir / "audit.jsonl", "a", encoding="utf-8") as handle:
        handle.write("{broken\n")
    manager = WorkflowRunViewerManager(config=cfg, workspace_root=tmp_path)

    detail = manager.get_workflow_detail(thread_id, workflow_id)

    assert any(
        item.code == "artifact.read_failed" and item.path == "audit.jsonl:6"
        for item in detail.diagnostics
    )


def test_workflow_viewer_title_falls_back_to_topic_for_legacy_runs(tmp_path: Path) -> None:
    cfg, thread_id, workflow_id, _ = _create_roundtable_fixture(tmp_path)
    workflow_dir = Path(cfg.session.file_store_path) / thread_id / "agent-workflows" / workflow_id
    for relative in ("workflow.json", "result.json", "reports/index.json"):
        path = workflow_dir / relative
        payload = json.loads(path.read_text("utf-8"))
        payload.pop("desc", None)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    manager = WorkflowRunViewerManager(config=cfg, workspace_root=tmp_path)

    detail = manager.get_workflow_detail(thread_id, workflow_id)

    assert detail.item.desc is None
    assert detail.item.title == "圆桌审查"


def test_workflow_viewer_rejects_artifact_path_escape(tmp_path: Path) -> None:
    cfg, thread_id, workflow_id, _ = _create_roundtable_fixture(tmp_path)
    manager = WorkflowRunViewerManager(config=cfg, workspace_root=tmp_path)

    with pytest.raises(ValueError):
        manager.read_artifact(
            thread_id=thread_id,
            workflow_id=workflow_id,
            artifact_id=encode_artifact_id("../secret.txt"),
        )


def test_workflow_viewer_resolves_default_session_path_from_kongming_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("KONGMING_HOME", str(home))
    cfg = Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"))
    cfg.session.file_store_path = ".kongming/sessions"
    thread_id = "thread-aaaaaaaaaaaa"
    workflow_id = "wf-20260612T031003-57c5672d"
    workflow_dir = home / "sessions" / thread_id / "agent-workflows" / workflow_id
    _write_json(
        workflow_dir / "workflow.json",
        {
            "workflow_id": workflow_id,
            "desc": "最小 workflow 验证：3 个子 agent 并行返回 1-100 随机数",
            "mode": "parallel",
            "parent_session_id": thread_id,
            "started_at": "2026-06-12T03:10:03+00:00",
            "finished_at": "2026-06-12T03:10:05+00:00",
            "status": "completed",
            "assigned_agents": [],
        },
    )

    manager = WorkflowRunViewerManager(config=cfg, workspace_root=tmp_path)

    assert manager.session_root == home / "sessions"
    listing = manager.list_workflows(thread_id)
    assert [item.workflow_id for item in listing.workflows] == [workflow_id]
    assert listing.workflows[0].title == "最小 workflow 验证：3 个子 agent 并行返回 1-100 随机数"


def test_agent_workflow_router_lists_thread_workflows(tmp_path: Path) -> None:
    cfg, thread_id, workflow_id, _ = _create_roundtable_fixture(tmp_path)
    app = FastAPI()
    app.state.config = cfg
    app.state.workspace_root = tmp_path
    app.state.thread_manager = SimpleNamespace(list_threads=lambda: [SimpleNamespace(id=thread_id)])
    app.add_exception_handler(KongmingWebError, kongming_error_handler)
    app.include_router(router)

    response = TestClient(app).get(f"/api/threads/{thread_id}/agent-workflows")

    assert response.status_code == 200
    body = response.json()
    assert body["workflows"][0]["workflow_id"] == workflow_id
    assert body["workflows"][0]["desc"] == "梳理现有 thread/session 路径与顶部工具栏结构"
    assert body["workflows"][0]["title"] == "梳理现有 thread/session 路径与顶部工具栏结构"
