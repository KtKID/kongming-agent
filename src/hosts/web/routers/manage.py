"""管理页路由（v0.1.5）。

端点：

- ``GET  /api/manage/cells``                — 列出所有活的 cell
- ``POST /api/manage/cells/{thread_id}/stop`` — 手动停止单个 cell（204）

注：v0.1.5 管理页用 REST polling，**没有** ``WS /ws/manage`` 订阅；后续 v0.1.6+ 再加。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Request

from hosts.web.dashboard import RuntimeStatusService
from hosts.web.errors import InvalidRequestError, InvalidThreadIdError, ThreadNotFoundError
from hosts.web.plugin_management import PluginManagementManager, PluginToolState
from hosts.web.protocol import (
    CellSummaryDTO,
    PluginToolDTO,
    PluginToolsResponseDTO,
    RuntimeStatusSnapshotDTO,
    UpdatePluginToolRequest,
)

if TYPE_CHECKING:
    from hosts.web.threads.types import ThreadManagerProtocol

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/manage", tags=["manage"])

THREAD_ID_RE: re.Pattern[str] = re.compile(r"^thread-[a-f0-9]{12}$")


def _validate_thread_id(thread_id: str) -> None:
    if not THREAD_ID_RE.match(thread_id):
        raise InvalidThreadIdError(
            f"invalid thread_id: {thread_id!r}; must match ^thread-[a-f0-9]{{12}}$"
        )


def _plugin_manager(request: Request) -> PluginManagementManager:
    """读取插件管理门户。"""
    manager = getattr(request.app.state, "plugin_management_manager", None)
    if not isinstance(manager, PluginManagementManager):
        raise InvalidRequestError("plugin management manager is not configured", status_code=500)
    return manager


async def _sync_plugin_tools_if_available(request: Request) -> None:
    """触发 runtime factory 同步 MCP 工具到插件 store。"""
    runtime_factory = getattr(request.app.state, "runtime_factory", None)
    sync = getattr(runtime_factory, "sync_plugin_tools_for_management", None)
    if not callable(sync):
        return
    try:
        await cast(Callable[[], Awaitable[None]], sync)()
    except Exception:
        logger.exception("plugin tools sync failed; returning persisted plugin states")


def _plugin_dto(state: PluginToolState) -> PluginToolDTO:
    """把内部状态转换成 REST DTO。"""
    return PluginToolDTO(
        id=state.id,
        name=state.name,
        display_name=state.display_name,
        source=state.source,
        enabled=state.enabled,
        server_id=state.server_id,
        mcp_tool_name=state.mcp_tool_name,
        description=state.description,
        canonical_name=state.canonical_name,
        is_alias=state.is_alias,
    )


@router.get("/cells")
async def list_cells(request: Request) -> list[CellSummaryDTO]:
    """列出所有活的 cell。"""
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    return tm.list_cells()


@router.get("/plugins", response_model=PluginToolsResponseDTO)
async def list_plugins(request: Request) -> PluginToolsResponseDTO:
    """列出当前已注册的 MCP 插件工具。"""
    await _sync_plugin_tools_if_available(request)
    manager = _plugin_manager(request)
    return PluginToolsResponseDTO(
        plugins=[_plugin_dto(state) for state in manager.list_registered_plugins()]
    )


@router.patch("/plugins/{tool_id}", response_model=PluginToolDTO)
async def update_plugin(
    tool_id: str, payload: UpdatePluginToolRequest, request: Request
) -> PluginToolDTO:
    """更新单个插件工具 enabled 状态。

    开关只写持久化 bool。已创建的 SessionEngine 保持自身工具快照，新 session
    在创建时读取最新 bool。
    """
    manager = _plugin_manager(request)
    try:
        state = manager.set_enabled(tool_id, payload.enabled)
    except KeyError as exc:
        raise InvalidRequestError(f"plugin tool not found: {tool_id}", status_code=404) from exc
    return _plugin_dto(state)


@router.get("/runtime-status", response_model=RuntimeStatusSnapshotDTO)
async def get_runtime_status(request: Request) -> RuntimeStatusSnapshotDTO:
    svc = RuntimeStatusService(
        request.app.state.config,
        request.app.state.thread_manager,
        claude_session_manager=request.app.state.claude_session_manager,
        codex_session_manager=request.app.state.codex_session_manager,
        approval_inbox_broadcaster=request.app.state.approval_inbox_broadcaster,
        kongming_home=request.app.state.kongming_home,
    )
    return svc.build_snapshot_dto()


@router.post("/cells/{thread_id}/stop", status_code=204)
async def stop_cell(thread_id: str, request: Request) -> None:
    """手动停止单个 cell。

    语义：

    - 不存在的 cell → 404 :class:`ThreadNotFoundError`
    - 已停的（``ThreadManager.evict_cell`` 幂等）→ 仍 204
    """
    _validate_thread_id(thread_id)
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    if tm.get_cell(thread_id) is None:
        raise ThreadNotFoundError(f"cell not found: {thread_id}")
    await tm.evict_cell(thread_id, reason="manual_stop", notify_ws=True)


__all__ = ["router"]
