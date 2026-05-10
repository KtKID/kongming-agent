"""集成测试：scan 自动绑定 + re-scan node_states 更新完整链路。"""

from __future__ import annotations

from pathlib import Path

import pytest

from web.workflow.models import RunStatus, StageStatus
from web.workflow.service import WorkflowService
from web.workflow.store import WorkflowStore

# ── helpers ──────────────────────────────────────────────────


def _setup_project(root: Path) -> None:
    (root / "dev-pipeline" / "tasks").mkdir(parents=True)
    (root / "docs").mkdir(parents=True)


def _create_task_dir(
    root: Path,
    task_id: str,
    *,
    readme_text: str = "# Test Task\n",
    extra_files: dict[str, str] | None = None,
) -> Path:
    task_dir = root / "dev-pipeline" / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "README.md").write_text(readme_text, encoding="utf-8")
    if extra_files:
        for name, content in extra_files.items():
            p = task_dir / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    return task_dir


# ── Test 1: 完整链路（自动绑定 + 状态更新）──────────────────


class TestFullPipeline:
    def test_scan_auto_binds_and_rescan_updates_node_states(self, tmp_path: Path) -> None:
        _setup_project(tmp_path)
        _create_task_dir(
            tmp_path,
            "test-task",
            extra_files={"dev-checklist.md": "- [ ] item\n"},
        )

        store = WorkflowStore(tmp_path / ".workflow")
        service = WorkflowService(project_root=tmp_path, store=store)

        # 第一次 scan：自动发现 task + 自动绑定 full-pipeline
        result = service.scan()
        assert "test-task" in [t.task_id for t in result.tasks]

        runs = service.list_runs()
        run = next(r for r in runs if r.task_id == "test-task")
        assert run.status == RunStatus.ACTIVE
        assert run.workflow_id == "full-pipeline"
        # req 节点已 done（dev-checklist.md 存在）
        assert run.node_states["n-req"] == StageStatus.DONE

        # 模拟 x-dev 完成
        task_dir = tmp_path / "dev-pipeline" / "tasks" / "test-task"
        (task_dir / "dev-report.md").write_text("# Dev Report\n", encoding="utf-8")

        # 再次 scan
        service.scan()
        updated_run = service.get_run(run.id)
        assert updated_run is not None
        assert updated_run.node_states["n-dev"] == StageStatus.DONE
        assert updated_run.node_states["n-req"] == StageStatus.DONE

        # 验证 event 记录
        events = service.list_events(run_id=run.id)
        changed_node_ids = {e.node_id for e in events}
        assert "n-dev" in changed_node_ids


# ── Test 2: spec ↔ task 关联 ──────────────────────────────


class TestSpecTaskAssociation:
    def test_spec_ref_detected_and_spec_node_done(self, tmp_path: Path) -> None:
        _setup_project(tmp_path)

        spec_dir = tmp_path / "docs" / "my-spec"
        spec_dir.mkdir(parents=True)
        (spec_dir / "01-goals-and-boundaries.md").write_text("# Goals\n", encoding="utf-8")

        _create_task_dir(
            tmp_path,
            "spec-linked-task",
            readme_text="# Spec Linked Task\n\n设计文档：docs/my-spec/\n",
            extra_files={"dev-checklist.md": "- [ ] a\n"},
        )

        store = WorkflowStore(tmp_path / ".workflow")
        service = WorkflowService(project_root=tmp_path, store=store)

        result = service.scan()
        task_snap = next(t for t in result.tasks if t.task_id == "spec-linked-task")
        assert task_snap.spec_ref == "my-spec"
        assert task_snap.stages.get("x-spec") == StageStatus.DONE

        # 自动绑定后，n-spec 节点应 done
        runs = service.list_runs()
        run = next(r for r in runs if r.task_id == "spec-linked-task")
        assert run.node_states["n-spec"] == StageStatus.DONE


# ── Test 3: 预置模板加载 ─────────────────────────────────


class TestPresetDefinitions:
    def test_list_definitions_contains_presets(self, tmp_path: Path) -> None:
        _setup_project(tmp_path)
        store = WorkflowStore(tmp_path / ".workflow")
        service = WorkflowService(project_root=tmp_path, store=store)

        defs = service.list_definitions()
        loaded_ids = {d.id for d in defs}
        assert "full-pipeline" in loaded_ids
        assert "quick-dev" in loaded_ids


# ── Test 4: 重复手动 create_run 被拒（自动绑定已占位）────


class TestDuplicateRunRejection:
    def test_duplicate_active_run_raises(self, tmp_path: Path) -> None:
        _setup_project(tmp_path)
        _create_task_dir(tmp_path, "dup-task")

        store = WorkflowStore(tmp_path / ".workflow")
        service = WorkflowService(project_root=tmp_path, store=store)
        service.scan()  # 自动绑定

        with pytest.raises(ValueError, match="already has an active run"):
            service.create_run(task_id="dup-task", workflow_id="full-pipeline")


# ── Test 5: toggle_pin ────────────────────────────────────


class TestTogglePin:
    def test_toggle_pin_creates_and_removes_file(self, tmp_path: Path) -> None:
        _setup_project(tmp_path)
        _create_task_dir(tmp_path, "pin-task")

        store = WorkflowStore(tmp_path / ".workflow")
        service = WorkflowService(project_root=tmp_path, store=store)

        pin_file = tmp_path / "dev-pipeline" / "tasks" / "pin-task" / ".pinned"
        assert not pin_file.exists()

        result = service.toggle_pin("pin-task")
        assert result is True
        assert pin_file.exists()

        result = service.toggle_pin("pin-task")
        assert result is False
        assert not pin_file.exists()

    def test_toggle_pin_nonexistent_task_raises(self, tmp_path: Path) -> None:
        _setup_project(tmp_path)

        store = WorkflowStore(tmp_path / ".workflow")
        service = WorkflowService(project_root=tmp_path, store=store)

        with pytest.raises(FileNotFoundError):
            service.toggle_pin("no-such-task")

    def test_scan_reflects_pinned_state(self, tmp_path: Path) -> None:
        _setup_project(tmp_path)
        _create_task_dir(tmp_path, "pin-task")

        store = WorkflowStore(tmp_path / ".workflow")
        service = WorkflowService(project_root=tmp_path, store=store)

        result = service.scan()
        assert result.tasks[0].pinned is False

        service.toggle_pin("pin-task")
        result = service.scan()
        assert result.tasks[0].pinned is True
