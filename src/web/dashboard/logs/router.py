"""Log viewer REST endpoints (full-log-v0.2).

Two read-only endpoints under ``/api/manage/logs``:

=============  =====================================  ================
HTTP endpoint  Purpose                               Status codes
=============  =====================================  ================
GET /sources   List all registered log sources        200
GET /read      Tail-read a single log source          200 / 400
=============  =====================================  ================

Design:

- **Thin protocol layer** — all I/O and parsing logic lives in
  :class:`~web.dashboard.logs.registry.LogSourceRegistry` and
  :class:`~web.dashboard.logs.service.LogReadService`.
- **Stateless per-request** — dependencies fetched from
  ``request.app.state`` (injected during :func:`web.app.create_app`).
- **DTO conversion** — internal dataclasses are mapped 1:1 to pydantic
  DTOs via ``_source_to_dto`` so the protocol layer stays decoupled from
  domain models.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from web.dashboard.logs.registry import ResolvedLogSource
from web.dashboard.logs.service import LogReadService
from web.protocol.log_dto import (
    LogReadResponseDTO,
    LogSourceDTO,
)

router = APIRouter(prefix="/api/manage/logs", tags=["manage-logs"])


# ---------------------------------------------------------------------------
# DTO conversion helpers
# ---------------------------------------------------------------------------


def _source_to_dto(src: ResolvedLogSource) -> LogSourceDTO:
    """Convert internal :class:`ResolvedLogSource` to wire DTO."""
    return LogSourceDTO(
        type=src.type,
        label=src.label,
        format=src.format,
        description=src.description,
        path=src.path,
        exists=src.exists,
        size_bytes=src.size_bytes,
        updated_at_ms=src.updated_at_ms,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/sources", response_model=list[LogSourceDTO])
async def list_log_sources(request: Request) -> list[LogSourceDTO]:
    """List all registered log sources with live file-system metadata."""
    registry = request.app.state.log_source_registry
    sources = registry.list_sources()
    return [_source_to_dto(s) for s in sources]


@router.get("/read", response_model=LogReadResponseDTO)
async def read_log(
    type: str = Query(..., description="日志类型"),
    tail_lines: int = Query(500, ge=1, le=5000),
    max_bytes: int = Query(524288, ge=1024, le=5242880),
    query: str = Query("", max_length=200),
    request: Request = None,  # type: ignore[assignment]
) -> LogReadResponseDTO:
    """Tail-read a single log source with optional filtering.

    Raises:
        HTTPException 400: unknown log source *type*.
    """
    service: LogReadService = request.app.state.log_read_service
    try:
        return service.read_tail(type, tail_lines, max_bytes, query)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


__all__ = ["router"]
