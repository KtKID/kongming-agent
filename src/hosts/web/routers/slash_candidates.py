"""GET /api/slash-candidates — 返回 commands + skills 的合并候选列表。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from hosts.web.routers.slash_catalog import (
    build_slash_catalog_context,
    get_slash_catalog_manager,
)

router = APIRouter(prefix="/api", tags=["slash-candidates"])


@router.get("/slash-candidates")
async def list_slash_candidates(request: Request) -> list[dict[str, str]]:
    manager = get_slash_catalog_manager(request)
    context = await build_slash_catalog_context(request, thread_id=None)
    return await manager.list_legacy_candidates(context)
