"""Codex REST 端点（v0.1）。

前缀 ``/api/codex``，提供项目列表 / 单会话历史回放 / 单会话元数据。

与 ``src/web/codex/route.py``（WebSocket 端点）平级；
本文件只做 REST，不做 WebSocket。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from web.codex.jsonl_history import parse_codex_rollout, read_session_meta
from web.codex.projects_scanner import list_codex_projects

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/codex", tags=["codex"])


@router.get("/projects")
async def get_projects() -> JSONResponse:
    """列出所有 Codex 项目（按 cwd 分组）。"""
    projects = await asyncio.to_thread(list_codex_projects)
    return JSONResponse([asdict(p) for p in projects])


@router.get("/sessions/{session_id}/history")
async def get_session_history(session_id: str) -> JSONResponse:
    """单会话历史回放 → NormalizedMessage[]。"""
    rollout_path = await asyncio.to_thread(_find_rollout_by_session_id, session_id)
    if rollout_path is None:
        return JSONResponse({"error": f"session {session_id} not found"}, status_code=404)
    messages = await asyncio.to_thread(parse_codex_rollout, rollout_path, session_id)
    return JSONResponse({"messages": messages})


@router.get("/sessions/{session_id}/metadata")
async def get_session_metadata(session_id: str) -> JSONResponse:
    """单会话元数据（cwd / model / cli_version）。"""
    rollout_path = await asyncio.to_thread(_find_rollout_by_session_id, session_id)
    if rollout_path is None:
        return JSONResponse({"error": f"session {session_id} not found"}, status_code=404)
    meta = await asyncio.to_thread(read_session_meta, rollout_path)
    if meta is None:
        return JSONResponse({"error": "session_meta not found in rollout"}, status_code=404)
    return JSONResponse({"metadata": meta})


def _find_rollout_by_session_id(
    session_id: str,
    codex_home: Path | None = None,
) -> Path | None:
    """根据 session_id 在 ~/.codex/sessions/ 树里找到对应的 rollout 文件。

    扫盘查找——从 projects_scanner 的扫盘结果反查。
    """
    projects = list_codex_projects(codex_home)
    for project in projects:
        for session in project.sessions:
            if session.session_id == session_id:
                return Path(session.rollout_path)
    return None


__all__ = ["router"]
