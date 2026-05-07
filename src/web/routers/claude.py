"""Claude Code 历史/恢复路由（v0.2 dev-checklist #7）。

端点：

- ``GET /api/claude/projects`` — 扫描 ``~/.claude/projects/`` 返回所有 project +
  每个 project 的 session 摘要树（按 ``last_modified`` 倒序）。

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
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from web.claude_code.projects_scanner import ProjectSummary, list_projects
from web.claude_code.session_writer import append_archived, append_custom_title

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/claude", tags=["claude"])


def _serialize_projects(projects: list[ProjectSummary]) -> list[dict[str, Any]]:
    return [
        {
            "name": p.name,
            "cwd": p.cwd,
            "display_name": p.display_name,
            "sessions": [
                {
                    "claude_thread_id": s.claude_thread_id,
                    "title": s.title,
                    "last_modified": s.last_modified,
                    "message_count": s.message_count,
                }
                for s in p.sessions
            ],
        }
        for p in projects
    ]


@router.get("/projects")
async def get_claude_projects() -> dict[str, Any]:
    """列出 ``~/.claude/projects/`` 下的所有 project 与 session 摘要。

    Returns:
        dict: ``{"projects": [...]}``，每个 project 含 ``name`` / ``cwd``
        / ``display_name`` / ``sessions``；session 含 ``claude_thread_id``
        / ``title`` / ``last_modified`` / ``message_count``。
        ``~/.claude/projects/`` 不存在时 ``projects`` 为空列表。
    """
    projects = await asyncio.to_thread(list_projects)
    return {"projects": _serialize_projects(projects)}


@router.get("/projects/refresh")
async def refresh_claude_projects() -> StreamingResponse:
    """流式刷新 Claude projects，并持续返回扫描进度。

    NDJSON 事件：
    - ``{"kind":"progress","current":1,"total":12,"current_project":"..."}``
    - ``{"kind":"done","projects":[...]}``
    - ``{"kind":"error","detail":"..."}``
    """

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
                    "kind": "progress",
                    "current": current,
                    "total": total,
                    "current_project": current_project,
                },
            )

        def worker() -> None:
            try:
                projects = list_projects(progress_callback=emit_progress)
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {
                        "kind": "done",
                        "projects": _serialize_projects(projects),
                    },
                )
            except Exception as exc:  # pragma: no cover - 防御性兜底
                logger.exception("claude projects refresh failed")
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {
                        "kind": "error",
                        "detail": str(exc),
                    },
                )

        task = asyncio.create_task(asyncio.to_thread(worker))
        try:
            while True:
                item = await queue.get()
                yield json.dumps(item, ensure_ascii=False) + "\n"
                if item["kind"] in {"done", "error"}:
                    break
        finally:
            await task

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# ---------------------------------------------------------------------------
# Rename session
# ---------------------------------------------------------------------------


class RenameClaudeSessionRequest(BaseModel):
    """重命名 Claude session 的请求体。"""

    claude_thread_id: Annotated[str, Field(min_length=1, max_length=100)]
    cwd: Annotated[str, Field(pattern=r"^/")]
    title: Annotated[str, Field(min_length=1, max_length=200)]


def _resolve_jsonl_path(
    claude_thread_id: str,
    cwd: str,
    claude_home: Path | None = None,
) -> Path | None:
    """从 *cwd* 定位 jsonl 路径。

    SDK 编码规则不止替换 ``/``（``_`` ``.`` 也会被替换成 ``-``），
    无法纯文本逆转。改用遍历 projects 目录，找到含目标 jsonl 的那个。
    """
    home = claude_home or Path.home() / ".claude"
    projects_dir = home / "projects"
    if not projects_dir.is_dir():
        return None
    target = f"{claude_thread_id}.jsonl"
    for entry in projects_dir.iterdir():
        if not entry.is_dir():
            continue
        candidate = entry / target
        if candidate.is_file():
            return candidate
    return None


@router.patch("/sessions/rename")
async def rename_claude_session(
    body: RenameClaudeSessionRequest,
) -> dict[str, bool]:
    """往 Claude jsonl 文件追加 ``custom-title`` 条目以重命名 session。"""
    jsonl_path = _resolve_jsonl_path(body.claude_thread_id, body.cwd)
    if jsonl_path is None:
        raise HTTPException(
            status_code=404,
            detail=f"jsonl not found for session {body.claude_thread_id}",
        )
    await asyncio.to_thread(
        append_custom_title,
        jsonl_path,
        body.claude_thread_id,
        body.title,
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Archive session
# ---------------------------------------------------------------------------


class ArchiveClaudeSessionRequest(BaseModel):
    """归档 Claude session 的请求体。"""

    claude_thread_id: Annotated[str, Field(min_length=1, max_length=100)]
    cwd: Annotated[str, Field(pattern=r"^/")]


@router.patch("/sessions/archive")
async def archive_claude_session(
    body: ArchiveClaudeSessionRequest,
) -> dict[str, bool]:
    """往 Claude jsonl 文件追加 ``archived`` 条目以归档 session。"""
    jsonl_path = _resolve_jsonl_path(body.claude_thread_id, body.cwd)
    if jsonl_path is None:
        raise HTTPException(
            status_code=404,
            detail=f"jsonl not found for session {body.claude_thread_id}",
        )
    await asyncio.to_thread(
        append_archived,
        jsonl_path,
        body.claude_thread_id,
    )
    return {"ok": True}


__all__ = ["router"]
