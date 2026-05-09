"""Workflow Dashboard REST 端点。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from web.workflow.service import WorkflowService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workflow", tags=["workflow"])


def _svc(request: Request) -> WorkflowService:
    svc = getattr(request.app.state, "workflow_service", None)
    if svc is None:
        raise HTTPException(503, "Workflow service not initialized")
    return svc  # type: ignore[no-any-return]


# ── definitions ──────────────────────────────────────────


@router.get("/definitions")
async def list_definitions(request: Request) -> list[dict[str, Any]]:
    from dataclasses import asdict

    defs = _svc(request).list_definitions()
    return [asdict(d) for d in defs]


@router.get("/definitions/{definition_id}")
async def get_definition(request: Request, definition_id: str) -> dict[str, Any]:
    from dataclasses import asdict

    d = _svc(request).get_definition(definition_id)
    if d is None:
        raise HTTPException(404, f"Definition not found: {definition_id}")
    return asdict(d)


# ── runs ─────────────────────────────────────────────────


@router.get("/runs")
async def list_runs(request: Request) -> list[dict[str, Any]]:
    from dataclasses import asdict

    runs = _svc(request).list_runs()
    return [asdict(r) for r in runs]


@router.post("/runs", status_code=201)
async def create_run(request: Request) -> dict[str, Any]:
    from dataclasses import asdict

    body = await request.json()
    task_id = body.get("task_id")
    workflow_id = body.get("workflow_id")
    spec_id = body.get("spec_id")
    if not task_id or not workflow_id:
        raise HTTPException(400, "task_id and workflow_id are required")
    try:
        run = _svc(request).create_run(task_id, workflow_id, spec_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return asdict(run)


@router.patch("/runs/{run_id}")
async def update_run_node(request: Request, run_id: str) -> dict[str, Any]:
    from dataclasses import asdict

    body = await request.json()
    node_id = body.get("node_id")
    status = body.get("status")
    if not node_id or not status:
        raise HTTPException(400, "node_id and status are required")
    try:
        from web.workflow.models import StageStatus

        run = _svc(request).update_run_node(run_id, node_id, StageStatus(status))
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return asdict(run)


# ── events ───────────────────────────────────────────────


@router.get("/runs/{run_id}/events")
async def list_events(request: Request, run_id: str) -> list[dict[str, Any]]:
    from dataclasses import asdict

    events = _svc(request).list_events(run_id=run_id)
    return [asdict(e) for e in events]


# ── scanner ──────────────────────────────────────────────


@router.get("/scanner/status")
async def scanner_status(request: Request) -> dict[str, Any]:
    svc = _svc(request)
    scan = svc.last_scan
    from dataclasses import asdict

    return {
        "last_scan_at": scan.scanned_at if scan else None,
        "scan_interval_seconds": getattr(request.app.state, "workflow_scan_interval", 30.0),
        "specs": [asdict(s) for s in scan.specs] if scan else [],
        "tasks": [asdict(t) for t in scan.tasks] if scan else [],
    }


@router.post("/scanner/trigger")
async def trigger_scan(request: Request) -> dict[str, Any]:
    svc = _svc(request)
    result = svc.scan()
    return {
        "scanned_at": result.scanned_at,
        "specs_count": len(result.specs),
        "tasks_count": len(result.tasks),
    }
