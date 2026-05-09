"""文件系统扫描器：检测 spec 与 task 产物，生成当前快照。"""

from __future__ import annotations

import re
from pathlib import Path

from web.workflow.models import (
    NodeTemplate,
    ScanResult,
    SpecSnapshot,
    StageStatus,
    TaskSnapshot,
)

_HEADING_RE = re.compile(r"^#\s+(.+)")
_SPEC_REF_RE = re.compile(r"docs/([^\s)]+)")


class WorkflowScanner:
    def __init__(self, project_root: Path, node_templates: dict[str, NodeTemplate]):
        self._root = project_root
        self._templates = node_templates

    def scan(self) -> ScanResult:
        specs = self._scan_specs()
        spec_ids = {s.spec_id for s in specs}
        tasks = self._scan_tasks(spec_ids)
        return ScanResult(specs=specs, tasks=tasks)

    # ── specs ──────────────────────────────────────────────

    def _scan_specs(self) -> list[SpecSnapshot]:
        docs_dir = self._root / "docs"
        if not docs_dir.is_dir():
            return []
        results: list[SpecSnapshot] = []
        for child in sorted(docs_dir.iterdir()):
            if not child.is_dir():
                continue
            self._try_collect_spec(child, results)
            for grandchild in sorted(child.iterdir()):
                if grandchild.is_dir():
                    self._try_collect_spec(grandchild, results)
        return results

    def _try_collect_spec(self, d: Path, out: list[SpecSnapshot]) -> None:
        if not (d / "01-goals-and-boundaries.md").exists():
            return
        spec_id = str(d.relative_to(self._root / "docs"))
        out.append(
            SpecSnapshot(
                spec_id=spec_id,
                name=spec_id,
                path=str(d),
                has_goals=True,
                has_module_breakdown=(d / "02-module-breakdown.md").exists(),
                has_task_map=(d / "90-task-map.md").exists(),
                has_architecture=(d / "architecture.html").exists(),
            )
        )

    # ── tasks ──────────────────────────────────────────────

    def _scan_tasks(self, spec_ids: set[str]) -> list[TaskSnapshot]:
        tasks_dir = self._root / "dev-pipeline" / "tasks"
        if not tasks_dir.is_dir():
            return []
        results: list[TaskSnapshot] = []
        for child in sorted(tasks_dir.iterdir()):
            if not child.is_dir():
                continue
            name = self._extract_title(child)
            spec_ref = self._extract_spec_ref(child)
            stages = self._detect_stages(child, spec_ref)
            results.append(
                TaskSnapshot(
                    task_id=child.name,
                    name=name,
                    path=str(child),
                    spec_ref=spec_ref,
                    stages=stages,
                )
            )
        return results

    def _extract_title(self, task_dir: Path) -> str:
        readme = task_dir / "README.md"
        if not readme.exists():
            return task_dir.name
        try:
            line = readme.read_text(encoding="utf-8").split("\n", 1)[0]
        except OSError:
            return task_dir.name
        m = _HEADING_RE.match(line)
        return m.group(1).strip() if m else task_dir.name

    def _extract_spec_ref(self, task_dir: Path) -> str | None:
        readme = task_dir / "README.md"
        if not readme.exists():
            return None
        try:
            lines = readme.read_text(encoding="utf-8").splitlines()[:20]
        except OSError:
            return None
        for line in lines:
            m = _SPEC_REF_RE.search(line)
            if m:
                raw = m.group(1).rstrip("/")
                return raw.split("/")[0]
        return None

    def _detect_stages(self, task_dir: Path, spec_ref: str | None) -> dict[str, StageStatus]:
        stages: dict[str, StageStatus] = {}
        for tid, tmpl in self._templates.items():
            if tmpl.detection_file is None:
                continue
            if tid == "x-spec":
                stages[tid] = self._detect_spec_stage(spec_ref)
                continue
            stages[tid] = self._detect_file(task_dir, tmpl.detection_file)
        return stages

    def _detect_spec_stage(self, spec_ref: str | None) -> StageStatus:
        if spec_ref is None:
            return StageStatus.NOT_STARTED
        spec_dir = self._root / "docs" / spec_ref
        if (spec_dir / "01-goals-and-boundaries.md").exists():
            return StageStatus.DONE
        return StageStatus.NOT_STARTED

    def _detect_file(self, base: Path, detection_file: str) -> StageStatus:
        if "*" in detection_file or "?" in detection_file:
            matches = list(base.glob(detection_file))
            return StageStatus.DONE if matches else StageStatus.NOT_STARTED
        return StageStatus.DONE if (base / detection_file).exists() else StageStatus.NOT_STARTED
