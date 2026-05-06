"""GET /api/slash-candidates — 返回 commands + skills 的合并候选列表。"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api", tags=["slash-candidates"])


@router.get("/slash-candidates")
async def list_slash_candidates(request: Request) -> list[dict[str, str]]:
    return getattr(request.app.state, "slash_candidates", [])
