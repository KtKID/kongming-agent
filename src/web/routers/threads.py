"""Thread CRUD 路由。

端点：

- ``GET    /api/threads``                              — 列出所有 thread metadata
- ``POST   /api/threads``                              — 创建 thread；返回 metadata（201）
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
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, HTTPException, Request

from evolution.apply_executor import build_apply_job, execute_apply_job
from evolution.models import (
    DecisionItem,
    DecisionRecord,
    DecisionSummary,
    DecisionTarget,
    EvolutionNutrient,
)
from evolution.state_store import EvolutionStateStore
from evolution.store import EvolutionStore, resolve_evolution_root
from web.claude_code.jsonl_history import jsonl_path_for, parse_jsonl_history
from web.claude_code.transcript_usage import parse_transcript_usage
from web.errors import InvalidThreadIdError, ThreadNotFoundError
from web.protocol import (
    CreateThreadRequest,
    EvolutionDecisionItemDTO,
    EvolutionDecisionRequest,
    EvolutionDecisionResponse,
    EvolutionDecisionSummaryDTO,
    EvolutionNutrientDTO,
    EvolutionReviewDTO,
    ImportClaudeSessionRequest,
    ImportClaudeSessionResponse,
    ImportCodexSessionRequest,
    ImportCodexSessionResponse,
    RenameThreadRequest,
    ThreadMetadataDTO,
    UpdateWorkspaceFileRequest,
    WorkspaceContextDTO,
    WorkspaceFileDTO,
    WorkspaceTreeDTO,
    WorkspaceTreeNodeDTO,
)
from web.workspace import (
    WorkspaceError,
    get_thread_meta,
    list_workspace_entries,
    read_workspace_text_file,
    require_workspace_root,
    write_workspace_text_file,
)

if TYPE_CHECKING:
    from web.thread_metadata import ThreadMetadata
    from web.types import ThreadManagerProtocol

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/threads", tags=["threads"])

THREAD_ID_RE: re.Pattern[str] = re.compile(r"^thread-[a-f0-9]{12}$")


def _validate_thread_id(thread_id: str) -> None:
    if not THREAD_ID_RE.match(thread_id):
        raise InvalidThreadIdError(
            f"invalid thread_id: {thread_id!r}; must match ^thread-[a-f0-9]{{12}}$"
        )


def _to_dto(meta: ThreadMetadata) -> ThreadMetadataDTO:
    prompt = meta.cumulative_prompt_tokens
    completion = meta.cumulative_completion_tokens
    total = meta.cumulative_total_tokens
    cache_read: int | None = meta.cumulative_cache_read_tokens or None
    cache_creation: int | None = meta.cumulative_cache_creation_tokens or None

    if meta.backend_kind == "claude_code" and meta.claude_thread_id and meta.cwd:
        usage = parse_transcript_usage(jsonl_path_for(meta.cwd, meta.claude_thread_id))
        prompt = usage.input_tokens
        completion = usage.output_tokens
        total = usage.input_tokens + usage.output_tokens
        cache_read = usage.cache_read_tokens or None
        cache_creation = usage.cache_creation_tokens or None

    return ThreadMetadataDTO(
        id=meta.id,
        name=meta.name,
        preset_id=meta.preset_id,
        backend_kind=meta.backend_kind,
        claude_thread_id=meta.claude_thread_id,
        codex_thread_id=meta.codex_thread_id,
        cwd=meta.cwd,
        created_at=meta.created_at,
        updated_at=meta.updated_at,
        message_count=meta.message_count,
        cumulative_prompt_tokens=prompt,
        cumulative_completion_tokens=completion,
        cumulative_total_tokens=total,
        cumulative_cache_read_tokens=cache_read,
        cumulative_cache_creation_tokens=cache_creation,
        schema_version=meta.schema_version,
    )


def _to_workspace_context(meta: ThreadMetadata) -> WorkspaceContextDTO:
    workspace_root = meta.cwd.strip()
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


def _evolution_store(request: Request) -> EvolutionStore:
    cfg = request.app.state.config
    root_dir = resolve_evolution_root(cfg.evolution.learning.root_path)
    return EvolutionStore(
        root_dir=root_dir,
        state_store=EvolutionStateStore(root_dir),
    )


def _review_id_for_run(run_id: str) -> str:
    return f"evo-review:{run_id}"


def _decision_target_for_value(value: str) -> DecisionTarget | None:
    if value == "accept_memory":
        return "memory"
    if value == "accept_skill":
        return "skill"
    return None


async def _apply_decision_item(
    *,
    request: Request,
    store: EvolutionStore,
    meta: ThreadMetadata,
    review_id: str,
    run_id: str,
    nutrient: EvolutionNutrient,
    decision: DecisionItem,
) -> DecisionRecord:
    workspace_root = (
        meta.cwd.strip()
        if isinstance(meta.cwd, str) and meta.cwd.strip()
        else str(getattr(request.app.state, "workspace_root", ""))
    )
    job = build_apply_job(
        review_id=review_id,
        session_id=meta.id,
        run_id=run_id,
        nutrient_id=decision.nutrient_id,
        decision=decision,
        workspace_root=Path(workspace_root).expanduser().resolve(),
        created_at_ms=int(time.time() * 1000),
    )
    await store.write_apply_job(job)
    outcome = await execute_apply_job(
        cfg=request.app.state.config,
        store=store,
        job=job,
        nutrient=nutrient,
    )
    return outcome.decision_record


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
    return [_to_dto(m) for m in metas]


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
    if normalized_cwd and not normalized_cwd.startswith("/"):
        raise HTTPException(
            status_code=400,
            detail="cwd must be an absolute path",
        )
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    meta = await tm.create_thread(
        body.name,
        body.preset_id,
        backend_kind=body.backend_kind,
        cwd=normalized_cwd,
    )
    return _to_dto(meta)


@router.post("/import-claude-session")
async def import_claude_session(
    body: ImportClaudeSessionRequest,
    request: Request,
) -> ImportClaudeSessionResponse:
    """导入已有 Claude Agent SDK session 为 kongming thread（v0.2.0 dev #8）。

    防重复语义：

    1. ``find_thread_by_claude_thread_id(claude_thread_id)`` 命中 → 返回该 thread
       + ``imported=False``（用户重复点同一 session，不再多创建 thread）。
    2. 未命中 → ``create_thread(name, "", backend_kind="claude_code")`` 后
       ``bind_claude_thread(thread_id, claude_thread_id, cwd)`` → ``imported=True``。

    bind_claude_thread 内部已守 invariant（已绑 / 1:1 冲突会抛），这里不再
    重复检查；竞态下两个 import 同时打到同 ``claude_thread_id`` 时，第二个会被
    bind 阶段的全局反查拦下抛 ``ClaudeThreadConflictError``，走 500（v0.2 接受
    极小概率失败 —— 单用户场景下不构成问题）。

    校验：DTO 已强制 ``cwd`` 必须以 ``/`` 开头、``name`` / ``claude_thread_id``
    长度上限。鉴权由全局 :class:`web.auth.AuthMiddleware` 兜底。
    """
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    existing = tm.find_thread_by_claude_thread_id(body.claude_thread_id)
    if existing is not None:
        return ImportClaudeSessionResponse(
            thread=_to_dto(existing),
            imported=False,
        )
    new_thread = await tm.create_thread(
        body.name,
        "",
        backend_kind="claude_code",
    )
    bound = await tm.bind_claude_thread(
        new_thread.id,
        body.claude_thread_id,
        body.cwd,
    )
    return ImportClaudeSessionResponse(
        thread=_to_dto(bound),
        imported=True,
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
            thread=_to_dto(existing),
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
        thread=_to_dto(bound),
        imported=True,
    )


@router.patch("/{thread_id}")
async def rename_thread(
    thread_id: str,
    body: RenameThreadRequest,
    request: Request,
) -> ThreadMetadataDTO:
    """重命名 thread。"""
    _validate_thread_id(thread_id)
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    try:
        meta = await tm.rename_thread(thread_id, body.name)
    except KeyError as exc:
        raise ThreadNotFoundError(f"thread not found: {thread_id}") from exc
    return _to_dto(meta)


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


@router.get("/{thread_id}/claude_history")
async def get_claude_history(
    thread_id: str,
    request: Request,
) -> dict[str, list[dict[str, object]]]:
    """拉取 ``claude_code`` 后端 thread 的 SDK 持久化历史（v0.2.0）。

    流程：
    1. 由 ``thread_id`` 反查 :class:`ThreadMetadata`（同 :func:`delete_thread`）。
    2. 校验 ``backend_kind == "claude_code"`` 且已绑定非空 ``claude_thread_id``。
    3. 用 ``cwd`` + ``claude_thread_id`` 拼出 JSONL 路径，存在性独立 404。
    4. :func:`parse_jsonl_history` 同步 IO，用 :func:`asyncio.to_thread` 隔离。

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
    messages = await asyncio.to_thread(parse_jsonl_history, path, meta.claude_thread_id)
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
    return _to_workspace_context(meta)


@router.get("/{thread_id}/evolution/reviews")
async def get_evolution_reviews(
    thread_id: str,
    request: Request,
) -> list[EvolutionReviewDTO]:
    _validate_thread_id(thread_id)
    await asyncio.to_thread(_require_thread_meta, request, thread_id)
    store = _evolution_store(request)
    reviews = await store.list_reviews_for_session(thread_id)
    out: list[EvolutionReviewDTO] = []
    for review in reviews:
        review_id = _review_id_for_run(review.run_id)
        decision = await store.read_decision(review_id)
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
    store = _evolution_store(request)
    reviews = await store.list_reviews_for_session(thread_id)
    review = next((item for item in reviews if _review_id_for_run(item.run_id) == review_id), None)
    if review is None:
        raise HTTPException(status_code=404, detail=f"review not found: {review_id}")
    nutrient_ids = {nutrient.nutrient_id for nutrient in review.nutrients}
    if body.nutrient_id not in nutrient_ids:
        raise HTTPException(
            status_code=404,
            detail=f"nutrient not found in review: {body.nutrient_id}",
        )
    existing = await store.read_decision(review_id)
    items = list(existing.items if existing is not None else ())
    now_ms = int(time.time() * 1000)
    new_item = DecisionItem(
        nutrient_id=body.nutrient_id,
        decision=body.decision,
        target=_decision_target_for_value(body.decision),
        decided_at_ms=now_ms,
    )
    replaced = False
    for index, item in enumerate(items):
        if item.nutrient_id == body.nutrient_id:
            items[index] = new_item
            replaced = True
            break
    if not replaced:
        items.append(new_item)
    record = await store.write_decision(
        DecisionRecord(
            review_id=review_id,
            session_id=thread_id,
            run_id=review.run_id,
            summary=DecisionSummary(
                total=len(review.nutrients),
                accepted_memory=0,
                accepted_skill=0,
                ignored=0,
                pending=len(review.nutrients),
            ),
            items=tuple(items),
        )
    )
    decision_item = next(
        (item for item in record.items if item.nutrient_id == body.nutrient_id), None
    )
    nutrient = next(
        (item for item in review.nutrients if item.nutrient_id == body.nutrient_id), None
    )
    if nutrient is None:
        raise HTTPException(
            status_code=404,
            detail=f"nutrient not found in review: {body.nutrient_id}",
        )
    if decision_item is None:
        raise HTTPException(status_code=500, detail=f"decision item missing: {body.nutrient_id}")
    record = await _apply_decision_item(
        request=request,
        store=store,
        meta=meta,
        review_id=review_id,
        run_id=review.run_id,
        nutrient=nutrient,
        decision=decision_item,
    )
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
    store = _evolution_store(request)
    reviews = await store.list_reviews_for_session(thread_id)
    review = next((item for item in reviews if _review_id_for_run(item.run_id) == review_id), None)
    if review is None:
        raise HTTPException(status_code=404, detail=f"review not found: {review_id}")
    record = await store.read_decision(review_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"decision record not found: {review_id}")

    actionable = [
        item
        for item in record.items
        if item.decision in {"accept_memory", "accept_skill"}
        and item.applied_status in {"pending", "failed"}
    ]
    nutrient_map = {nutrient.nutrient_id: nutrient for nutrient in review.nutrients}
    for item in actionable:
        nutrient = nutrient_map.get(item.nutrient_id)
        if nutrient is None:
            continue
        record = await _apply_decision_item(
            request=request,
            store=store,
            meta=meta,
            review_id=review_id,
            run_id=review.run_id,
            nutrient=nutrient,
            decision=item,
        )

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
        root = require_workspace_root(meta)
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
        try:
            root = require_workspace_root(meta)
        except WorkspaceError:
            root = cast(Path, request.app.state.workspace_root)
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
        root = require_workspace_root(meta)
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
