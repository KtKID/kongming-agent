"""Thread-scoped artifact viewer REST 路由。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

from hosts.web.errors import (
    InvalidThreadIdError,
    KongmingWebError,
    ThreadNotFoundError,
)
from hosts.web.routers.threads import THREAD_ID_RE
from hosts.web.thread_artifacts import ThreadArtifactManager

if TYPE_CHECKING:
    from hosts.web.threads.types import ThreadManagerProtocol

router = APIRouter(prefix="/api/threads", tags=["thread-artifacts"])


class InvalidThreadArtifactRequestError(KongmingWebError):
    status_code = 422


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


def _manager(request: Request) -> ThreadArtifactManager:
    return ThreadArtifactManager(config=request.app.state.config)


@router.get("/{thread_id}/artifacts")
async def list_thread_artifacts(thread_id: str, request: Request) -> dict[str, object]:
    await _require_thread(request, thread_id)
    return _manager(request).list_artifacts(thread_id).model_dump()


@router.get("/{thread_id}/artifacts/{artifact_id}")
async def get_thread_artifact(
    thread_id: str, artifact_id: str, request: Request
) -> dict[str, object]:
    await _require_thread(request, thread_id)
    try:
        return (
            _manager(request)
            .read_artifact(
                thread_id=thread_id,
                artifact_id=artifact_id,
            )
            .model_dump()
        )
    except ValueError as exc:
        raise InvalidThreadArtifactRequestError(str(exc)) from exc
