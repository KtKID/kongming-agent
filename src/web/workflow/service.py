"""Workflow Dashboard 业务逻辑层：串联 Scanner + Store。"""

from __future__ import annotations

from pathlib import Path

from web.workflow.models import (
    EventSource,
    RunStatus,
    ScanResult,
    StageStatus,
    WorkflowDefinition,
    WorkflowEvent,
    WorkflowRun,
    _now_iso,
    new_id,
)
from web.workflow.scanner import WorkflowScanner
from web.workflow.store import WorkflowStore
from web.workflow.templates import get_node_templates, get_preset_definitions


class WorkflowService:
    def __init__(self, project_root: Path, store: WorkflowStore) -> None:
        self._templates = get_node_templates()
        self._scanner = WorkflowScanner(project_root, self._templates)
        self._store = store
        self._last_scan: ScanResult | None = None
        self._ensure_presets()

    # ── presets bootstrap ────────────────────────────────────

    def _ensure_presets(self) -> None:
        existing = self._store.load_definitions()
        existing_ids = {d.id for d in existing}
        presets = get_preset_definitions()
        added = False
        for preset in presets:
            if preset.id not in existing_ids:
                existing.append(preset)
                added = True
        if added:
            self._store.save_definitions(existing)

    # ── scan ─────────────────────────────────────────────────

    def scan(self) -> ScanResult:
        result = self._scanner.scan()
        self._last_scan = result
        self._sync_runs_from_scan(result)
        return result

    @property
    def last_scan(self) -> ScanResult | None:
        return self._last_scan

    _DEFAULT_WORKFLOW_ID = "full-pipeline"

    def _sync_runs_from_scan(self, scan: ScanResult) -> None:
        runs = self._store.load_runs()
        task_stages = {t.task_id: t.stages for t in scan.tasks}
        runs_by_task = {r.task_id: r for r in runs if r.status != RunStatus.COMPLETED}
        changed = False

        default_def = self._get_definition(self._DEFAULT_WORKFLOW_ID)
        if default_def:
            for task in scan.tasks:
                if task.task_id not in runs_by_task:
                    node_states = {
                        n.id: task.stages.get(n.template_id, StageStatus.NOT_STARTED)
                        for n in default_def.nodes
                    }
                    run = WorkflowRun(
                        id=new_id(),
                        workflow_id=self._DEFAULT_WORKFLOW_ID,
                        task_id=task.task_id,
                        spec_id=task.spec_ref,
                        node_states=node_states,
                        status=RunStatus.ACTIVE,
                    )
                    runs.append(run)
                    runs_by_task[task.task_id] = run
                    changed = True

        for run in runs:
            if run.task_id not in task_stages:
                continue
            stages = task_stages[run.task_id]
            definition = self._get_definition(run.workflow_id)
            if definition is None:
                continue
            for node in definition.nodes:
                tmpl_id = node.template_id
                stage_status = stages.get(tmpl_id, StageStatus.NOT_STARTED)
                old = run.node_states.get(node.id, StageStatus.NOT_STARTED)
                if stage_status != old:
                    run.node_states[node.id] = stage_status
                    run.updated_at = _now_iso()
                    changed = True
                    self._store.append_event(
                        WorkflowEvent(
                            event_id=f"{_now_iso()}-{new_id()[:8]}",
                            run_id=run.id,
                            node_id=node.id,
                            old_status=old,
                            new_status=stage_status,
                            source=EventSource.SCANNER,
                        )
                    )
            # check if all nodes done → completed
            if (
                all(run.node_states.get(n.id) == StageStatus.DONE for n in definition.nodes)
                and run.status != RunStatus.COMPLETED
            ):
                run.status = RunStatus.COMPLETED
                run.updated_at = _now_iso()
                changed = True

        if changed:
            self._store.save_runs(runs)

    # ── definitions ──────────────────────────────────────────

    def list_definitions(self) -> list[WorkflowDefinition]:
        return self._store.load_definitions()

    def get_definition(self, definition_id: str) -> WorkflowDefinition | None:
        return self._get_definition(definition_id)

    def _get_definition(self, definition_id: str) -> WorkflowDefinition | None:
        for d in self._store.load_definitions():
            if d.id == definition_id:
                return d
        return None

    # ── runs ─────────────────────────────────────────────────

    def list_runs(self) -> list[WorkflowRun]:
        return self._store.load_runs()

    def get_run(self, run_id: str) -> WorkflowRun | None:
        for r in self._store.load_runs():
            if r.id == run_id:
                return r
        return None

    def create_run(
        self,
        task_id: str,
        workflow_id: str,
        spec_id: str | None = None,
    ) -> WorkflowRun:
        definition = self._get_definition(workflow_id)
        if definition is None:
            raise ValueError(f"Workflow definition not found: {workflow_id}")

        runs = self._store.load_runs()
        for r in runs:
            if r.task_id == task_id and r.status != RunStatus.COMPLETED:
                raise ValueError(f"Task already has an active run: {task_id}")

        node_states = {node.id: StageStatus.NOT_STARTED for node in definition.nodes}

        run = WorkflowRun(
            id=new_id(),
            workflow_id=workflow_id,
            task_id=task_id,
            spec_id=spec_id,
            node_states=node_states,
            status=RunStatus.ACTIVE,
        )
        runs.append(run)
        self._store.save_runs(runs)
        return run

    def update_run_node(
        self,
        run_id: str,
        node_id: str,
        status: StageStatus,
    ) -> WorkflowRun:
        runs = self._store.load_runs()
        target = None
        for r in runs:
            if r.id == run_id:
                target = r
                break
        if target is None:
            raise ValueError(f"Run not found: {run_id}")

        old = target.node_states.get(node_id, StageStatus.NOT_STARTED)
        if old != status:
            target.node_states[node_id] = status
            target.updated_at = _now_iso()
            self._store.save_runs(runs)
            self._store.append_event(
                WorkflowEvent(
                    event_id=f"{_now_iso()}-{new_id()[:8]}",
                    run_id=run_id,
                    node_id=node_id,
                    old_status=old,
                    new_status=status,
                    source=EventSource.MANUAL,
                )
            )
        return target

    # ── pin ──────────────────────────────────────────────────

    def toggle_pin(self, task_id: str) -> bool:
        """Toggle .pinned marker file for a task. Returns new pinned state."""
        task_dir = self._scanner._root / "dev-pipeline" / "tasks" / task_id
        if not task_dir.is_dir():
            raise FileNotFoundError(f"Task directory not found: {task_id}")
        pin_file = task_dir / ".pinned"
        if pin_file.exists():
            pin_file.unlink()
            return False
        else:
            pin_file.touch()
            return True

    # ── events ───────────────────────────────────────────────

    def list_events(self, run_id: str | None = None) -> list[WorkflowEvent]:
        return self._store.load_events(run_id=run_id)
