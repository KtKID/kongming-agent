"""Avatar 消息 REST API 路由。

本脚本把 AvatarManager 暴露为 `/api/avatar/v1/*` REST 合同。当前 v1 联调期
鉴权 helper 直接放行请求，便于 XSpace 先跑通消息拉取、ack 和展示闭环。关键函数
职责：鉴权 helper 保留原参数形状并返回通过，DTO helper 负责 snake_case 内部模型到
camelCase wire 响应的转换。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from hosts.web.approvals.global_inbox import get_inbox_broadcaster
from hosts.web.avatar import (
    AvatarAckBatchRequest,
    AvatarAckRequest,
    AvatarAckStatus,
    AvatarCapabilities,
    AvatarChatAccepted,
    AvatarChatRequest,
    AvatarManager,
    AvatarMessageAction,
    AvatarMessageInput,
    AvatarMessageLevel,
    AvatarMessageListQuery,
    AvatarMessageSnapshot,
    AvatarMessageStatus,
)
from hosts.web.avatar import errors as avatar_errors
from hosts.web.protocol.rest_models import UserInputAttachment

router = APIRouter(tags=["avatar"])
logger = logging.getLogger(__name__)

_READ_SCOPES = frozenset({"avatar.read", "thread.read"})
_ACK_SCOPES = frozenset({"avatar.ack"})
_CHAT_SCOPES = frozenset({"avatar.chat"})
_APPROVAL_SCOPES = frozenset({"avatar.chat"})


class RegisterAvatarMessageRequest(BaseModel):
    """调试注册 Avatar 消息请求 DTO。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=2000)
    level: AvatarMessageLevel = AvatarMessageLevel.INFO
    priority: int = Field(default=50, ge=0, le=100)
    thread_id: str | None = Field(default=None, alias="threadId", max_length=256)
    run_id: str | None = Field(default=None, alias="runId", max_length=256)
    request_id: str | None = Field(default=None, alias="requestId", max_length=256)
    action: AvatarMessageAction | None = None
    dedupe_key: str | None = Field(default=None, alias="dedupeKey", min_length=1, max_length=512)
    expires_at: datetime | None = Field(default=None, alias="expiresAt")
    metadata: dict[str, Any] = Field(default_factory=dict)


class AckAvatarMessageRequest(BaseModel):
    """Avatar 单条 ack 请求 DTO。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    status: AvatarAckStatus = AvatarAckStatus.CONSUMED
    consumer_id: str = Field(
        default="xspace-avatar",
        alias="consumerId",
        min_length=1,
        max_length=160,
    )
    at: datetime | None = None


class AckAvatarMessagesRequest(BaseModel):
    """Avatar 批量 ack 请求 DTO。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    message_ids: list[str] = Field(alias="messageIds", min_length=1, max_length=100)
    status: AvatarAckStatus = AvatarAckStatus.CONSUMED
    consumer_id: str = Field(
        default="xspace-avatar",
        alias="consumerId",
        min_length=1,
        max_length=160,
    )
    at: datetime | None = None


class AvatarRestChatMessageBody(BaseModel):
    """Avatar REST chat message DTO。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    text: str = Field(min_length=1, max_length=8000)
    reasoning_effort: Literal["none", "low", "medium", "high", "max"] | None = Field(
        default=None,
        alias="reasoningEffort",
    )
    attachments: list[UserInputAttachment] | None = None
    metadata: dict[str, Any] | None = None


class AvatarRestChatClientBody(BaseModel):
    """Avatar REST chat client DTO。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    device_id: str | None = Field(default=None, alias="deviceId", max_length=256)
    client_message_id: str = Field(alias="clientMessageId", min_length=1, max_length=256)
    capabilities: dict[str, bool] = Field(default_factory=dict)


class AvatarChatRequestBody(BaseModel):
    """Avatar chat REST 请求 DTO。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    thread_id: str | None = Field(default=None, alias="threadId", max_length=256)
    preset_id: str | None = Field(default=None, alias="presetId", max_length=256)
    cwd: str | None = None
    message: AvatarRestChatMessageBody
    client: AvatarRestChatClientBody


class AvatarResolveApprovalRequest(BaseModel):
    """Avatar 审批回写请求 DTO。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    thread_id: str | None = Field(default=None, alias="threadId", max_length=256)
    call_id: str | None = Field(default=None, alias="callId", max_length=256)
    request_id: str | None = Field(default=None, alias="requestId", max_length=256)
    action: Literal["accept_once", "accept_for_session", "reject"]
    client_id: str | None = Field(
        default=None,
        alias="clientId",
        max_length=160,
    )


def _manager(request: Request) -> AvatarManager:
    """读取 app.state 中的 AvatarManager。"""
    manager = getattr(request.app.state, "avatar_manager", None)
    if not isinstance(manager, AvatarManager):
        raise avatar_errors.invalid_request("avatar manager is not configured")
    return manager


def _approval_inbox_broadcaster(request: Request) -> Any:
    """读取 app.state 中的 ApprovalInboxBroadcaster 兼容对象。"""
    manager = getattr(request.app.state, "approval_inbox_broadcaster", None)
    if manager is None:
        manager = get_inbox_broadcaster()
        request.app.state.approval_inbox_broadcaster = manager
    resolve = getattr(manager, "resolve", None)
    if not callable(resolve):
        raise avatar_errors.invalid_request("avatar approval inbox is not configured")
    return manager


def _error_response(error: avatar_errors.AvatarMessageError) -> JSONResponse:
    """把 AvatarMessageError 转换为 JSONResponse。"""
    return JSONResponse(
        status_code=error.http_status,
        content=avatar_errors.avatar_error_body(error),
    )


def _authorize(
    request: Request,
    *,
    scopes: frozenset[str],
    allow_cookie: bool = True,
    require_csrf_for_cookie: bool = False,
) -> None:
    """Avatar v1 联调期放行请求，输入保留原鉴权参数，输出恒为通过。"""
    return None


def _authorize_cookie(
    request: Request,
    *,
    require_csrf: bool = False,
) -> None:
    """Avatar v1 联调期放行 Web 调试注册请求，输出恒为通过。"""
    return None


def _message_to_wire(message: AvatarMessageSnapshot) -> dict[str, Any]:
    """把内部消息快照转换为 camelCase wire DTO。"""
    payload = message.input
    return {
        "messageId": message.message_id,
        "sequence": message.sequence,
        "revision": message.revision,
        "status": message.status.value,
        "source": payload.source,
        "title": payload.title,
        "body": payload.body,
        "level": payload.level.value,
        "priority": payload.priority,
        "threadId": payload.thread_id,
        "runId": payload.run_id,
        "requestId": payload.request_id,
        "action": payload.action.model_dump(mode="json") if payload.action else None,
        "dedupeKey": payload.dedupe_key,
        "expiresAt": payload.expires_at.isoformat() if payload.expires_at else None,
        "ackedAt": message.acked_at.isoformat() if message.acked_at else None,
        "consumedAt": message.consumed_at.isoformat() if message.consumed_at else None,
        "createdAt": message.created_at.isoformat(),
        "updatedAt": message.updated_at.isoformat(),
        "metadata": payload.metadata,
    }


def _capabilities_to_wire(capabilities: AvatarCapabilities) -> dict[str, Any]:
    """把 AvatarCapabilities 转换为 camelCase wire DTO。"""
    return {
        "protocolVersion": capabilities.protocol_version,
        "messageRegistry": capabilities.message_registry,
        "restList": capabilities.rest_list,
        "restAck": capabilities.rest_ack,
        "wsNotifications": capabilities.ws_notifications,
        "avatarChat": capabilities.avatar_chat,
        "avatarRealtimeChat": capabilities.avatar_realtime_chat,
        "chatTransports": capabilities.chat_transports,
        "requiredScopes": capabilities.required_scopes,
    }


def _accepted_to_wire(accepted: AvatarChatAccepted) -> dict[str, Any]:
    """把 AvatarChatAccepted 转换为 camelCase wire DTO。"""
    return {
        "accepted": accepted.accepted,
        "threadId": accepted.thread_id,
        "runId": accepted.run_id,
        "transport": accepted.transport,
        "websocketUrl": accepted.websocket_url,
        "serverTime": accepted.server_time.isoformat(),
    }


def _approval_decision_for_action(
    action: str,
    *,
    remember_rule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 Avatar action 映射为 ApprovalManager.resolve 入参。"""
    if action == "accept_once":
        return {"allow": True, "remember": False}
    if action == "accept_for_session":
        if remember_rule is None:
            raise avatar_errors.invalid_request("remember rule is unavailable")
        return {
            "allow": True,
            "remember": True,
            "rememberRule": remember_rule,
        }
    return {"allow": False, "remember": False}


def _register_input(payload: RegisterAvatarMessageRequest) -> AvatarMessageInput:
    """把 debug register 请求转换为 AvatarMessageInput。"""
    return AvatarMessageInput(
        source=payload.source,
        title=payload.title,
        body=payload.body,
        level=payload.level,
        priority=payload.priority,
        thread_id=payload.thread_id,
        run_id=payload.run_id,
        request_id=payload.request_id,
        action=payload.action,
        dedupe_key=payload.dedupe_key,
        expires_at=payload.expires_at,
        metadata=payload.metadata,
    )


@router.get("/api/avatar/v1/capabilities")
async def get_avatar_capabilities(request: Request) -> JSONResponse:
    """返回 Avatar message registry 能力声明。"""
    try:
        _authorize(request, scopes=_READ_SCOPES)
        capabilities = _manager(request).capabilities()
    except avatar_errors.AvatarMessageError as exc:
        return _error_response(exc)
    return JSONResponse(content=_capabilities_to_wire(capabilities))


@router.post("/api/avatar/v1/messages")
async def register_avatar_message(
    payload: RegisterAvatarMessageRequest,
    request: Request,
) -> JSONResponse:
    """通过 Web cookie 调试路径注册 Avatar 消息。"""
    try:
        _authorize_cookie(request, require_csrf=True)
        message = _manager(request).register_message(_register_input(payload))
    except avatar_errors.AvatarMessageError as exc:
        return _error_response(exc)
    return JSONResponse(content=_message_to_wire(message))


@router.post("/api/avatar/v1/approvals/{request_id}/resolve")
async def resolve_avatar_approval(
    request_id: str,
    payload: AvatarResolveApprovalRequest,
    request: Request,
) -> JSONResponse:
    """Avatar 审批 resolve endpoint。"""
    try:
        _authorize(
            request,
            scopes=_APPROVAL_SCOPES,
            require_csrf_for_cookie=True,
        )
        if payload.request_id is not None and payload.request_id != request_id:
            raise avatar_errors.invalid_request("requestId does not match path")
        if payload.call_id is not None and payload.call_id != request_id:
            raise avatar_errors.invalid_request("callId does not match path")
        if not payload.thread_id:
            raise avatar_errors.invalid_request("threadId is required")
        manager = _approval_inbox_broadcaster(request)
        remember_rule: dict[str, Any] | None = None
        if payload.action == "accept_for_session":
            remember_rule_for = getattr(manager, "remember_rule_for", None)
            if callable(remember_rule_for):
                candidate = remember_rule_for(payload.thread_id, request_id)
                if isinstance(candidate, dict):
                    remember_rule = candidate
        decision = _approval_decision_for_action(
            payload.action,
            remember_rule=remember_rule,
        )
        ok = await manager.resolve(payload.thread_id, request_id, decision)
        logger.info(
            "avatar approval resolve: request_id=%s thread_id=%s action=%s client_id=%s ok=%s",
            request_id,
            payload.thread_id,
            payload.action,
            payload.client_id,
            ok,
        )
        if not ok:
            raise avatar_errors.approval_not_found(request_id)
    except avatar_errors.AvatarMessageError as exc:
        return _error_response(exc)
    return JSONResponse(
        content={
            "ok": True,
            "requestId": request_id,
            "action": payload.action,
        }
    )


@router.get("/api/avatar/v1/messages")
async def list_avatar_messages(
    request: Request,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    since: datetime | None = None,
    level: Annotated[list[AvatarMessageLevel] | None, Query()] = None,
    source: Annotated[list[str] | None, Query()] = None,
    thread_id: Annotated[str | None, Query(alias="threadId")] = None,
    status: Annotated[list[AvatarMessageStatus] | None, Query()] = None,
) -> JSONResponse:
    """列出 Avatar 消息。"""
    try:
        _authorize(request, scopes=_READ_SCOPES)
        result = _manager(request).list_messages(
            AvatarMessageListQuery(
                cursor=cursor,
                limit=limit,
                since=since,
                level=level,
                source=source,
                thread_id=thread_id,
                status=status,
            )
        )
    except avatar_errors.AvatarMessageError as exc:
        return _error_response(exc)
    return JSONResponse(
        content={
            "items": [_message_to_wire(item) for item in result.items],
            "nextCursor": result.next_cursor,
            "serverTime": result.server_time.isoformat(),
        }
    )


@router.post("/api/avatar/v1/messages/ack")
async def ack_avatar_messages(
    payload: AckAvatarMessagesRequest,
    request: Request,
) -> JSONResponse:
    """批量 ack Avatar 消息。"""
    try:
        _authorize(
            request,
            scopes=_ACK_SCOPES,
            require_csrf_for_cookie=True,
        )
        result = _manager(request).ack_messages(
            AvatarAckBatchRequest(
                message_ids=payload.message_ids,
                status=payload.status,
                consumer_id=payload.consumer_id,
                at=payload.at,
            )
        )
    except avatar_errors.AvatarMessageError as exc:
        return _error_response(exc)
    return JSONResponse(
        content={
            "results": [
                {
                    "messageId": item.message_id,
                    "ok": item.ok,
                    "message": _message_to_wire(item.message) if item.message else None,
                    "error": item.error,
                }
                for item in result.results
            ]
        }
    )


@router.post("/api/avatar/v1/messages/{message_id}/ack")
async def ack_avatar_message(
    message_id: str,
    payload: AckAvatarMessageRequest,
    request: Request,
) -> JSONResponse:
    """ack 单条 Avatar 消息。"""
    try:
        _authorize(
            request,
            scopes=_ACK_SCOPES,
            require_csrf_for_cookie=True,
        )
        message = _manager(request).ack_message(
            message_id,
            AvatarAckRequest(
                status=payload.status,
                consumer_id=payload.consumer_id,
                at=payload.at,
            ),
        )
    except avatar_errors.AvatarMessageError as exc:
        return _error_response(exc)
    return JSONResponse(content=_message_to_wire(message))


@router.post("/api/avatar/v1/chat")
async def post_avatar_chat(
    payload: AvatarChatRequestBody,
    request: Request,
) -> JSONResponse:
    """Avatar chat REST accepted endpoint。"""
    try:
        _authorize(
            request,
            scopes=_CHAT_SCOPES,
            require_csrf_for_cookie=True,
        )
        accepted = await _manager(request).chat(
            AvatarChatRequest(
                text=payload.message.text,
                thread_id=payload.thread_id,
                preset_id=payload.preset_id,
                cwd=payload.cwd or "",
                reasoning_effort=payload.message.reasoning_effort,
                attachments=payload.message.attachments,
                client_message_id=payload.client.client_message_id,
                device_id=payload.client.device_id,
                capabilities=payload.client.capabilities,
            )
        )
    except avatar_errors.AvatarMessageError as exc:
        return _error_response(exc)
    return JSONResponse(content=_accepted_to_wire(accepted))


__all__ = ["router"]
