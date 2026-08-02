"""Thread CRUD 路由。

端点：

- ``GET    /api/threads``                              — 列出所有 thread metadata
- ``POST   /api/threads``                              — 创建 thread；返回 metadata（201）
- ``POST   /api/threads/{thread_id}/fork``             — 从 assistant 回复边界分叉 generic chat thread（201）
- ``POST   /api/threads/import-claude-session``         — 导入已有 Claude SDK session（v0.2.0）
- ``PATCH  /api/threads/{thread_id}``                   — 重命名（不改 preset）
- ``DELETE /api/threads/{thread_id}``                   — 删除（204）
- ``GET    /api/threads/{thread_id}/claude_history``    — 拉 claude_code 后端 SDK 持久化历史（v0.2.0）

注意：**没有** ``GET /api/threads/{id}/history`` —— generic_chat 历史走 WS
``thread.history`` 帧；只有 ``claude_code`` 后端绑定 SDK session 之后才有
``claude_history`` REST 端点（Claude Agent SDK 落盘 JSONL，前端 resume 时拉）。

``import-claude-session`` 故意走 ``/api/threads/`` 前缀而不是 ``/api/claude/``：
URL 里出现的资源是新 thread（``imported=true`` 时），与 thread CRUD 是同一族
副作用；``routers/claude.py`` 留给纯 SDK session 浏览（projects 列表）。

安全：

- ``thread_id`` 必须匹配 ``^thread-[a-f0-9]{12}$``；不匹配抛 422
  :class:`InvalidThreadIdError`，防 path traversal。
- ``ThreadManager.delete_thread`` 幂等（thread 不存在不抛），但这里**不**对外
  暴露幂等性 —— 不存在抛 :class:`ThreadNotFoundError(404)`，符合 REST 语义。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException, Query, Request

from evolution.models import (
    DecisionRecord,
    DecisionSummary,
)
from hosts.web.app_support.path_utils import is_absolute_workspace_path
from hosts.web.errors import InvalidThreadIdError, ModelCatalogWebError, ThreadNotFoundError
from hosts.web.integrations.claude_code.jsonl_history import jsonl_path_for, parse_jsonl_history
from hosts.web.protocol import (
    CreateGenericThreadFromFirstMessageRequest,
    CreateGenericThreadFromFirstMessageResponse,
    CreateThreadRequest,
    EvolutionDecisionItemDTO,
    EvolutionDecisionRequest,
    EvolutionDecisionResponse,
    EvolutionDecisionSummaryDTO,
    EvolutionNutrientDTO,
    EvolutionReviewDTO,
    ForkThreadRequest,
    ImportClaudeSessionRequest,
    ImportClaudeSessionResponse,
    ImportCodexSessionRequest,
    ImportCodexSessionResponse,
    RenameThreadRequest,
    ThreadMetadataDTO,
    ThreadPermissionsDTO,
    UpdateThreadPermissionsRequest,
    UpdateThreadPresetRequest,
    UpdateWorkspaceFileRequest,
    WorkspaceContextDTO,
    WorkspaceFileDTO,
    WorkspaceTreeDTO,
    WorkspaceTreeNodeDTO,
)
from hosts.web.threads.errors import (
    ThreadForkConflictError,
    ThreadPermissionsRevisionConflictError,
    ThreadPermissionsStorageError,
    ThreadPermissionsValidationError,
    ThreadPresetRefreshError,
)
from hosts.web.workspace.model import (
    WorkspaceError,
    get_thread_meta,
    list_workspace_entries,
    read_workspace_text_file,
    require_workspace_root,
    resolve_workspace_cwd,
    write_workspace_text_file,
)
from infrastructure.config.model_provider_catalog import ModelProviderCatalogError

if TYPE_CHECKING:
    from hosts.web.threads.metadata import ThreadMetadata
    from hosts.web.threads.types import (
        ThreadManagerProtocol,
        ThreadPermissionsManagerProtocol,
    )

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/threads", tags=["threads"])

THREAD_ID_RE: re.Pattern[str] = re.compile(r"^thread-[a-f0-9]{12}$")
CLAUDE_HISTORY_MAX_MESSAGES = 300


def _validate_thread_id(thread_id: str) -> None:
    if not THREAD_ID_RE.match(thread_id):
        raise InvalidThreadIdError(
            f"invalid thread_id: {thread_id!r}; must match ^thread-[a-f0-9]{{12}}$"
        )


async def _to_dto(meta: ThreadMetadata, tm: ThreadManagerProtocol) -> ThreadMetadataDTO:
    """把 ThreadMetadata 转 REST DTO。

    **usage-token-v2-bigbang**：不再返回 ``usage_summary`` 字段。token 数据
    通过独立端点 ``GET /threads/<tid>/usage`` 拿 v2 manager.get_thread_usage
    的派生结果。``tm`` 参数保留作未来扩展，本函数当前未使用。
    """
    _ = tm  # 暂时未用；保留参数兼容现有调用方
    return ThreadMetadataDTO(
        id=meta.id,
        name=meta.name,
        preset_id=meta.preset_id,
        backend_kind=meta.backend_kind,
        thread_kind=meta.thread_kind,
        source_kind=meta.source_kind,
        source_id=meta.source_id,
        claude_thread_id=meta.claude_thread_id,
        codex_thread_id=meta.codex_thread_id,
        cwd=meta.cwd,
        created_at=meta.created_at,
        updated_at=meta.updated_at,
        message_count=meta.message_count,
        is_pinned=meta.is_pinned,
        is_archived=meta.is_archived,
        forked_from_id=meta.forked_from_id,
        forked_from_history_index=meta.forked_from_history_index,
        schema_version=meta.schema_version,
    )


def _to_workspace_context(
    meta: ThreadMetadata,
    *,
    server_workspace_root: Path,
) -> WorkspaceContextDTO:
    """构造 WorkspaceContextDTO；thread.cwd 空时 fallback 到 server 启动目录。

    用户心智："纯聊天 thread 默认 cwd = server 启动路径"，让 files/shell/Zap
    都能在没绑 cwd 的 thread 上工作（统一走 ``resolve_workspace_cwd`` helper）。

    Args:
        meta: thread 元数据。
        server_workspace_root: server 启动目录（``app.state.workspace_root``）；
            kw-only 强制显式传入，让代码读者一眼看出 fallback 来源。
    """
    workspace_root = resolve_workspace_cwd(meta, server_workspace_root)
    files_available = bool(workspace_root)
    shell_available = files_available
    unavailable_reason: str | None = None
    if not workspace_root:
        unavailable_reason = "thread has no workspace cwd"
    return WorkspaceContextDTO(
        thread_id=meta.id,
        backend_kind=meta.backend_kind,
        workspace_root=workspace_root,
        claude_thread_id=meta.claude_thread_id,
        shell_provider=(
            "claude_code"
            if shell_available and meta.backend_kind == "claude_code"
            else "system_shell"
            if shell_available
            else "none"
        ),
        files_available=files_available,
        shell_available=shell_available,
        unavailable_reason=unavailable_reason,
    )


def _review_id_for_run(run_id: str) -> str:
    return f"evo-review:{run_id}"


def _evolution_manager(request: Request) -> Any:
    manager = getattr(request.app.state, "evolution_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="evolution manager not available")
    return manager


def _workspace_root_for_meta(request: Request, meta: ThreadMetadata) -> Path:
    workspace_root = (
        meta.cwd.strip()
        if isinstance(meta.cwd, str) and meta.cwd.strip()
        else str(getattr(request.app.state, "workspace_root", ""))
    )
    return Path(workspace_root).expanduser().resolve()


def _to_evolution_review_dto(
    *,
    review_id: str,
    review: object,
    decision: DecisionRecord | None,
) -> EvolutionReviewDTO:
    from evolution.models import ReviewResult

    if not isinstance(review, ReviewResult):
        raise TypeError("review must be ReviewResult")
    summary = (
        decision.summary
        if decision is not None
        else DecisionSummary(
            total=len(review.nutrients),
            accepted_memory=0,
            accepted_skill=0,
            ignored=0,
            pending=len(review.nutrients),
        )
    )
    return EvolutionReviewDTO(
        review_id=review_id,
        run_id=review.run_id,
        session_id=review.session_id,
        reviewed_at_ms=review.reviewed_at_ms,
        review_summary=review.review_summary,
        nutrients=[
            EvolutionNutrientDTO.model_validate(nutrient.to_dict()) for nutrient in review.nutrients
        ],
        decision_summary=EvolutionDecisionSummaryDTO.model_validate(summary.to_dict()),
        decisions=[
            EvolutionDecisionItemDTO.model_validate(item.to_dict())
            for item in (decision.items if decision is not None else ())
        ],
    )


def _require_thread_meta(request: Request, thread_id: str) -> ThreadMetadata:
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    metas = tm.list_threads()
    meta = get_thread_meta(thread_id, metas)
    if meta is None:
        raise ThreadNotFoundError(f"thread not found: {thread_id}")
    return meta


@router.get("")
async def list_threads(request: Request) -> list[ThreadMetadataDTO]:
    """列出所有 thread metadata。

    实现：``ThreadManager.list_threads()`` 是同步 IO（扫盘），用
    :func:`asyncio.to_thread` 隔离事件循环。
    """
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    metas = await asyncio.to_thread(tm.list_threads)
    return [await _to_dto(m, tm) for m in metas]


@router.post("", status_code=201)
async def create_thread(
    body: CreateThreadRequest,
    request: Request,
) -> ThreadMetadataDTO:
    """创建 thread。

    校验：
    - ``backend_kind="generic_chat"`` 必须有非空 ``preset_id``，否则 400。
    - ``backend_kind="claude_code"`` / ``"codex"`` 时 ``preset_id`` 可空（前端通常省略 / 传 ""）。
    - ``cwd`` 传入时必须是绝对路径。
    """
    if body.backend_kind == "generic_chat" and (not body.preset_id or not body.preset_id.strip()):
        raise HTTPException(
            status_code=400,
            detail="preset_id required for generic_chat backend",
        )
    normalized_cwd = body.cwd.strip()
    if normalized_cwd and not is_absolute_workspace_path(normalized_cwd):
        raise HTTPException(
            status_code=400,
            detail="cwd must be an absolute path",
        )
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    try:
        meta = await tm.create_thread(
            body.name,
            body.preset_id,
            backend_kind=body.backend_kind,
            cwd=normalized_cwd,
        )
    except ModelProviderCatalogError as exc:
        raise ModelCatalogWebError(exc) from exc
    return await _to_dto(meta, tm)


@router.post("/generic/first-message")
async def create_generic_thread_from_first_message(
    body: CreateGenericThreadFromFirstMessageRequest,
    request: Request,
) -> CreateGenericThreadFromFirstMessageResponse:
    """通用频道空白页首发创建。

    路由层只做 HTTP 错误映射；创建、cwd 解析、metadata 与首条 user message
    持久化都由 ThreadManager 门户方法收口。
    """
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    try:
        meta = await tm.create_generic_thread_from_first_message(
            text=body.text,
            preset_id=body.preset_id,
            cwd=body.cwd,
            reasoning_effort=body.reasoning_effort,
        )
    except ModelProviderCatalogError as exc:
        raise ModelCatalogWebError(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return CreateGenericThreadFromFirstMessageResponse(thread=await _to_dto(meta, tm))


@router.post("/{thread_id}/fork", status_code=201)
async def fork_thread(
    thread_id: str,
    request: Request,
    body: ForkThreadRequest | None = None,
) -> ThreadMetadataDTO:
    """从 assistant 回复边界 fork generic chat thread，并返回新 thread。

    409 表示源 thread 仍在运行或有排队输入；调用方可在 thread 进入 idle 后重试。
    """
    _validate_thread_id(thread_id)
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    try:
        meta = await tm.fork_thread(
            thread_id,
            history_index=body.history_index if body is not None else None,
        )
    except KeyError as exc:
        raise ThreadNotFoundError(f"thread not found: {thread_id}") from exc
    except ThreadForkConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await _to_dto(meta, tm)


@router.post("/import-claude-session")
async def import_claude_session(
    body: ImportClaudeSessionRequest,
    request: Request,
) -> ImportClaudeSessionResponse:
    """导入已有 Claude Agent SDK session 为 kongming thread（v0.2.0 dev #8）。

    防重复语义：

    1. ``claude_thread_id`` 已绑过 → 返回该 thread + ``imported=False``。
    2. 未绑定 → 新建 thread + 绑定 → ``imported=True``。

    走 :meth:`ThreadManager.create_and_bind_claude_thread`：用 per-ctid
    :class:`asyncio.Lock` 串行化"反查→不存在则 create+bind"临界区，根治原
    "两步非原子写入"的 race window（曾让同 ctid 并发请求产出多条 thread
    metadata，即"幽灵 thread"——参见 ``docs/fixes/claude-session-rename-
    archive-metadata-source.md``）。

    校验：DTO 已强制 ``cwd`` 必须以 ``/`` 开头、``name`` / ``claude_thread_id``
    长度上限。鉴权由全局 :class:`web.auth.middleware.AuthMiddleware` 兜底。
    """
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    meta, imported = await tm.create_and_bind_claude_thread(
        claude_thread_id=body.claude_thread_id,
        cwd=body.cwd,
        name=body.name,
    )
    return ImportClaudeSessionResponse(
        thread=await _to_dto(meta, tm),
        imported=imported,
    )


@router.post("/import-codex-session")
async def import_codex_session(
    body: ImportCodexSessionRequest,
    request: Request,
) -> ImportCodexSessionResponse:
    """导入已有 Codex CLI session 为 kongming thread。

    防重复语义同 import-claude-session：codex_thread_id 已绑 → 返回原 thread。
    """
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    existing = tm.find_thread_by_codex_thread_id(body.codex_thread_id)
    if existing is not None:
        return ImportCodexSessionResponse(
            thread=await _to_dto(existing, tm),
            imported=False,
        )
    new_thread = await tm.create_thread(
        body.name,
        "",
        backend_kind="codex",
    )
    bound = await tm.bind_codex_thread(
        new_thread.id,
        body.codex_thread_id,
        body.cwd,
    )
    return ImportCodexSessionResponse(
        thread=await _to_dto(bound, tm),
        imported=True,
    )


@router.patch("/{thread_id}")
async def rename_thread(
    thread_id: str,
    body: RenameThreadRequest,
    request: Request,
) -> ThreadMetadataDTO:
    """更新 thread 属性（重命名 / 置顶）。"""
    _validate_thread_id(thread_id)
    tm: ThreadManagerProtocol = request.app.state.thread_manager

    meta: ThreadMetadata | None = None

    if body.name is not None:
        try:
            meta = await tm.rename_thread(thread_id, body.name)
        except KeyError as exc:
            raise ThreadNotFoundError(f"thread not found: {thread_id}") from exc

    if body.is_pinned is not None:
        try:
            meta = await tm.pin_thread(thread_id, body.is_pinned)
        except KeyError as exc:
            raise ThreadNotFoundError(f"thread not found: {thread_id}") from exc

    if body.is_archived is not None:
        try:
            meta = await tm.set_archived(thread_id, body.is_archived)
        except KeyError as exc:
            raise ThreadNotFoundError(f"thread not found: {thread_id}") from exc

    if meta is None:
        # 什么都没改，读取当前状态返回
        from hosts.web.threads.metadata import read_thread_metadata

        meta_read = await asyncio.to_thread(
            read_thread_metadata, request.app.state.kongming_home, thread_id
        )
        if meta_read is None:
            raise ThreadNotFoundError(f"thread not found: {thread_id}")
        return await _to_dto(meta_read, tm)

    return await _to_dto(meta, tm)


@router.patch("/{thread_id}/preset")
async def update_thread_preset(
    thread_id: str,
    body: UpdateThreadPresetRequest,
    request: Request,
) -> ThreadMetadataDTO:
    """更新 Generic Chat thread 的模型 preset。

    运行中的 turn 保持原 provider；下一次 ``user.input`` 前 ThreadManager 会
    确保 cell runtime 已按新 preset 重建。
    """
    _validate_thread_id(thread_id)
    preset_id = body.preset_id.strip()
    manager = request.app.state.model_catalog_manager
    try:
        manager.get_preset(preset_id)
    except ModelProviderCatalogError as exc:
        raise ModelCatalogWebError(exc) from exc
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    try:
        meta = await tm.update_thread_preset(thread_id, preset_id)
    except KeyError as exc:
        raise ThreadNotFoundError(f"thread not found: {thread_id}") from exc
    except ModelProviderCatalogError as exc:
        raise ModelCatalogWebError(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ThreadPresetRefreshError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return await _to_dto(meta, tm)


@router.get("/{thread_id}/usage")
async def get_thread_usage(thread_id: str, request: Request) -> dict[str, Any]:
    """v2 新端点：返回 thread 当前 token 用量 DTO（按通道分支返回不同 DTO）。

    内部走 ``manager.get_thread_usage(thread_id)`` 派生 SDK 真源（jsonl/rollout）。
    返回 ``{"usage": <ClaudeUsage|CodexUsage|GenericChat*Usage|None>}``——前端按
    ``usage.provider`` 字段做 narrowing 分支渲染。

    错误：

    - 404 thread 不存在
    - 200 + ``usage=None`` thread 没绑 SDK 真源 / 派生失败（不是错误，前端 StatusLine 留空）
    """
    _validate_thread_id(thread_id)
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    metas = await asyncio.to_thread(tm.list_threads)
    if not any(m.id == thread_id for m in metas):
        raise ThreadNotFoundError(f"thread not found: {thread_id}")
    usage = await tm.usage_manager.get_thread_usage(thread_id)
    return {"usage": usage.model_dump() if usage is not None else None}


@router.delete("/{thread_id}", status_code=204)
async def delete_thread(thread_id: str, request: Request) -> None:
    """删除 thread。

    语义：thread 不存在 → 404。``ThreadManager.delete_thread`` 自身幂等，
    但 REST 层显式查存在性以返回 404 —— 用 ``list_threads`` 反查（开销可
    接受，v0.1.5 thread 数量少）。
    """
    _validate_thread_id(thread_id)
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    metas = await asyncio.to_thread(tm.list_threads)
    if not any(m.id == thread_id for m in metas):
        raise ThreadNotFoundError(f"thread not found: {thread_id}")
    await tm.delete_thread(thread_id)


def _permissions_manager(request: Request) -> ThreadPermissionsManagerProtocol:
    """返回 Web 装配层共享的 REST permissions 门户。"""
    manager: ThreadPermissionsManagerProtocol | None = getattr(
        request.app.state,
        "thread_permissions_manager",
        None,
    )
    if manager is None:
        raise HTTPException(status_code=503, detail="permissions manager not available")
    return manager


async def _require_thread(thread_id: str, request: Request) -> None:
    """验证路径 thread 存在并具有稳定生命周期身份。"""
    _validate_thread_id(thread_id)
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    metadata = await asyncio.to_thread(tm.list_threads)
    if not any(item.id == thread_id for item in metadata):
        raise ThreadNotFoundError(f"thread not found: {thread_id}")


@router.get("/{thread_id}/permissions", response_model=ThreadPermissionsDTO)
async def get_thread_permissions(
    thread_id: str,
    request: Request,
) -> ThreadPermissionsDTO:
    """读取当前 thread 的独立 permissions 本子。"""
    await _require_thread(thread_id, request)
    try:
        return await _permissions_manager(request).get(thread_id)
    except ThreadPermissionsStorageError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "permissions_storage_unavailable",
                "thread_id": thread_id,
                "message": exc.message,
            },
        ) from exc


@router.put("/{thread_id}/permissions", response_model=ThreadPermissionsDTO)
async def update_thread_permissions(
    thread_id: str,
    body: UpdateThreadPermissionsRequest,
    request: Request,
) -> ThreadPermissionsDTO:
    """按 revision CAS 整本替换当前 thread 的 allow/deny。"""
    await _require_thread(thread_id, request)
    if body.thread_id != thread_id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "thread_id_mismatch",
                "path_thread_id": thread_id,
                "body_thread_id": body.thread_id,
            },
        )
    try:
        return await _permissions_manager(request).replace(
            thread_id,
            allow=body.allow,
            deny=body.deny,
            expected_revision=body.revision,
        )
    except ThreadPermissionsRevisionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "permissions_revision_conflict",
                "thread_id": thread_id,
                "expected_revision": exc.expected_revision,
                "actual_revision": exc.actual_revision,
            },
        ) from exc
    except ThreadPermissionsValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_permission_expression",
                "thread_id": thread_id,
                "message": exc.message,
            },
        ) from exc
    except ThreadPermissionsStorageError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "permissions_storage_unavailable",
                "thread_id": thread_id,
                "message": exc.message,
            },
        ) from exc


@router.get("/{thread_id}/claude_history")
async def get_claude_history(
    thread_id: str,
    request: Request,
    include_tools: bool = Query(default=False),
) -> dict[str, list[dict[str, object]]]:
    """拉取 ``claude_code`` 后端 thread 的 SDK 持久化历史（v0.2.0）。

    流程：
    1. 由 ``thread_id`` 反查 :class:`ThreadMetadata`（同 :func:`delete_thread`）。
    2. 校验 ``backend_kind == "claude_code"`` 且已绑定非空 ``claude_thread_id``。
    3. 用 ``cwd`` + ``claude_thread_id`` 拼出 JSONL 路径，存在性独立 404。
    4. :func:`parse_jsonl_history` 同步 IO，用 :func:`asyncio.to_thread` 隔离。
       Web 回放只返回最近 ``CLAUDE_HISTORY_MAX_MESSAGES`` 条，控制超大 session
       的浏览器载荷；默认 ``include_tools=false``，只给历史阅读视图返回自然语言
       与 thinking 内容。

    返回 ``{"messages": [<NormalizedMessage dict>, ...]}``——v0.2.0 暂不为
    history 引入 DTO，前端用 ``protocol.ts`` 已有的 ``NormalizedMessage`` 类型
    解析。

    错误：

    - 400 thread 不存在 / ``backend_kind`` 非 ``claude_code`` / 未绑定 claude_thread_id
    - 404 jsonl 文件不存在（thread 已绑但 SDK 文件被外部清理）
    """
    _validate_thread_id(thread_id)
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    metas = await asyncio.to_thread(tm.list_threads)
    meta = next((m for m in metas if m.id == thread_id), None)
    if meta is None:
        raise HTTPException(status_code=400, detail="thread not found")
    if meta.backend_kind != "claude_code":
        raise HTTPException(
            status_code=400,
            detail="thread backend_kind is not claude_code",
        )
    if not meta.claude_thread_id:
        raise HTTPException(
            status_code=400,
            detail="thread has no bound claude_thread_id",
        )
    path = jsonl_path_for(meta.cwd, meta.claude_thread_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"jsonl file not found: {path}")
    messages = await asyncio.to_thread(
        parse_jsonl_history,
        path,
        meta.claude_thread_id,
        max_messages=CLAUDE_HISTORY_MAX_MESSAGES,
        include_tools=include_tools,
    )
    return {"messages": messages}


@router.get("/{thread_id}/workspace-context")
async def get_workspace_context(
    thread_id: str,
    request: Request,
) -> WorkspaceContextDTO:
    """返回当前 thread 的共享 workspace 上下文。"""
    _validate_thread_id(thread_id)
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    metas = await asyncio.to_thread(tm.list_threads)
    meta = get_thread_meta(thread_id, metas)
    if meta is None:
        raise ThreadNotFoundError(f"thread not found: {thread_id}")
    return _to_workspace_context(
        meta,
        server_workspace_root=cast(Path, request.app.state.workspace_root),
    )


@router.get("/{thread_id}/evolution/reviews")
async def get_evolution_reviews(
    thread_id: str,
    request: Request,
) -> list[EvolutionReviewDTO]:
    _validate_thread_id(thread_id)
    await asyncio.to_thread(_require_thread_meta, request, thread_id)
    manager = _evolution_manager(request)
    reviews = await manager.list_review_records_for_session(thread_id)
    out: list[EvolutionReviewDTO] = []
    for review, decision in reviews:
        review_id = _review_id_for_run(review.run_id)
        out.append(
            _to_evolution_review_dto(
                review_id=review_id,
                review=review,
                decision=decision,
            )
        )
    return out


@router.post("/{thread_id}/evolution/reviews/{review_id}/decisions")
async def post_evolution_review_decision(
    thread_id: str,
    review_id: str,
    body: EvolutionDecisionRequest,
    request: Request,
) -> EvolutionDecisionResponse:
    _validate_thread_id(thread_id)
    meta = await asyncio.to_thread(_require_thread_meta, request, thread_id)
    manager = _evolution_manager(request)
    try:
        review, record = await manager.apply_review_decision(
            thread_id=thread_id,
            review_id=review_id,
            nutrient_id=body.nutrient_id,
            decision=body.decision,
            workspace_root=_workspace_root_for_meta(request, meta),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return EvolutionDecisionResponse(
        review=_to_evolution_review_dto(
            review_id=review_id,
            review=review,
            decision=record,
        )
    )


@router.post(
    "/{thread_id}/evolution/reviews/{review_id}/reapply",
    response_model=EvolutionDecisionResponse,
)
async def post_evolution_review_reapply(
    thread_id: str,
    review_id: str,
    request: Request,
) -> EvolutionDecisionResponse:
    _validate_thread_id(thread_id)
    meta = await asyncio.to_thread(_require_thread_meta, request, thread_id)
    manager = _evolution_manager(request)
    try:
        review, record = await manager.reapply_review_decisions(
            thread_id=thread_id,
            review_id=review_id,
            workspace_root=_workspace_root_for_meta(request, meta),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return EvolutionDecisionResponse(
        review=_to_evolution_review_dto(
            review_id=review_id,
            review=review,
            decision=record,
        )
    )


@router.get("/{thread_id}/workspace-tree")
async def get_workspace_tree(
    thread_id: str,
    request: Request,
    path: str = "",
) -> WorkspaceTreeDTO:
    """列出 workspace 某一层目录的直接子项。"""
    _validate_thread_id(thread_id)
    try:
        meta = await asyncio.to_thread(_require_thread_meta, request, thread_id)
        root = require_workspace_root(
            meta,
            fallback_to=cast(Path, request.app.state.workspace_root),
        )
        entries = await asyncio.to_thread(list_workspace_entries, root, path)
    except ThreadNotFoundError:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NotADirectoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WorkspaceTreeDTO(
        path=path,
        entries=[WorkspaceTreeNodeDTO.model_validate(item) for item in entries],
    )


@router.get("/{thread_id}/workspace-file")
async def get_workspace_file(
    thread_id: str,
    request: Request,
    path: str,
) -> WorkspaceFileDTO:
    """读取 workspace 文本文件。"""
    _validate_thread_id(thread_id)
    try:
        meta = await asyncio.to_thread(_require_thread_meta, request, thread_id)
        root = require_workspace_root(
            meta,
            fallback_to=cast(Path, request.app.state.workspace_root),
        )
        resolved_path = path.strip()
        if resolved_path.startswith("/"):
            abs_p = Path(resolved_path).resolve()
            with contextlib.suppress(ValueError):
                resolved_path = str(abs_p.relative_to(root.resolve()))
        payload = await asyncio.to_thread(read_workspace_text_file, root, resolved_path)
    except ThreadNotFoundError:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IsADirectoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WorkspaceFileDTO.model_validate(payload)


@router.put("/{thread_id}/workspace-file")
async def put_workspace_file(
    thread_id: str,
    body: UpdateWorkspaceFileRequest,
    request: Request,
) -> WorkspaceFileDTO:
    """保存 workspace 文本文件。"""
    _validate_thread_id(thread_id)
    try:
        meta = await asyncio.to_thread(_require_thread_meta, request, thread_id)
        root = require_workspace_root(
            meta,
            fallback_to=cast(Path, request.app.state.workspace_root),
        )
        payload = await asyncio.to_thread(
            write_workspace_text_file,
            root,
            body.path,
            body.content,
        )
    except ThreadNotFoundError:
        raise
    except WorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WorkspaceFileDTO.model_validate(payload)


__all__ = ["THREAD_ID_RE", "router"]
