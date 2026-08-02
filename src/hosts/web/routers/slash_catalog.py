"""Slash catalog REST routes。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query, Request

from hosts.web.slash_catalog import (
    SlashCatalogBackendKind,
    SlashCatalogContext,
    SlashCatalogGroupItemsResponseDTO,
    SlashCatalogGroupNotFound,
    SlashCatalogGroupsResponseDTO,
    SlashCatalogManager,
)
from infrastructure.config.paths import get_kongming_home

router = APIRouter(prefix="/api", tags=["slash-catalog"])


@router.get("/slash-catalog", response_model=SlashCatalogGroupsResponseDTO)
async def list_slash_catalog(
    request: Request,
    thread_id: str | None = Query(default=None),
) -> SlashCatalogGroupsResponseDTO:
    """返回 slash catalog 首层分组，输入为可选 thread_id，输出为 groups DTO。"""
    manager = get_slash_catalog_manager(request)
    context = await build_slash_catalog_context(request, thread_id=thread_id)
    return SlashCatalogGroupsResponseDTO(groups=await manager.list_groups(context))


@router.get(
    "/slash-catalog/groups/{group_id}",
    response_model=SlashCatalogGroupItemsResponseDTO,
)
async def list_slash_catalog_group(
    group_id: str,
    request: Request,
    thread_id: str | None = Query(default=None),
) -> SlashCatalogGroupItemsResponseDTO:
    """返回 slash catalog 二层条目，输入为 group_id/thread_id，输出为 group/items DTO。"""
    manager = get_slash_catalog_manager(request)
    context = await build_slash_catalog_context(request, thread_id=thread_id)
    try:
        group, items = await manager.list_group_items(group_id, context)
    except SlashCatalogGroupNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "slash_catalog.group_not_found",
                "message": str(exc),
                "available_group_ids": list(exc.available_group_ids),
            },
        ) from exc
    return SlashCatalogGroupItemsResponseDTO(group=group, items=items)


def get_slash_catalog_manager(request: Request) -> SlashCatalogManager:
    """读取或创建 catalog manager，输入为 request，输出为 app state 门户。"""
    manager = getattr(request.app.state, "slash_catalog_manager", None)
    if manager is None:
        manager = SlashCatalogManager(providers=())
        request.app.state.slash_catalog_manager = manager
    return cast(SlashCatalogManager, manager)


async def build_slash_catalog_context(
    request: Request,
    *,
    thread_id: str | None,
) -> SlashCatalogContext:
    """由 Web app state 构建 context，输入为 request/thread_id，输出为 catalog context。"""
    state = request.app.state
    home = _path_attr(state, "kongming_home", get_kongming_home())
    workspace = _path_attr(state, "workspace_root", home)
    config: Any | None = getattr(state, "config", None)
    backend_kind = await _resolve_backend_kind(state, thread_id)
    return SlashCatalogContext(
        home=home,
        workspace=workspace,
        config=config,
        thread_id=thread_id,
        backend_kind=backend_kind,
    )


async def _resolve_backend_kind(
    state: Any,
    thread_id: str | None,
) -> SlashCatalogBackendKind | None:
    """读取 thread backend，输入为 app state/thread id，输出为受限 backend kind。"""
    if not thread_id:
        return None
    thread_manager = getattr(state, "thread_manager", None)
    if thread_manager is None:
        return None
    get_cell = getattr(thread_manager, "get_cell", None)
    if callable(get_cell):
        cell = get_cell(thread_id)
        metadata = getattr(cell, "metadata", None)
        backend_kind = getattr(metadata, "backend_kind", None)
        if backend_kind in {"generic_chat", "claude_code", "codex"}:
            return cast(SlashCatalogBackendKind, backend_kind)
    list_threads = getattr(thread_manager, "list_threads", None)
    if not callable(list_threads):
        return None
    metas = await asyncio.to_thread(list_threads)
    for metadata in metas:
        if getattr(metadata, "id", None) != thread_id:
            continue
        backend_kind = getattr(metadata, "backend_kind", None)
        if backend_kind in {"generic_chat", "claude_code", "codex"}:
            return cast(SlashCatalogBackendKind, backend_kind)
    return None


def _path_attr(state: Any, name: str, fallback: Path) -> Path:
    """读取路径型 app state 属性，输入为 state/name/fallback，输出为 Path。"""
    value = getattr(state, name, fallback)
    return value if isinstance(value, Path) else Path(str(value))


__all__ = [
    "build_slash_catalog_context",
    "get_slash_catalog_manager",
    "router",
]
