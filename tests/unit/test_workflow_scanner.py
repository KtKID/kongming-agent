"""WorkflowScanner 单元测试。"""

from __future__ import annotations

from pathlib import Path

from web.workflow.models import StageStatus
from web.workflow.scanner import WorkflowScanner
from web.workflow.templates import get_node_templates


def _make_scanner(root: Path) -> WorkflowScanner:
    return WorkflowScanner(root, get_node_templates())


# ── 1. 空项目 ──────────────────────────────────────────────


def test_empty_project_returns_empty(tmp_path: Path) -> None:
    """无 docs/ 无 dev-pipeline/tasks/ → specs 和 tasks 都为空。"""
    result = _make_scanner(tmp_path).scan()
    assert result.specs == []
    assert result.tasks == []


# ── 2. 只有 spec 目录 ──────────────────────────────────────


def test_spec_only(tmp_path: Path) -> None:
    """docs/my-spec/01-goals-and-boundaries.md 存在 → specs 1 项，tasks 空。"""
    spec_dir = tmp_path / "docs" / "my-spec"
    spec_dir.mkdir(parents=True)
    (spec_dir / "01-goals-and-boundaries.md").write_text("# Goals")

    result = _make_scanner(tmp_path).scan()
    assert len(result.specs) == 1
    assert result.specs[0].spec_id == "my-spec"
    assert result.specs[0].has_goals is True
    assert result.tasks == []


# ── 3. 只有 task 目录 ──────────────────────────────────────


def test_task_only_with_req_stage(tmp_path: Path) -> None:
    """dev-pipeline/tasks/my-task/ 有 README.md + dev-checklist.md → tasks 1 项，x-req = done。"""
    task_dir = tmp_path / "dev-pipeline" / "tasks" / "my-task"
    task_dir.mkdir(parents=True)
    (task_dir / "README.md").write_text("# My Task\n\nSome description")
    (task_dir / "dev-checklist.md").write_text("- [ ] item")

    result = _make_scanner(tmp_path).scan()
    assert len(result.tasks) == 1
    t = result.tasks[0]
    assert t.task_id == "my-task"
    assert t.stages["x-req"] == StageStatus.DONE


# ── 4. task 有 spec 引用 ──────────────────────────────────


def test_task_with_spec_ref(tmp_path: Path) -> None:
    """README.md 前 20 行包含 docs/my-spec/ → spec_ref = 'my-spec'。"""
    task_dir = tmp_path / "dev-pipeline" / "tasks" / "my-task"
    task_dir.mkdir(parents=True)
    (task_dir / "README.md").write_text(
        "# My Task\n\nSpec: docs/my-spec/01-goals-and-boundaries.md\n"
    )

    result = _make_scanner(tmp_path).scan()
    assert result.tasks[0].spec_ref == "my-spec"


# ── 5. task 无 spec 引用 ──────────────────────────────────


def test_task_without_spec_ref(tmp_path: Path) -> None:
    """README.md 不包含 docs/ 引用 → spec_ref = None。"""
    task_dir = tmp_path / "dev-pipeline" / "tasks" / "my-task"
    task_dir.mkdir(parents=True)
    (task_dir / "README.md").write_text("# My Task\n\nNo spec reference here.\n")

    result = _make_scanner(tmp_path).scan()
    assert result.tasks[0].spec_ref is None


# ── 6. 各阶段产物文件检测 ─────────────────────────────────


def test_dev_report_sets_xdev_done(tmp_path: Path) -> None:
    """dev-report.md 存在 → x-dev = done。"""
    task_dir = tmp_path / "dev-pipeline" / "tasks" / "my-task"
    task_dir.mkdir(parents=True)
    (task_dir / "README.md").write_text("# T")
    (task_dir / "dev-report.md").write_text("done")

    result = _make_scanner(tmp_path).scan()
    assert result.tasks[0].stages["x-dev"] == StageStatus.DONE


def test_verify_report_glob_sets_xverify_done(tmp_path: Path) -> None:
    """reports/verify/verify-report-20260509.md 存在 → x-verify = done（glob 匹配）。"""
    task_dir = tmp_path / "dev-pipeline" / "tasks" / "my-task"
    verify_dir = task_dir / "reports" / "verify"
    verify_dir.mkdir(parents=True)
    (task_dir / "README.md").write_text("# T")
    (verify_dir / "verify-report-20260509.md").write_text("ok")

    result = _make_scanner(tmp_path).scan()
    assert result.tasks[0].stages["x-verify"] == StageStatus.DONE


def test_no_artifacts_means_not_started(tmp_path: Path) -> None:
    """无产物文件 → 对应阶段 = not_started。"""
    task_dir = tmp_path / "dev-pipeline" / "tasks" / "my-task"
    task_dir.mkdir(parents=True)
    (task_dir / "README.md").write_text("# T")

    result = _make_scanner(tmp_path).scan()
    stages = result.tasks[0].stages
    assert stages["x-dev"] == StageStatus.NOT_STARTED
    assert stages["x-verify"] == StageStatus.NOT_STARTED
    assert stages["x-req"] == StageStatus.NOT_STARTED
    assert stages["x-qa-gate"] == StageStatus.NOT_STARTED
    assert stages["x-fix"] == StageStatus.NOT_STARTED


# ── 7. x-spec 阶段特殊逻辑 ───────────────────────────────


def test_xspec_done_when_spec_ref_and_spec_exists(tmp_path: Path) -> None:
    """task 有 spec_ref 且 spec 目录含 01-goals-and-boundaries.md → x-spec = done。"""
    # 建 spec
    spec_dir = tmp_path / "docs" / "my-spec"
    spec_dir.mkdir(parents=True)
    (spec_dir / "01-goals-and-boundaries.md").write_text("# Goals")

    # 建 task 引用该 spec
    task_dir = tmp_path / "dev-pipeline" / "tasks" / "my-task"
    task_dir.mkdir(parents=True)
    (task_dir / "README.md").write_text("# T\n\nSee docs/my-spec/ for details\n")

    result = _make_scanner(tmp_path).scan()
    assert result.tasks[0].stages["x-spec"] == StageStatus.DONE


def test_xspec_not_started_when_no_spec_ref(tmp_path: Path) -> None:
    """task 无 spec_ref → x-spec = not_started。"""
    task_dir = tmp_path / "dev-pipeline" / "tasks" / "my-task"
    task_dir.mkdir(parents=True)
    (task_dir / "README.md").write_text("# T\n\nNo spec here\n")

    result = _make_scanner(tmp_path).scan()
    assert result.tasks[0].stages["x-spec"] == StageStatus.NOT_STARTED


def test_xspec_not_started_when_spec_ref_but_no_goals_file(tmp_path: Path) -> None:
    """task 有 spec_ref 但 spec 目录无 01-goals-and-boundaries.md → x-spec = not_started。"""
    # spec 目录存在但无 goals 文件
    spec_dir = tmp_path / "docs" / "my-spec"
    spec_dir.mkdir(parents=True)

    task_dir = tmp_path / "dev-pipeline" / "tasks" / "my-task"
    task_dir.mkdir(parents=True)
    (task_dir / "README.md").write_text("# T\n\nRef: docs/my-spec/something\n")

    result = _make_scanner(tmp_path).scan()
    assert result.tasks[0].stages["x-spec"] == StageStatus.NOT_STARTED


# ── 8. README.md 标题提取 ─────────────────────────────────


def test_title_from_readme_heading(tmp_path: Path) -> None:
    """README.md 首行 `# My Task` → name = 'My Task'。"""
    task_dir = tmp_path / "dev-pipeline" / "tasks" / "my-task"
    task_dir.mkdir(parents=True)
    (task_dir / "README.md").write_text("# My Task\n\nDescription")

    result = _make_scanner(tmp_path).scan()
    assert result.tasks[0].name == "My Task"


def test_title_fallback_to_dirname(tmp_path: Path) -> None:
    """无 README.md → name = 目录名。"""
    task_dir = tmp_path / "dev-pipeline" / "tasks" / "my-task"
    task_dir.mkdir(parents=True)

    result = _make_scanner(tmp_path).scan()
    assert result.tasks[0].name == "my-task"


def test_title_fallback_when_no_heading(tmp_path: Path) -> None:
    """README.md 首行不是 # 标题 → name = 目录名。"""
    task_dir = tmp_path / "dev-pipeline" / "tasks" / "my-task"
    task_dir.mkdir(parents=True)
    (task_dir / "README.md").write_text("No heading here\n")

    result = _make_scanner(tmp_path).scan()
    assert result.tasks[0].name == "my-task"


# ── 补充覆盖 ──────────────────────────────────────────────


def test_spec_nested_under_subdirectory(tmp_path: Path) -> None:
    """docs/parent/child/01-goals-and-boundaries.md → spec_id = 'parent/child'。"""
    nested = tmp_path / "docs" / "parent" / "child"
    nested.mkdir(parents=True)
    (nested / "01-goals-and-boundaries.md").write_text("# G")

    result = _make_scanner(tmp_path).scan()
    assert any(s.spec_id == "parent/child" for s in result.specs)


def test_spec_optional_files_detection(tmp_path: Path) -> None:
    """spec 目录各可选文件的 has_xxx 字段检测。"""
    spec_dir = tmp_path / "docs" / "full-spec"
    spec_dir.mkdir(parents=True)
    (spec_dir / "01-goals-and-boundaries.md").write_text("# G")
    (spec_dir / "02-module-breakdown.md").write_text("# M")
    (spec_dir / "90-task-map.md").write_text("# T")
    (spec_dir / "architecture.html").write_text("<html/>")

    result = _make_scanner(tmp_path).scan()
    s = result.specs[0]
    assert s.has_goals is True
    assert s.has_module_breakdown is True
    assert s.has_task_map is True
    assert s.has_architecture is True


def test_spec_partial_files(tmp_path: Path) -> None:
    """spec 只有 goals，其余可选文件缺失 → 对应 has_xxx = False。"""
    spec_dir = tmp_path / "docs" / "partial"
    spec_dir.mkdir(parents=True)
    (spec_dir / "01-goals-and-boundaries.md").write_text("# G")

    result = _make_scanner(tmp_path).scan()
    s = result.specs[0]
    assert s.has_goals is True
    assert s.has_module_breakdown is False
    assert s.has_task_map is False
    assert s.has_architecture is False


def test_qa_gate_glob_detection(tmp_path: Path) -> None:
    """reports/qa-gate/qa-gate-report-*.md glob 匹配 → x-qa-gate = done。"""
    task_dir = tmp_path / "dev-pipeline" / "tasks" / "my-task"
    qa_dir = task_dir / "reports" / "qa-gate"
    qa_dir.mkdir(parents=True)
    (task_dir / "README.md").write_text("# T")
    (qa_dir / "qa-gate-report-20260509.md").write_text("pass")

    result = _make_scanner(tmp_path).scan()
    assert result.tasks[0].stages["x-qa-gate"] == StageStatus.DONE


def test_fix_glob_detection(tmp_path: Path) -> None:
    """reports/fix/fix-*.md glob 匹配 → x-fix = done。"""
    task_dir = tmp_path / "dev-pipeline" / "tasks" / "my-task"
    fix_dir = task_dir / "reports" / "fix"
    fix_dir.mkdir(parents=True)
    (task_dir / "README.md").write_text("# T")
    (fix_dir / "fix-001.md").write_text("fixed")

    result = _make_scanner(tmp_path).scan()
    assert result.tasks[0].stages["x-fix"] == StageStatus.DONE


def test_multiple_tasks_sorted(tmp_path: Path) -> None:
    """多个 task 按目录名排序返回。"""
    tasks_dir = tmp_path / "dev-pipeline" / "tasks"
    for name in ["c-task", "a-task", "b-task"]:
        d = tasks_dir / name
        d.mkdir(parents=True)
        (d / "README.md").write_text(f"# {name}")

    result = _make_scanner(tmp_path).scan()
    ids = [t.task_id for t in result.tasks]
    assert ids == ["a-task", "b-task", "c-task"]


def test_scan_result_has_scanned_at(tmp_path: Path) -> None:
    """ScanResult 包含 scanned_at 时间戳。"""
    result = _make_scanner(tmp_path).scan()
    assert result.scanned_at is not None
    assert len(result.scanned_at) > 0


def test_spec_ref_extracts_first_segment(tmp_path: Path) -> None:
    """docs/my-spec/sub/path → spec_ref 取第一段 'my-spec'。"""
    task_dir = tmp_path / "dev-pipeline" / "tasks" / "my-task"
    task_dir.mkdir(parents=True)
    (task_dir / "README.md").write_text("# T\n\nSee docs/my-spec/sub/path/file.md\n")

    result = _make_scanner(tmp_path).scan()
    assert result.tasks[0].spec_ref == "my-spec"


def test_spec_ref_ignores_lines_after_20(tmp_path: Path) -> None:
    """docs/ 引用出现在第 21 行之后 → spec_ref = None。"""
    task_dir = tmp_path / "dev-pipeline" / "tasks" / "my-task"
    task_dir.mkdir(parents=True)
    lines = ["# T"] + ["filler"] * 20 + ["docs/hidden-spec/file.md"]
    (task_dir / "README.md").write_text("\n".join(lines))

    result = _make_scanner(tmp_path).scan()
    assert result.tasks[0].spec_ref is None


def test_non_dir_in_tasks_ignored(tmp_path: Path) -> None:
    """dev-pipeline/tasks/ 下的普通文件不被当作 task。"""
    tasks_dir = tmp_path / "dev-pipeline" / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "random-file.txt").write_text("not a task")

    result = _make_scanner(tmp_path).scan()
    assert result.tasks == []


def test_non_dir_in_docs_ignored(tmp_path: Path) -> None:
    """docs/ 下的普通文件不被当作 spec。"""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "random-file.md").write_text("not a spec")

    result = _make_scanner(tmp_path).scan()
    assert result.specs == []
