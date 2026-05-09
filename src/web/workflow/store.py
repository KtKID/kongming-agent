from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict
from enum import Enum
from pathlib import Path
from typing import Any

from web.workflow.models import (
    EdgeTrigger,
    EventSource,
    RunStatus,
    StageStatus,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowEvent,
    WorkflowNode,
    WorkflowRun,
    _now_iso,
)

SCHEMA_VERSION = 1

_DEFINITIONS_FILENAME = "definitions.json"
_RUNS_FILENAME = "runs.json"
_EVENTS_FILENAME = "events.jsonl"


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _atomic_write_json(path: Path, payload: Any) -> None:
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".wf_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=_json_default)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "updated_at": _now_iso(), "items": []}
    with open(path, encoding="utf-8") as f:
        result: dict[str, Any] = json.load(f)
        return result


# ── dict ↔ dataclass ──────────────────────────────────────


def _def_to_dict(d: WorkflowDefinition) -> dict[str, Any]:
    raw = asdict(d)
    for _node in raw["nodes"]:
        pass
    for edge in raw["edges"]:
        edge["trigger"] = EdgeTrigger(edge["trigger"]).value
    return raw


def _dict_to_def(raw: dict[str, Any]) -> WorkflowDefinition:
    nodes = [WorkflowNode(**n) for n in raw.get("nodes", [])]
    edges = [
        WorkflowEdge(
            id=e["id"],
            from_node_id=e["from_node_id"],
            to_node_id=e["to_node_id"],
            trigger=EdgeTrigger(e["trigger"]),
        )
        for e in raw.get("edges", [])
    ]
    return WorkflowDefinition(
        id=raw["id"],
        name=raw["name"],
        description=raw["description"],
        nodes=nodes,
        edges=edges,
        is_preset=raw.get("is_preset", False),
        created_at=raw.get("created_at", ""),
    )


def _run_to_dict(r: WorkflowRun) -> dict[str, Any]:
    raw = asdict(r)
    raw["status"] = RunStatus(raw["status"]).value
    raw["node_states"] = {k: StageStatus(v).value for k, v in raw["node_states"].items()}
    return raw


def _dict_to_run(raw: dict[str, Any]) -> WorkflowRun:
    return WorkflowRun(
        id=raw["id"],
        workflow_id=raw["workflow_id"],
        task_id=raw["task_id"],
        spec_id=raw.get("spec_id"),
        node_states={k: StageStatus(v) for k, v in raw.get("node_states", {}).items()},
        status=RunStatus(raw.get("status", "active")),
        created_at=raw.get("created_at", ""),
        updated_at=raw.get("updated_at", ""),
    )


def _event_to_dict(e: WorkflowEvent) -> dict[str, Any]:
    raw = asdict(e)
    raw["old_status"] = StageStatus(raw["old_status"]).value
    raw["new_status"] = StageStatus(raw["new_status"]).value
    raw["source"] = EventSource(raw["source"]).value
    return raw


def _dict_to_event(raw: dict[str, Any]) -> WorkflowEvent:
    return WorkflowEvent(
        event_id=raw["event_id"],
        run_id=raw["run_id"],
        node_id=raw["node_id"],
        old_status=StageStatus(raw["old_status"]),
        new_status=StageStatus(raw["new_status"]),
        source=EventSource(raw.get("source", "scanner")),
        timestamp=raw.get("timestamp", ""),
    )


# ── Store ─────────────────────────────────────────────────


class WorkflowStore:
    def __init__(self, home: Path) -> None:
        self._home = Path(home).resolve()
        self._home.mkdir(parents=True, exist_ok=True)
        self._defs_path = self._home / _DEFINITIONS_FILENAME
        self._runs_path = self._home / _RUNS_FILENAME
        self._events_path = self._home / _EVENTS_FILENAME
        self._bootstrap()

    def _bootstrap(self) -> None:
        for path in (self._defs_path, self._runs_path):
            if not path.exists():
                _atomic_write_json(
                    path,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "updated_at": _now_iso(),
                        "items": [],
                    },
                )
        if not self._events_path.exists():
            self._events_path.touch()

    # ── definitions ───────────────────────────────────────

    def load_definitions(self) -> list[WorkflowDefinition]:
        payload = _load_json(self._defs_path)
        return [_dict_to_def(item) for item in payload.get("items", [])]

    def save_definitions(self, defs: list[WorkflowDefinition]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _now_iso(),
            "items": [_def_to_dict(d) for d in defs],
        }
        _atomic_write_json(self._defs_path, payload)

    # ── runs ──────────────────────────────────────────────

    def load_runs(self) -> list[WorkflowRun]:
        payload = _load_json(self._runs_path)
        return [_dict_to_run(item) for item in payload.get("items", [])]

    def save_runs(self, runs: list[WorkflowRun]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _now_iso(),
            "items": [_run_to_dict(r) for r in runs],
        }
        _atomic_write_json(self._runs_path, payload)

    # ── events ────────────────────────────────────────────

    def append_event(self, event: WorkflowEvent) -> None:
        line = json.dumps(_event_to_dict(event), ensure_ascii=False, default=_json_default)
        with open(self._events_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def load_events(self, run_id: str | None = None) -> list[WorkflowEvent]:
        if not self._events_path.exists():
            return []
        events: list[WorkflowEvent] = []
        with open(self._events_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if run_id is not None and raw.get("run_id") != run_id:
                    continue
                events.append(_dict_to_event(raw))
        return events
