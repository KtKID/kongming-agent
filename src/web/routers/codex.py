"""Codex REST 端点（v0.1）。

前缀 ``/api/codex``，提供项目列表 / 单会话历史回放 / 单会话元数据，
并在 v0.1 web-projects-registry 引入 cwd registry 的 add/remove/refresh
端点（与 ``src/web/routers/claude.py`` 完全对称，仅模块路径与字段差异）。

与 ``src/web/integrations/codex/route.py``（WebSocket 端点）平级；
本文件只做 REST，不做 WebSocket。

鉴权：

- 路径前缀 ``/api/`` 自动落入 :class:`web.auth.AuthMiddleware` 保护范围 —— 未登录
  请求会被中间件拦截并返回 401，本模块不需要再写 ``Depends``。

实现：

- :func:`web.integrations.codex.projects_scanner.list_codex_projects` 是同步扫盘 IO，用
  :func:`asyncio.to_thread` 隔离事件循环。
- ``CodexProjectSummary`` / ``CodexSessionSummary`` 是 ``frozen=True`` dataclass ——
  显式 dict 字面量映射成响应 JSON。
- registry 读 / 写均通过 :mod:`web.integrations.codex.projects_registry`，由调用方注入
  ``request.app.state.kongming_home``，确保 worktree 隔离。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from web.integrations.codex.jsonl_history import parse_codex_rollout, read_session_meta
from web.integrations.codex.projects_registry import (
    add_project,
    load_registry,
    remove_project,
)
from web.integrations.codex.projects_scanner import CodexProjectSummary, list_codex_projects
from web.protocol.rest_models import AddProjectRequest, ProjectRegistryEntryDTO

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/codex", tags=["codex"])


def _serialize_projects(
    projects: list[CodexProjectSummary],
    pinned_ids: set[str],
) -> list[dict[str, Any]]:
    return [
        {
            "cwd": p.cwd,
            "display_name": p.display_name,
            "last_modified": p.last_modified,
            "sessions": sorted(
                [
                    {
                        "session_id": s.session_id,
                        "title": s.title,
                        "cwd": s.cwd,
                        "last_modified": s.last_modified,
                        "message_count": s.message_count,
                        "cli_version": s.cli_version,
                        "rollout_path": s.rollout_path,
                        "provider": s.provider,
                        "is_pinned": s.session_id in pinned_ids,
                    }
                    for s in p.sessions
                ],
                key=lambda s: (s["is_pinned"], s["last_modified"]),
                reverse=True,
            ),
        }
        for p in projects
    ]


@router.get("/projects")
async def get_projects(request: Request) -> JSONResponse:
    """列出 registry 登记的所有 project 与 session 摘要。

    流程：

    1. 读 ``<kongming_home>/web/codex_projects.json`` 拿 registry 登记的 cwd 列表。
    2. 调 :func:`list_codex_projects` 按 cwd 列表过滤 ``~/.codex/sessions/`` 下的 rollout。
    3. 用 thread metadata 上的 ``is_pinned`` 标注 session。

    Returns:
        JSON 列表，每个元素含 ``cwd`` / ``display_name`` / ``last_modified``
        / ``sessions``；session 含 ``session_id`` / ``title`` / ``cwd``
        / ``last_modified`` / ``message_count`` / ``cli_version``
        / ``rollout_path`` / ``provider`` / ``is_pinned``。
        registry 为空时返回空列表。
    """
    home: Path = request.app.state.kongming_home
    codex_home: Path | None = getattr(request.app.state, "codex_home", None)
    entries = await asyncio.to_thread(load_registry, home)
    cwds = [e.cwd for e in entries]
    projects = await asyncio.to_thread(
        list_codex_projects,
        cwds,
        codex_home=codex_home,
    )

    tm = request.app.state.thread_manager
    all_threads = await asyncio.to_thread(tm.list_threads)
    pinned_ids: set[str] = {
        meta.codex_thread_id for meta in all_threads if meta.is_pinned and meta.codex_thread_id
    }
    return JSONResponse(_serialize_projects(projects, pinned_ids))


@router.post("/projects", status_code=201)
async def add_codex_project(
    body: AddProjectRequest,
    request: Request,
) -> ProjectRegistryEntryDTO:
    """登记一条 cwd 到 codex project registry。

    校验：

    - ``body.cwd`` 必须以 ``/`` 开头（DTO 层 pattern 已强制）。
    - ``body.cwd`` 必须是已存在的目录，否则返 400；只校验 ``Path.is_dir()``，
      不校验可读 / 可写——这些是运行时关心的事，登记时保持宽松。

    幂等：同 cwd 已存在 → 更新 alias，``added_at`` 保留。

    Returns:
        201 + 写入后的 :class:`ProjectRegistryEntryDTO`。
    """
    cwd = body.cwd.strip()
    if not Path(cwd).is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"cwd is not a directory: {cwd}",
        )
    home: Path = request.app.state.kongming_home
    entry = await asyncio.to_thread(add_project, home, cwd, body.alias)
    return ProjectRegistryEntryDTO(
        cwd=entry.cwd,
        alias=entry.alias,
        added_at=entry.added_at,
    )


@router.delete("/projects", status_code=204)
async def remove_codex_project(cwd: str, request: Request) -> Response:
    """从 codex project registry 删除一条 cwd。

    Args:
        cwd: 待删 cwd（query 参数，调用方需 URL-encode）。

    Returns:
        204 删除成功；404 cwd 不在 registry。
    """
    home: Path = request.app.state.kongming_home
    removed = await asyncio.to_thread(remove_project, home, cwd)
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"cwd not in registry: {cwd}",
        )
    return Response(status_code=204)


@router.get("/projects/refresh")
async def refresh_codex_projects(request: Request) -> StreamingResponse:
    """流式刷新 Codex projects，并持续返回扫描进度。

    NDJSON 事件（protocol-frame-type-unify-v0.2 后判别字段从 ``kind`` 切到
    ``frame_type``，与全局 wire 协议对齐）：

    - ``{"frame_type":"progress","current":1,"total":12,"current_project":"..."}``
    - ``{"frame_type":"done","projects":[...]}``
    - ``{"frame_type":"error","detail":"..."}``
    """
    home: Path = request.app.state.kongming_home
    codex_home: Path | None = getattr(request.app.state, "codex_home", None)
    entries = await asyncio.to_thread(load_registry, home)
    registry_cwds = [e.cwd for e in entries]
    tm = request.app.state.thread_manager
    all_threads = await asyncio.to_thread(tm.list_threads)
    pinned_ids: set[str] = {
        meta.codex_thread_id for meta in all_threads if meta.is_pinned and meta.codex_thread_id
    }

    async def stream() -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def emit_progress(
            current: int,
            total: int,
            current_project: str,
        ) -> None:
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {
                    "frame_type": "progress",
                    "current": current,
                    "total": total,
                    "current_project": current_project,
                },
            )

        def worker() -> None:
            try:
                projects = list_codex_projects(
                    registry_cwds,
                    codex_home=codex_home,
                    progress_callback=emit_progress,
                )
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {
                        "frame_type": "done",
                        "projects": _serialize_projects(projects, pinned_ids),
                    },
                )
            except Exception as exc:  # pragma: no cover - 防御性兜底
                _logger.exception("codex projects refresh failed")
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {
                        "frame_type": "error",
                        "detail": str(exc),
                    },
                )

        task = asyncio.create_task(asyncio.to_thread(worker))
        try:
            while True:
                item = await queue.get()
                yield json.dumps(item, ensure_ascii=False) + "\n"
                if item["frame_type"] in {"done", "error"}:
                    break
        finally:
            await task

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@router.get("/sessions/{session_id}/history")
async def get_session_history(session_id: str, request: Request) -> JSONResponse:
    """单会话历史回放 → NormalizedMessage[]。"""
    codex_home: Path | None = getattr(request.app.state, "codex_home", None)
    rollout_path = await asyncio.to_thread(
        _find_rollout_by_session_id,
        session_id,
        codex_home,
    )
    if rollout_path is None:
        return JSONResponse({"error": f"session {session_id} not found"}, status_code=404)
    messages = await asyncio.to_thread(parse_codex_rollout, rollout_path, session_id)
    return JSONResponse({"messages": messages})


@router.get("/sessions/{session_id}/metadata")
async def get_session_metadata(session_id: str, request: Request) -> JSONResponse:
    """单会话元数据（cwd / model / cli_version）。"""
    codex_home: Path | None = getattr(request.app.state, "codex_home", None)
    rollout_path = await asyncio.to_thread(
        _find_rollout_by_session_id,
        session_id,
        codex_home,
    )
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
    """根据 session_id 在 ``~/.codex/sessions/`` 树里找到对应的 rollout 文件。

    扫盘查找——从 projects_scanner 的扫盘结果反查。这里 ``registry_cwds``
    传空列表会导致 scanner 返回 ``[]``，因此显式从 ``codex_home/sessions/``
    全量扫；保持与原行为一致。
    """
    # 直接复用 scanner 的内部能力：读 registry，按登记 cwd 查 rollout。
    # 但 metadata/history 路由调用方未必登记了 cwd（举例：从 thread metadata
    # 直接打开历史 session），所以这里走 fallback——遍历 sessions/ 全量。
    home = codex_home if codex_home is not None else Path.home() / ".codex"
    sessions_dir = home / "sessions"
    if not sessions_dir.is_dir():
        return None
    target_suffix = f"{session_id}.jsonl"
    for root, _dirs, files in os.walk(sessions_dir):
        for fname in files:
            if fname.startswith("rollout-") and fname.endswith(target_suffix):
                return Path(root) / fname
    return None


__all__ = ["router"]
