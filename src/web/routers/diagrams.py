"""架构图浏览 REST 路由。

扫描项目中的 HTML 架构图，提供列表查询和内容读取两个端点。

数据源：
- ``docs/**/*.html`` — 文件名匹配 ``architecture*.html``
- ``dev-pipeline/tasks/*/diagram.html``
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Literal, cast

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel

from web.errors import KongmingWebError

router = APIRouter(prefix="/api/diagrams", tags=["diagrams"])

# ---------- DTO ----------


class DiagramEntry(BaseModel):
    id: str
    source: Literal["docs", "tasks"]
    title: str
    path: str


class DiagramListResponse(BaseModel):
    diagrams: list[DiagramEntry]


# ---------- errors ----------


class DiagramNotFoundError(KongmingWebError):
    status_code = 404


# ---------- helpers ----------

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TITLE_SUFFIX_RE = re.compile(r"\s*[·\-—]\s*模块依赖图\s*$")


def _workspace_root(request: Request) -> Path:
    return cast(Path, request.app.state.workspace_root)


def _extract_title(html_path: Path, fallback: str) -> str:
    """从 HTML ``<title>`` 标签提取标题，去掉常见后缀。"""
    try:
        # 只读前 2 KB 足够拿到 <title>
        text = html_path.read_text(encoding="utf-8", errors="ignore")[:2048]
    except OSError:
        return fallback
    m = _TITLE_RE.search(text)
    if not m:
        return fallback
    title = m.group(1).strip()
    title = _TITLE_SUFFIX_RE.sub("", title)
    return title or fallback


def _scan_diagrams(workspace: Path) -> list[DiagramEntry]:
    """同步扫描两个目录，返回 DiagramEntry 列表。"""
    entries: list[DiagramEntry] = []

    # 1. docs/**/*.html — 只收 architecture*.html
    docs_dir = workspace / "docs"
    if docs_dir.is_dir():
        for html_path in docs_dir.rglob("architecture*.html"):
            rel = html_path.relative_to(workspace)
            rel_str = str(rel)
            # id: 去掉 .html 后缀
            entry_id = str(rel.with_suffix(""))
            title = _extract_title(html_path, entry_id)
            entries.append(
                DiagramEntry(
                    id=entry_id,
                    source="docs",
                    title=title,
                    path=rel_str,
                )
            )

    # 2. dev-pipeline/tasks/*/diagram.html
    tasks_dir = workspace / "dev-pipeline" / "tasks"
    if tasks_dir.is_dir():
        for html_path in tasks_dir.glob("*/diagram.html"):
            rel = html_path.relative_to(workspace)
            rel_str = str(rel)
            entry_id = str(rel.with_suffix(""))
            title = _extract_title(html_path, entry_id)
            entries.append(
                DiagramEntry(
                    id=entry_id,
                    source="tasks",
                    title=title,
                    path=rel_str,
                )
            )

    # 按 path 排序保证稳定顺序
    entries.sort(key=lambda e: e.path)
    return entries


# ---------- endpoints ----------


@router.get("")
async def list_diagrams(request: Request) -> DiagramListResponse:
    """扫描项目中的 HTML 架构图，返回列表。"""
    workspace = _workspace_root(request)
    entries = await asyncio.to_thread(_scan_diagrams, workspace)
    return DiagramListResponse(diagrams=entries)


@router.get("/content")
async def get_diagram_content(
    request: Request,
    path: str = Query(..., description="相对于项目根的 HTML 文件路径"),
) -> Response:
    """根据 path 参数返回 HTML 原文。

    path 必须命中扫描白名单，否则返回 404。
    """
    # 防止路径遍历
    if ".." in path:
        raise DiagramNotFoundError("diagram not found")

    workspace = _workspace_root(request)

    # 用白名单校验：先扫描一遍拿到合法路径集合
    entries = await asyncio.to_thread(_scan_diagrams, workspace)
    allowed_paths = {e.path for e in entries}

    if path not in allowed_paths:
        raise DiagramNotFoundError("diagram not found")

    target = workspace / path
    # 再次确认 resolved 路径在 workspace 内（防御性校验）
    try:
        target.resolve().relative_to(workspace.resolve())
    except ValueError as exc:
        raise DiagramNotFoundError("diagram not found") from exc

    try:
        html_text = await asyncio.to_thread(target.read_text, "utf-8")
    except OSError as exc:
        raise DiagramNotFoundError("diagram not found") from exc

    return Response(content=html_text, media_type="text/html")


__all__ = ["DiagramEntry", "DiagramListResponse", "router"]
