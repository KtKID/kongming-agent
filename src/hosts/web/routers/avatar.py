"""Avatar 消息 REST API 路由。

本脚本把 AvatarManager 暴露为 `/api/avatar/v1/*` REST 合同。关键流程是
Router 从 app.state 读取 AvatarManager，解析 Web cookie 或 XSpace mobile
device token scope，执行消息注册、拉取、ack 和 chat disabled 响应。关键函数
职责：鉴权 helper 收口 scope/CSRF 边界，DTO helper 负责 snake_case 内部模型到
camelCase wire 响应的转换。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from hosts.web.auth.middleware import (
    CSRF_HEADER_NAME,
    CSRF_HEADER_VALUE,
    SESSION_COOKIE_NAME,
    verify_session_cookie,
)
from hosts.web.avatar import (
    AvatarAckBatchRequest,
    AvatarAckRequest,
    AvatarAckStatus,
    AvatarCapabilities,
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
from hosts.web.xspace_mobile import errors as mobile_errors
from hosts.web.xspace_mobile.models import MobileDeviceRecord
from hosts.web.xspace_mobile.token_service import MobileDeviceTokenService

router = APIRouter(tags=["avatar"])

_READ_SCOPES = frozenset({"avatar.read", "thread.read"})
_ACK_SCOPES = frozenset({"avatar.ack"})
_CHAT_SCOPES = frozenset({"avatar.chat"})


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


class AvatarChatRequestBody(BaseModel):
    """Avatar chat REST 请求 DTO。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    text: str = Field(min_length=1, max_length=8000)
    thread_id: str | None = Field(default=None, alias="threadId", max_length=256)
    device_id: str | None = Field(default=None, alias="deviceId", max_length=256)
    capabilities: dict[str, bool] = Field(default_factory=dict)


def _manager(request: Request) -> AvatarManager:
    """读取 app.state 中的 AvatarManager。"""
    manager = getattr(request.app.state, "avatar_manager", None)
    if not isinstance(manager, AvatarManager):
        raise avatar_errors.invalid_request("avatar manager is not configured")
    return manager


def _token_service(request: Request) -> MobileDeviceTokenService:
    """读取 app.state 中的移动 token service。"""
    service = getattr(request.app.state, "xspace_mobile_token_service", None)
    if not isinstance(service, MobileDeviceTokenService):
        raise avatar_errors.forbidden("xspace mobile token service is not configured")
    return service


def _error_response(error: avatar_errors.AvatarMessageError) -> JSONResponse:
    """把 AvatarMessageError 转换为 JSONResponse。"""
    return JSONResponse(
        status_code=error.http_status,
        content=avatar_errors.avatar_error_body(error),
    )


def _csrf_valid(request: Request) -> bool:
    """判断请求是否携带 Web cookie 调试路径需要的 CSRF header。"""
    return request.headers.get(CSRF_HEADER_NAME) == CSRF_HEADER_VALUE


def _cookie_valid(request: Request) -> bool:
    """判断 Web cookie 登录态是否有效。"""
    serializer = getattr(request.app.state, "serializer", None)
    if serializer is None:
        return False
    payload = verify_session_cookie(request.cookies.get(SESSION_COOKIE_NAME), serializer)
    if payload is None:
        return False
    request.state.session_payload = payload
    return True


def _bearer_device(request: Request) -> MobileDeviceRecord | None:
    """解析并校验 Authorization Bearer device token。"""
    raw = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not raw.startswith(prefix):
        return None
    token = raw[len(prefix) :].strip()
    if not token:
        return None
    try:
        return _token_service(request).validate_device_token(token)
    except mobile_errors.MobilePairingError:
        return None


def _authorize(
    request: Request,
    *,
    scopes: frozenset[str],
    allow_cookie: bool = True,
    require_csrf_for_cookie: bool = False,
) -> MobileDeviceRecord | None:
    """校验 Bearer scope 或 Web cookie 调试权限。

    关键输入：请求、允许 scope、cookie 调试开关和 CSRF 要求。
    关键输出：Bearer 鉴权时返回设备记录；cookie 鉴权时返回 None。
    """
    device = _bearer_device(request)
    if device is not None:
        if scopes and not scopes.intersection(device.scopes):
            raise avatar_errors.forbidden("avatar scope missing")
        return device

    if allow_cookie and _cookie_valid(request):
        if require_csrf_for_cookie and not _csrf_valid(request):
            raise avatar_errors.forbidden(f"CSRF guard: {CSRF_HEADER_NAME} required")
        return None

    raise avatar_errors.forbidden("avatar authentication required")


def _authorize_cookie(
    request: Request,
    *,
    require_csrf: bool = False,
) -> None:
    """校验 Web cookie 调试权限。

    关键输入：请求和 CSRF 要求。
    关键输出：合法 cookie 通过；失败时抛 AvatarMessageError。
    """
    if not _cookie_valid(request):
        raise avatar_errors.forbidden("avatar web cookie required")
    if require_csrf and not _csrf_valid(request):
        raise avatar_errors.forbidden(f"CSRF guard: {CSRF_HEADER_NAME} required")


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
        "requiredScopes": capabilities.required_scopes,
    }


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
    """Avatar chat v1 disabled endpoint。"""
    try:
        device = _authorize(
            request,
            scopes=_CHAT_SCOPES,
            require_csrf_for_cookie=True,
        )
        device_id = payload.device_id or (device.device_id if device is not None else None)
        _manager(request).chat(
            AvatarChatRequest(
                text=payload.text,
                thread_id=payload.thread_id,
                device_id=device_id,
                capabilities=payload.capabilities,
            )
        )
    except avatar_errors.AvatarMessageError as exc:
        return _error_response(exc)
    return JSONResponse(content={"status": "ok"})


__all__ = ["router"]
