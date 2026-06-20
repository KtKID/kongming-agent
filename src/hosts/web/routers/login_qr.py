"""XSpace Mobile 登录二维码 HTTP/Web 路由。

本脚本把 ``LoginQrManager`` 和 ``LoginQrAuthService`` 暴露为 FastAPI 路由，作用是
服务 `/login` 页面创建与确认扫码登录、XSpace APK claim/exchange，以及系统相机
fallback 页面。关键流程是登录页 create/status/confirm 使用 browser token 和 CSRF，
APK claim/exchange 使用 nonce 与状态机，exchange 成功返回 device token 和 WebView
handoff URL。关键函数职责：API handler 只做 DTO 转换、鉴权边界和错误响应，业务状态机
统一委托 Manager/AuthService。
"""

from __future__ import annotations

import html
import logging
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import HTMLResponse, JSONResponse

from hosts.web.xspace_mobile import errors
from hosts.web.xspace_mobile.login_qr_auth_service import LoginQrAuthService
from hosts.web.xspace_mobile.login_qr_manager import LoginQrManager
from hosts.web.xspace_mobile.models import (
    LoginQrClaimRecord,
    LoginQrSessionRecord,
    MobileDeviceDescriptor,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_DEFAULT_SCOPES = ["webview", "thread.read", "approval.resolve"]
_LOGIN_QR_TOKEN_HEADER = "X-Kongming-Login-Qr-Token"


class CreateLoginQrSessionRequest(BaseModel):
    """创建登录 QR session 请求 DTO。"""

    model_config = ConfigDict(extra="forbid")

    protocol_version: str = "1"
    client: str = "kongming-login"
    requested_scopes: list[str] = Field(default_factory=lambda: list(_DEFAULT_SCOPES))


class ClaimLoginQrSessionRequest(BaseModel):
    """Android claim 登录 QR 请求 DTO。"""

    model_config = ConfigDict(extra="forbid")

    protocol_version: str
    nonce: str
    device: MobileDeviceDescriptor
    capabilities: dict[str, bool] = Field(default_factory=dict)


class ConfirmLoginQrSessionRequest(BaseModel):
    """登录页确认登录 QR 请求 DTO。"""

    model_config = ConfigDict(extra="forbid")

    browser_token: str
    claim_id: str
    password: str


class ExchangeLoginQrSessionRequest(BaseModel):
    """Android exchange 登录 QR 请求 DTO。"""

    model_config = ConfigDict(extra="forbid")

    protocol_version: str
    claim_id: str
    nonce: str
    device_id: str


def _manager(request: Request) -> LoginQrManager:
    """读取 app.state 中的登录二维码 Manager。"""
    manager = getattr(request.app.state, "xspace_mobile_login_qr_manager", None)
    if not isinstance(manager, LoginQrManager):
        raise errors.invalid_request("xspace mobile login qr manager is not configured")
    return manager


def _auth_service(request: Request) -> LoginQrAuthService:
    """读取 app.state 中的登录二维码授权服务。"""
    service = getattr(request.app.state, "xspace_mobile_login_qr_auth_service", None)
    if not isinstance(service, LoginQrAuthService):
        raise errors.invalid_request("xspace mobile login qr auth service is not configured")
    return service


def _configured_server_origin(request: Request) -> str | None:
    """读取配置里的扫码登录 server origin。"""
    cfg = getattr(request.app.state, "config", None)
    web_cfg = getattr(cfg, "web", None)
    origin = getattr(web_cfg, "server_origin", None) or getattr(web_cfg, "public_origin", None)
    if isinstance(origin, str) and origin.strip():
        return origin.strip()
    return None


def _client_ip(request: Request) -> str:
    """从 Request 取客户端 IP。"""
    if request.client is None:
        return "unknown"
    return request.client.host or "unknown"


def _mobile_error(error: errors.MobilePairingError) -> JSONResponse:
    """把登录二维码业务错误转换为 JSONResponse。"""
    status_code, body = errors.mobile_pairing_error_response(error)
    headers: dict[str, str] = {}
    if error.code == "rate_limited":
        retry_after = "".join(ch for ch in error.message if ch.isdigit())
        if retry_after:
            headers["Retry-After"] = retry_after
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def _claim_view(claim: LoginQrClaimRecord | None) -> dict[str, Any] | None:
    """把登录二维码 claim 记录转换为轮询 DTO。"""
    if claim is None:
        return None
    return {
        "claim_id": claim.claim_id,
        "device_id": claim.device_id,
        "label": claim.label,
        "platform": claim.platform,
        "app_version": claim.app_version,
        "capabilities": claim.capabilities,
        "status": claim.status.value,
        "created_at": claim.created_at.isoformat(),
    }


def _session_view(
    session: LoginQrSessionRecord,
    claim: LoginQrClaimRecord | None,
) -> dict[str, Any]:
    """把登录二维码 session/claim 转换为轮询 DTO。"""
    return {
        "login_qr_id": session.login_qr_id,
        "status": session.status.value,
        "expires_at": session.expires_at.isoformat(),
        "claim": _claim_view(claim),
    }


def _web_session_url(origin: str, handoff_token: str) -> str:
    """生成 APK WebView 登录 URL。"""
    query = urlencode({"handoff_token": handoff_token})
    return f"{origin}/-/xspace/mobile/session/consume?{query}"


def _render_login_fallback_page(request: Request) -> HTMLResponse:
    """渲染系统相机 fallback 页面。"""
    query_params = request.query_params
    server = query_params.get("server", "")
    origin_mode = query_params.get("origin_mode", "")
    login_qr_id = query_params.get("login_qr_id", "")
    nonce = query_params.get("nonce", "")
    version = query_params.get("v", "1")
    query = urlencode(
        {
            "server": server,
            "origin_mode": origin_mode,
            "login_qr_id": login_qr_id,
            "nonce": nonce,
            "v": version,
            "purpose": "login",
        }
    )
    deeplink = f"xspace://login-kongming?{query}"
    copy_url = str(request.url)
    return HTMLResponse(
        f"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Open XSpace Login</title>
</head>
<body>
  <h1>打开 XSpace 登录</h1>
  <p><a href="{html.escape(deeplink)}">打开 XSpace Android</a></p>
  <h2>Deeplink</h2>
  <pre>{html.escape(deeplink)}</pre>
  <h2>Copy URL</h2>
  <pre>{html.escape(copy_url)}</pre>
</body>
</html>
        """.strip()
    )


@router.post("/api/xspace/mobile/login-qr-sessions")
async def create_login_qr_session(
    payload: CreateLoginQrSessionRequest,
    request: Request,
) -> JSONResponse:
    """登录页创建扫码登录 session。"""
    try:
        result = _manager(request).create_login_qr_session(
            protocol_version=payload.protocol_version,
            client=payload.client,
            requested_scopes=payload.requested_scopes,
            raw_server_origin=_configured_server_origin(request),
        )
    except errors.MobilePairingError as exc:
        logger.warning(
            "xspace_mobile.login_qr.create failed code=%s client=%s scopes=%s",
            errors.normalize_mobile_pairing_error_code(exc.code),
            payload.client,
            ",".join(payload.requested_scopes),
        )
        return _mobile_error(exc)
    return JSONResponse(content=result.model_dump(mode="json", exclude={"nonce"}))


@router.get("/api/xspace/mobile/login-qr-sessions/{login_qr_id}")
async def get_login_qr_session(
    login_qr_id: str,
    request: Request,
    x_kongming_login_qr_token: str = Header(alias=_LOGIN_QR_TOKEN_HEADER),
) -> JSONResponse:
    """登录页轮询扫码登录状态。"""
    try:
        session, claim = _manager(request).get_login_qr_view(
            login_qr_id,
            browser_token=x_kongming_login_qr_token,
        )
    except errors.MobilePairingError as exc:
        return _mobile_error(exc)
    return JSONResponse(content=_session_view(session, claim))


@router.post("/api/xspace/mobile/login-qr-sessions/{login_qr_id}/claim")
async def claim_login_qr_session(
    login_qr_id: str,
    payload: ClaimLoginQrSessionRequest,
    request: Request,
) -> JSONResponse:
    """Android claim 扫码登录 session。"""
    try:
        result = _manager(request).claim_login_qr(
            login_qr_id=login_qr_id,
            protocol_version=payload.protocol_version,
            nonce=payload.nonce,
            device=payload.device,
            capabilities=payload.capabilities,
        )
    except errors.MobilePairingError as exc:
        return _mobile_error(exc)
    return JSONResponse(
        content={
            "login_qr_id": result.login_qr_id,
            "claim_id": result.claim_id,
            "status": result.status.value,
            "poll_after_ms": result.poll_after_ms,
        }
    )


@router.post("/api/xspace/mobile/login-qr-sessions/{login_qr_id}/confirm")
async def confirm_login_qr_session(
    login_qr_id: str,
    payload: ConfirmLoginQrSessionRequest,
    request: Request,
) -> JSONResponse:
    """登录页使用密码确认扫码登录。"""
    try:
        result = await _auth_service(request).confirm_with_password(
            login_qr_id=login_qr_id,
            claim_id=payload.claim_id,
            browser_token=payload.browser_token,
            password=payload.password,
            password_hash=request.app.state.password_hash,
            client_ip=_client_ip(request),
        )
    except errors.MobilePairingError as exc:
        return _mobile_error(exc)
    return JSONResponse(
        content={"status": result.status.value, "poll_after_ms": result.poll_after_ms}
    )


@router.post("/api/xspace/mobile/login-qr-sessions/{login_qr_id}/exchange")
async def exchange_login_qr_session(
    login_qr_id: str,
    payload: ExchangeLoginQrSessionRequest,
    request: Request,
) -> JSONResponse:
    """Android 轮询并兑换扫码登录 device token。"""
    try:
        result = _manager(request).exchange_login_qr(
            login_qr_id=login_qr_id,
            claim_id=payload.claim_id,
            nonce=payload.nonce,
            device_id=payload.device_id,
        )
    except errors.MobilePairingError as exc:
        if errors.normalize_mobile_pairing_error_code(exc.code) == "approval_pending":
            return JSONResponse(
                status_code=202,
                content={"status": "pending_approval", "poll_after_ms": 1000},
            )
        return _mobile_error(exc)
    origin = result.server_origin.origin
    return JSONResponse(
        content={
            "status": "approved",
            "server_origin": result.server_origin.model_dump(mode="json"),
            "server": origin,
            "device_token": result.device_token,
            "token_type": "Bearer",
            "scopes": result.scopes,
            "device_id": result.device_id,
            "instance": {"alias": "Kongming", "url": origin},
            "web_session_url": _web_session_url(origin, result.handoff_token),
        }
    )


@router.get("/-/xspace/mobile/login")
async def mobile_login_fallback_page(request: Request) -> HTMLResponse:
    """返回公开扫码登录 fallback 页面。"""
    return _render_login_fallback_page(request)


__all__ = ["router"]
