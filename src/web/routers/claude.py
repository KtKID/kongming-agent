"""Claude Code 历史/恢复路由（v0.2 dev-checklist #7）。

端点：

- ``GET /api/claude/projects`` — 读 registry 登记的 cwd 列表，逐个扫
  ``~/.claude/projects/<encoded(cwd)>/``，返回 project + session 摘要树
  （按 ``last_modified`` 倒序）。
- ``POST /api/claude/projects`` — 登记一条 cwd 到 registry；要求 cwd 是已存在目录，
  幂等（同 cwd 已存在则更新 alias）。
- ``DELETE /api/claude/projects?cwd=<urlencoded>`` — 从 registry 删除 cwd。

后续任务（#8）将在此 router 追加 ``POST /api/claude/sessions/{claude_thread_id}/import``
等 endpoint，因此此模块只放 claude_code 相关的扫描 / 恢复路由，**不**与
``threads.py`` 混用。

鉴权：

- 路径前缀 ``/api/`` 自动落入 :class:`web.auth.AuthMiddleware` 保护范围 —— 未登录
  请求会被中间件拦截并返回 401，本模块不需要再写 ``Depends``。
- 这是必须的：未鉴权暴露会让任何访问者列出本机所有 SDK session（隐私问题）。

实现：

- :func:`web.claude_code.projects_scanner.list_projects` 是同步扫盘 IO，用
  :func:`asyncio.to_thread` 隔离事件循环（与 ``threads.list_threads`` 同款）。
- ``ProjectSummary`` / ``SessionSummary`` 是 ``frozen=True`` dataclass —— FastAPI
  默认不会原样序列化 frozen dataclass，因此显式 dict 字面量映射成响应 JSON。
- registry 读 / 写均通过 :mod:`web.claude_code.projects_registry`，由调用方注入
  ``request.app.state.kongming_home``，确保 worktree 隔离。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from web.claude_code.projects_registry import (
    add_project,
    load_registry,
    remove_project,
)
from web.claude_code.projects_scanner import ProjectSummary, list_projects
from web.protocol.rest_models import AddProjectRequest, ProjectRegistryEntryDTO
from web.thread_metadata import ThreadMetadata

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/claude", tags=["claude"])


def _build_thread_metadata_index(
    threads: list[ThreadMetadata],
) -> dict[str, ThreadMetadata]:
    """按 ``claude_thread_id`` 反查 thread metadata 的索引构造器。

    历史上 ``bind_claude_thread`` 的 1:1 守门可能漏掉过若干路径（导致同一
    ``claude_thread_id`` 挂多个 thread metadata），所以这里必须容忍冲突。

    冲突时**保留 ``threads`` 列表中首次出现的那条**——调用方传入的列表通常已按
    ``(is_pinned, updated_at) DESC`` 排序（``ThreadManager.list_threads`` 默认），
    所以首条就是"最优"（置顶 + 最近活跃）。用 dict 推导会被后者（次优）覆盖，
    导致 scanner 拿到的 title / archived 退化到次优 thread 的值。
    """
    idx: dict[str, ThreadMetadata] = {}
    for m in threads:
        if m.claude_thread_id:
            idx.setdefault(m.claude_thread_id, m)
    return idx


def _serialize_projects(
    projects: list[ProjectSummary],
    pinned_ids: set[str],
) -> list[dict[str, Any]]:
    return [
        {
            "name": p.name,
            "cwd": p.cwd,
            "display_name": p.display_name,
            "sessions": sorted(
                [
                    {
                        "claude_thread_id": s.claude_thread_id,
                        "title": s.title,
                        "last_modified": s.last_modified,
                        "message_count": s.message_count,
                        "is_pinned": s.claude_thread_id in pinned_ids,
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
async def get_claude_projects(request: Request) -> dict[str, Any]:
    """列出 registry 登记的所有 project 与 session 摘要。

    流程：

    1. 读 ``<kongming_home>/web/claude_projects.json`` 拿 registry 登记的 cwd 列表。
    2. 调 :func:`list_projects` 按 cwd 列表逐个扫 ``~/.claude/projects/<encoded>/``。
    3. 用 thread metadata 上的 ``is_pinned`` 标注 session。

    Returns:
        dict: ``{"projects": [...]}``，每个 project 含 ``name`` / ``cwd``
        / ``display_name`` / ``sessions``；session 含 ``claude_thread_id``
        / ``title`` / ``last_modified`` / ``message_count`` / ``is_pinned``。
        registry 为空时 ``projects`` 为空列表。
    """
    home: Path = request.app.state.kongming_home
    claude_home: Path | None = getattr(request.app.state, "claude_home", None)
    entries = await asyncio.to_thread(load_registry, home)
    cwds = [e.cwd for e in entries]
    tm = request.app.state.thread_manager
    # 用 tm.list_threads() 而不是 list_thread_metadata：前者会用内存中已 boot
    # cell 的 metadata 覆盖扫盘版本，避免刚 rename 的 thread 还没写盘就读到 stale。
    threads = await asyncio.to_thread(tm.list_threads)
    thread_metadata_index = _build_thread_metadata_index(threads)
    projects = await asyncio.to_thread(
        list_projects,
        cwds,
        claude_home=claude_home,
        thread_metadata_index=thread_metadata_index,
    )
    pinned_ids: set[str] = {
        meta.claude_thread_id for meta in threads if meta.is_pinned and meta.claude_thread_id
    }
    return {"projects": _serialize_projects(projects, pinned_ids)}


@router.post("/projects", status_code=201)
async def add_claude_project(
    body: AddProjectRequest,
    request: Request,
) -> ProjectRegistryEntryDTO:
    """登记一条 cwd 到 claude project registry。

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
async def remove_claude_project(cwd: str, request: Request) -> Response:
    """从 claude project registry 删除一条 cwd。

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
async def refresh_claude_projects(request: Request) -> StreamingResponse:
    """流式刷新 Claude projects，并持续返回扫描进度。

    NDJSON 事件（protocol-frame-type-unify-v0.2 后判别字段从 ``kind`` 切到
    ``frame_type``，与全局 wire 协议对齐）：

    - ``{"frame_type":"progress","current":1,"total":12,"current_project":"..."}``
    - ``{"frame_type":"done","projects":[...]}``
    - ``{"frame_type":"error","detail":"..."}``
    """
    home: Path = request.app.state.kongming_home
    claude_home: Path | None = getattr(request.app.state, "claude_home", None)
    entries = await asyncio.to_thread(load_registry, home)
    registry_cwds = [e.cwd for e in entries]
    tm = request.app.state.thread_manager
    threads = await asyncio.to_thread(tm.list_threads)
    thread_metadata_index = _build_thread_metadata_index(threads)
    pinned_ids: set[str] = {
        meta.claude_thread_id for meta in threads if meta.is_pinned and meta.claude_thread_id
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
                projects = list_projects(
                    registry_cwds,
                    claude_home=claude_home,
                    progress_callback=emit_progress,
                    thread_metadata_index=thread_metadata_index,
                )
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {
                        "frame_type": "done",
                        "projects": _serialize_projects(projects, pinned_ids),
                    },
                )
            except Exception as exc:  # pragma: no cover - 防御性兜底
                logger.exception("claude projects refresh failed")
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


__all__ = ["router"]
