"""Workspace Git 只读路由。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request

if TYPE_CHECKING:
    from web.threads.metadata import ThreadMetadata
    from web.threads.types import ThreadManagerProtocol

from web.errors import ThreadNotFoundError
from web.protocol import (
    WorkspaceGitActionResultDTO,
    WorkspaceGitBranchesDTO,
    WorkspaceGitCheckoutRequest,
    WorkspaceGitCommitRequest,
    WorkspaceGitCommitsDTO,
    WorkspaceGitCreateBranchRequest,
    WorkspaceGitFileDiffDTO,
    WorkspaceGitPathsRequest,
    WorkspaceGitStatusDTO,
)
from web.routers.threads import THREAD_ID_RE
from web.workspace import WorkspaceError, require_workspace_root
from web.workspace_git import (
    WorkspaceGitError,
    checkout_git_branch,
    commit_git,
    create_git_branch,
    read_git_branches,
    read_git_commits,
    read_git_file_diff,
    read_git_status,
    stage_git_paths,
    unstage_git_paths,
)

router = APIRouter(prefix="/api/threads", tags=["workspace-git"])


def _validate_thread_id(thread_id: str) -> None:
    if not THREAD_ID_RE.match(thread_id):
        raise HTTPException(status_code=422, detail="invalid thread id")


def _require_thread_meta(request: Request, thread_id: str) -> ThreadMetadata:
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    meta: ThreadMetadata | None = next(
        (item for item in tm.list_threads() if item.id == thread_id),
        None,
    )
    if meta is None:
        raise ThreadNotFoundError(f"thread not found: {thread_id}")
    return meta


@router.get("/{thread_id}/workspace-git/status")
async def get_workspace_git_status(thread_id: str, request: Request) -> WorkspaceGitStatusDTO:
    _validate_thread_id(thread_id)
    try:
        meta = await asyncio.to_thread(_require_thread_meta, request, thread_id)
        root = require_workspace_root(meta)
        payload = await asyncio.to_thread(read_git_status, root)
    except ThreadNotFoundError:
        raise
    except (WorkspaceError, WorkspaceGitError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WorkspaceGitStatusDTO.model_validate(payload)


@router.get("/{thread_id}/workspace-git/branches")
async def get_workspace_git_branches(
    thread_id: str,
    request: Request,
) -> WorkspaceGitBranchesDTO:
    _validate_thread_id(thread_id)
    try:
        meta = await asyncio.to_thread(_require_thread_meta, request, thread_id)
        root = require_workspace_root(meta)
        payload = await asyncio.to_thread(read_git_branches, root)
    except ThreadNotFoundError:
        raise
    except (WorkspaceError, WorkspaceGitError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WorkspaceGitBranchesDTO.model_validate(payload)


@router.get("/{thread_id}/workspace-git/commits")
async def get_workspace_git_commits(
    thread_id: str,
    request: Request,
) -> WorkspaceGitCommitsDTO:
    _validate_thread_id(thread_id)
    try:
        meta = await asyncio.to_thread(_require_thread_meta, request, thread_id)
        root = require_workspace_root(meta)
        payload = await asyncio.to_thread(read_git_commits, root)
    except ThreadNotFoundError:
        raise
    except (WorkspaceError, WorkspaceGitError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WorkspaceGitCommitsDTO.model_validate(payload)


@router.get("/{thread_id}/workspace-git/file-diff")
async def get_workspace_git_file_diff(
    thread_id: str,
    request: Request,
    path: str,
) -> WorkspaceGitFileDiffDTO:
    _validate_thread_id(thread_id)
    try:
        meta = await asyncio.to_thread(_require_thread_meta, request, thread_id)
        root = require_workspace_root(meta)
        payload = await asyncio.to_thread(read_git_file_diff, root, path)
    except ThreadNotFoundError:
        raise
    except (WorkspaceError, WorkspaceGitError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WorkspaceGitFileDiffDTO.model_validate(payload)


@router.post("/{thread_id}/workspace-git/stage")
async def post_workspace_git_stage(
    thread_id: str,
    request: Request,
    body: WorkspaceGitPathsRequest,
) -> WorkspaceGitActionResultDTO:
    _validate_thread_id(thread_id)
    try:
        meta = await asyncio.to_thread(_require_thread_meta, request, thread_id)
        root = require_workspace_root(meta)
        payload = await asyncio.to_thread(stage_git_paths, root, body.paths)
    except ThreadNotFoundError:
        raise
    except (WorkspaceError, WorkspaceGitError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WorkspaceGitActionResultDTO.model_validate(payload)


@router.post("/{thread_id}/workspace-git/unstage")
async def post_workspace_git_unstage(
    thread_id: str,
    request: Request,
    body: WorkspaceGitPathsRequest,
) -> WorkspaceGitActionResultDTO:
    _validate_thread_id(thread_id)
    try:
        meta = await asyncio.to_thread(_require_thread_meta, request, thread_id)
        root = require_workspace_root(meta)
        payload = await asyncio.to_thread(unstage_git_paths, root, body.paths)
    except ThreadNotFoundError:
        raise
    except (WorkspaceError, WorkspaceGitError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WorkspaceGitActionResultDTO.model_validate(payload)


@router.post("/{thread_id}/workspace-git/checkout")
async def post_workspace_git_checkout(
    thread_id: str,
    request: Request,
    body: WorkspaceGitCheckoutRequest,
) -> WorkspaceGitActionResultDTO:
    _validate_thread_id(thread_id)
    try:
        meta = await asyncio.to_thread(_require_thread_meta, request, thread_id)
        root = require_workspace_root(meta)
        payload = await asyncio.to_thread(checkout_git_branch, root, body.branch)
    except ThreadNotFoundError:
        raise
    except (WorkspaceError, WorkspaceGitError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WorkspaceGitActionResultDTO.model_validate(payload)


@router.post("/{thread_id}/workspace-git/create-branch")
async def post_workspace_git_create_branch(
    thread_id: str,
    request: Request,
    body: WorkspaceGitCreateBranchRequest,
) -> WorkspaceGitActionResultDTO:
    _validate_thread_id(thread_id)
    try:
        meta = await asyncio.to_thread(_require_thread_meta, request, thread_id)
        root = require_workspace_root(meta)
        payload = await asyncio.to_thread(
            create_git_branch, root, body.branch, checkout=body.checkout
        )
    except ThreadNotFoundError:
        raise
    except (WorkspaceError, WorkspaceGitError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WorkspaceGitActionResultDTO.model_validate(payload)


@router.post("/{thread_id}/workspace-git/commit")
async def post_workspace_git_commit(
    thread_id: str,
    request: Request,
    body: WorkspaceGitCommitRequest,
) -> WorkspaceGitActionResultDTO:
    _validate_thread_id(thread_id)
    try:
        meta = await asyncio.to_thread(_require_thread_meta, request, thread_id)
        root = require_workspace_root(meta)
        payload = await asyncio.to_thread(commit_git, root, body.message)
    except ThreadNotFoundError:
        raise
    except (WorkspaceError, WorkspaceGitError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WorkspaceGitActionResultDTO.model_validate(payload)


__all__ = ["router"]
