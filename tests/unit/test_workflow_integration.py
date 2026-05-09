"""集成测试：scan → create_run → re-scan → node_states 更新完整链路。"""

from __future__ import annotations

from pathlib import Path

import pytest

from web.workflow.models import RunStatus, StageStatus
from web.workflow.service import WorkflowService
from web.workflow.store import WorkflowStore
from web.workflow.templates import get_preset_definitions

# ── helpers ──────────────────────────────────────────────────


def _setup_project(root: Path) -> None:
    """创建最小 project 骨架：dev-pipeline/tasks/ 和 docs/。"""
    (root / "dev-pipeline" / "tasks").mkdir(parents=True)
    (root / "docs").mkdir(parents=True)


def _create_task_dir(
    root: Path,
    task_id: str,
    *,
    readme_text: str = "# Test Task\n",
    extra_files: dict[str, str] | None = None,
) -> Path:
    """在 dev-pipeline/tasks/<task_id>/ 下创建 task 目录。"""
    task_dir = root / "dev-pipeline" / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "README.md").write_text(readme_text, encoding="utf-8")
    if extra_files:
        for name, content in extra_files.items():
            p = task_dir / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    return task_dir


def _full_pipeline_id() -> str:
    presets = get_preset_definitions()
    return presets[0].id  # "full-pipeline"


# ── Test 1: 完整链路 ────────────────────────────────────────


class TestFullPipeline:
    """scan → create_run → 模拟产物 → re-scan → 验证 node_states 更新 + 事件记录。"""

    def test_scan_create_run_rescan_updates_node_states(self, tmp_path: Path) -> None:
        _setup_project(tmp_path)
        _create_task_dir(
            tmp_path,
            "test-task",
            extra_files={"dev-checklist.md": "- [ ] item\n"},
        )

        store = WorkflowStore(tmp_path / ".workflow")
        service = WorkflowService(project_root=tmp_path, store=store)

        # ── 第一次 scan：发现 task ────────────────────────
        result = service.scan()
        assert service.last_scan is not None
        task_ids = [t.task_id for t in result.tasks]
        assert "test-task" in task_ids

        # ── create_run：绑定 full-pipeline ─────────────────
        wf_id = _full_pipeline_id()
        run = service.create_run(task_id="test-task", workflow_id=wf_id)
        assert run.status == RunStatus.ACTIVE
        # 所有 node_states 初始为 not_started
        for node_id, status in run.node_states.items():
            assert status == StageStatus.NOT_STARTED, (
                f"node {node_id} should be not_started, got {status}"
            )

        # ── 模拟 x-dev 完成：放入 dev-report.md ───────────
        task_dir = tmp_path / "dev-pipeline" / "tasks" / "test-task"
        (task_dir / "dev-report.md").write_text("# Dev Report\n", encoding="utf-8")

        # ── 再次 scan：验证 node_states 更新 ──────────────
        service.scan()

        updated_run = service.get_run(run.id)
        assert updated_run is not None

        # x-dev 对应节点（n-dev）变 done
        assert updated_run.node_states["n-dev"] == StageStatus.DONE

        # x-req 节点也应 done（dev-checklist.md 之前已放入）
        assert updated_run.node_states["n-req"] == StageStatus.DONE

        # x-spec 节点仍 not_started（无 spec_ref 关联）
        assert updated_run.node_states["n-spec"] == StageStatus.NOT_STARTED

        # ── 验证 WorkflowEvent 记录 ──────────────────────
        events = service.list_events(run_id=run.id)
        assert len(events) >= 1
        changed_node_ids = {e.node_id for e in events}
        assert "n-dev" in changed_node_ids
        # 每条 event 的 new_status 都是 done
        for e in events:
            assert e.new_status == StageStatus.DONE
            assert e.old_status == StageStatus.NOT_STARTED


# ── Test 2: spec ↔ task 关联 ──────────────────────────────


class TestSpecTaskAssociation:
    """spec_ref 正确解析 + x-spec 节点自动 done。"""

    def test_spec_ref_detected_and_spec_node_done(self, tmp_path: Path) -> None:
        _setup_project(tmp_path)

        # 创建 spec 目录 + 必要文件
        spec_dir = tmp_path / "docs" / "my-spec"
        spec_dir.mkdir(parents=True)
        (spec_dir / "01-goals-and-boundaries.md").write_text("# Goals\n", encoding="utf-8")

        # 创建 task，README 引用 docs/my-spec/
        _create_task_dir(
            tmp_path,
            "spec-linked-task",
            readme_text="# Spec Linked Task\n\n设计文档：docs/my-spec/\n",
            extra_files={"dev-checklist.md": "- [ ] a\n"},
        )

        store = WorkflowStore(tmp_path / ".workflow")
        service = WorkflowService(project_root=tmp_path, store=store)

        # scan → 验证 spec_ref
        result = service.scan()
        task_snap = next(t for t in result.tasks if t.task_id == "spec-linked-task")
        assert task_snap.spec_ref == "my-spec"

        # 验证 x-spec stage = done（spec 目录下有 01-goals-and-boundaries.md）
        assert task_snap.stages.get("x-spec") == StageStatus.DONE

        # 绑定 run 后再 scan → 验证 n-spec 节点 done
        wf_id = _full_pipeline_id()
        run = service.create_run(task_id="spec-linked-task", workflow_id=wf_id, spec_id="my-spec")
        service.scan()
        updated = service.get_run(run.id)
        assert updated is not None
        assert updated.node_states["n-spec"] == StageStatus.DONE


# ── Test 3: 预置模板加载 ─────────────────────────────────


class TestPresetDefinitions:
    """新建 service 时预置模板自动注入 store。"""

    def test_list_definitions_contains_presets(self, tmp_path: Path) -> None:
        _setup_project(tmp_path)
        store = WorkflowStore(tmp_path / ".workflow")
        service = WorkflowService(project_root=tmp_path, store=store)

        defs = service.list_definitions()
        preset_names = get_preset_definitions()
        preset_ids = {p.id for p in preset_names}

        loaded_ids = {d.id for d in defs}
        assert preset_ids.issubset(loaded_ids), f"Missing presets: {preset_ids - loaded_ids}"
        # full-pipeline 和 quick-dev 都应存在
        assert "full-pipeline" in loaded_ids
        assert "quick-dev" in loaded_ids


# ── Test 4: 重复绑定 ─────────────────────────────────────


class TestDuplicateRunRejection:
    """同一 task 已有活跃 run 时，再次 create_run 应 raise ValueError。"""

    def test_duplicate_active_run_raises(self, tmp_path: Path) -> None:
        _setup_project(tmp_path)
        _create_task_dir(tmp_path, "dup-task")

        store = WorkflowStore(tmp_path / ".workflow")
        service = WorkflowService(project_root=tmp_path, store=store)
        service.scan()

        wf_id = _full_pipeline_id()
        service.create_run(task_id="dup-task", workflow_id=wf_id)

        with pytest.raises(ValueError, match="already has an active run"):
            service.create_run(task_id="dup-task", workflow_id=wf_id)
