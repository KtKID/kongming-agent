"""Thread-scoped Agent Workflow Viewer REST 路由。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Query, Request

from hosts.web.errors import (
    InvalidThreadIdError,
    KongmingWebError,
    ThreadNotFoundError,
)
from hosts.web.routers.threads import THREAD_ID_RE
from hosts.web.workflow_viewer import WorkflowRunViewerManager

if TYPE_CHECKING:
    from hosts.web.threads.types import ThreadManagerProtocol

router = APIRouter(prefix="/api/threads", tags=["agent-workflows"])


class InvalidWorkflowViewerRequestError(KongmingWebError):
    status_code = 422


class WorkflowNotFoundError(KongmingWebError):
    status_code = 404


def _validate_thread_id(thread_id: str) -> None:
    if not THREAD_ID_RE.match(thread_id):
        raise InvalidThreadIdError(
            f"invalid thread_id: {thread_id!r}; must match ^thread-[a-f0-9]{{12}}$"
        )


async def _require_thread(request: Request, thread_id: str) -> None:
    _validate_thread_id(thread_id)
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    metas = await asyncio.to_thread(tm.list_threads)
    if not any(meta.id == thread_id for meta in metas):
        raise ThreadNotFoundError(f"thread not found: {thread_id}")


def _manager(request: Request) -> WorkflowRunViewerManager:
    return WorkflowRunViewerManager(
        config=request.app.state.config,
        workspace_root=Path(request.app.state.workspace_root),
    )


@router.get("/{thread_id}/agent-workflows")
async def list_agent_workflows(thread_id: str, request: Request) -> dict[str, object]:
    await _require_thread(request, thread_id)
    return _manager(request).list_workflows(thread_id).model_dump()


@router.get("/{thread_id}/agent-workflows/{workflow_id}")
async def get_agent_workflow_detail(
    thread_id: str, workflow_id: str, request: Request
) -> dict[str, object]:
    await _require_thread(request, thread_id)
    try:
        return _manager(request).get_workflow_detail(thread_id, workflow_id).model_dump()
    except FileNotFoundError as exc:
        raise WorkflowNotFoundError(str(exc)) from exc
    except ValueError as exc:
        raise InvalidWorkflowViewerRequestError(str(exc)) from exc


@router.get("/{thread_id}/agent-workflows/{workflow_id}/subagents/{task_run_id}/conversation")
async def get_agent_workflow_conversation(
    thread_id: str,
    workflow_id: str,
    task_run_id: str,
    request: Request,
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=300),
) -> dict[str, object]:
    await _require_thread(request, thread_id)
    try:
        return (
            _manager(request)
            .load_conversation(
                thread_id=thread_id,
                workflow_id=workflow_id,
                task_run_id=task_run_id,
                cursor=cursor,
                limit=limit,
            )
            .model_dump()
        )
    except FileNotFoundError as exc:
        raise WorkflowNotFoundError(str(exc)) from exc
    except ValueError as exc:
        raise InvalidWorkflowViewerRequestError(str(exc)) from exc


@router.get("/{thread_id}/agent-workflows/{workflow_id}/artifacts/{artifact_id}")
async def get_agent_workflow_artifact(
    thread_id: str,
    workflow_id: str,
    artifact_id: str,
    request: Request,
) -> dict[str, object]:
    await _require_thread(request, thread_id)
    try:
        return (
            _manager(request)
            .read_artifact(
                thread_id=thread_id,
                workflow_id=workflow_id,
                artifact_id=artifact_id,
            )
            .model_dump()
        )
    except FileNotFoundError as exc:
        raise WorkflowNotFoundError(str(exc)) from exc
    except ValueError as exc:
        raise InvalidWorkflowViewerRequestError(str(exc)) from exc
