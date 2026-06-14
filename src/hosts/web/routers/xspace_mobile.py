"""XSpace Mobile 配对 HTTP/Web 路由。

本脚本把 ``MobilePairingManager`` 和 ``MobileDeviceTokenService`` 暴露为
FastAPI 路由，作用是服务 XSpace Android 扫码 claim、exchange、session handoff，
以及 Kongming Web 的连接手机页面。关键流程是 Web 创建 pairing session，Android
匿名 claim 并轮询 exchange，Web 批准后 Android 换取 device token 与 handoff URL，
最后 consume route 设置 ``kongming_session`` cookie 并跳回首页。关键函数职责：
API handler 只做 DTO 转换、鉴权边界和错误响应，业务状态机统一委托 Manager。
"""

from __future__ import annotations

import html
import logging
from typing import Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from hosts.web.auth.middleware import (
    SESSION_COOKIE_NAME,
    issue_session_cookie,
    verify_session_cookie,
)
from hosts.web.xspace_mobile import errors
from hosts.web.xspace_mobile.manager import MobilePairingManager
from hosts.web.xspace_mobile.models import (
    MobileDeviceDescriptor,
    PairingClaimRecord,
    PairingSessionRecord,
)
from hosts.web.xspace_mobile.token_service import MobileDeviceTokenService

router = APIRouter()
logger = logging.getLogger(__name__)

_DEFAULT_SCOPES = ["webview", "thread.read", "approval.resolve"]


class CreatePairingSessionRequest(BaseModel):
    """创建 pairing session 请求 DTO。"""

    model_config = ConfigDict(extra="forbid")

    protocol_version: str = "1"
    client: str = "kongming-web"
    requested_scopes: list[str] = Field(default_factory=lambda: list(_DEFAULT_SCOPES))


class ClaimPairingSessionRequest(BaseModel):
    """Android claim 请求 DTO。"""

    model_config = ConfigDict(extra="forbid")

    protocol_version: str
    nonce: str
    device: MobileDeviceDescriptor
    capabilities: dict[str, bool] = Field(default_factory=dict)


class ApprovePairingSessionRequest(BaseModel):
    """桌面批准或拒绝请求 DTO。"""

    model_config = ConfigDict(extra="forbid")

    claim_id: str
    approved: bool = True


class ExchangePairingSessionRequest(BaseModel):
    """Android exchange 请求 DTO。"""

    model_config = ConfigDict(extra="forbid")

    protocol_version: str
    claim_id: str
    nonce: str
    device_id: str


def _manager(request: Request) -> MobilePairingManager:
    """读取 app.state 中的移动配对 Manager。

    关键输入：FastAPI request。
    关键输出：已由 app factory 装配的 Manager。
    """
    manager = getattr(request.app.state, "xspace_mobile_pairing_manager", None)
    if not isinstance(manager, MobilePairingManager):
        raise errors.invalid_request("xspace mobile pairing manager is not configured")
    return manager


def _token_service(request: Request) -> MobileDeviceTokenService:
    """读取 app.state 中的移动 token service。

    关键输入：FastAPI request。
    关键输出：已由 app factory 装配的 TokenService。
    """
    service = getattr(request.app.state, "xspace_mobile_token_service", None)
    if not isinstance(service, MobileDeviceTokenService):
        raise errors.invalid_request("xspace mobile token service is not configured")
    return service


def _origin(request: Request) -> str:
    """生成当前请求的 server origin。

    关键输入：FastAPI request。
    关键输出：``scheme://host`` 形式 origin。
    """
    return str(request.base_url).rstrip("/")


def _server_origin(request: Request) -> str:
    """生成移动配对对外 server origin。

    关键输入：FastAPI request 与 app.state.config.web.public_origin。
    关键输出：手机可访问的 ``scheme://host[:port]`` origin。
    """
    cfg = getattr(request.app.state, "config", None)
    public_origin = getattr(getattr(cfg, "web", None), "public_origin", None)
    if isinstance(public_origin, str) and public_origin.strip():
        return public_origin.strip().rstrip("/")
    return _origin(request)


def _web_session_url(request: Request, handoff_token: str) -> str:
    """生成 Android WebView 登录 URL。

    关键输入：当前请求和 handoff token 明文。
    关键输出：可被 Android 打开的 consume URL。
    """
    query = urlencode({"handoff_token": handoff_token})
    return f"{_server_origin(request)}/-/xspace/mobile/session/consume?{query}"


def _mobile_error(error: errors.MobilePairingError) -> JSONResponse:
    """把移动配对业务错误转换为 JSONResponse。

    关键输入：Manager/TokenService 抛出的 ``MobilePairingError``。
    关键输出：带稳定错误结构和 HTTP 状态码的响应。
    """
    status_code, body = errors.mobile_pairing_error_response(error)
    return JSONResponse(status_code=status_code, content=body)


def _bearer_token(request: Request) -> str:
    """解析 Authorization Bearer token。

    关键输入：HTTP request headers。
    关键输出：device token 明文；缺失或格式错误时抛稳定错误。
    """
    raw = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not raw.startswith(prefix):
        raise errors.invalid_token("authorization bearer token required")
    token = raw[len(prefix) :].strip()
    if not token:
        raise errors.invalid_token("authorization bearer token required")
    return token


def _require_logged_in(request: Request) -> Response | None:
    """校验非 API 页面路由的 Web cookie 登录态。

    关键输入：FastAPI request。
    关键输出：登录态有效时返回 ``None``，失效时返回 401 HTML 响应。
    """
    serializer = getattr(request.app.state, "serializer", None)
    if serializer is None:
        return HTMLResponse("auth not configured", status_code=500)
    payload = verify_session_cookie(request.cookies.get(SESSION_COOKIE_NAME), serializer)
    if payload is None:
        return HTMLResponse("not authenticated", status_code=401)
    request.state.session_payload = payload
    return None


def _claim_view(claim: PairingClaimRecord | None) -> dict[str, Any] | None:
    """把 claim 记录转换为页面轮询 DTO。

    关键输入：claim record 或空值。
    关键输出：前端可展示的 claim 字典。
    """
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


def _pairing_view(
    session: PairingSessionRecord,
    claim: PairingClaimRecord | None,
) -> dict[str, Any]:
    """把 pairing session 和 claim 转换为页面轮询 DTO。

    关键输入：session record 和可选 claim record。
    关键输出：Web connect 页可展示的 JSON 字典。
    """
    return {
        "pairing_id": session.pairing_id,
        "status": session.status.value,
        "expires_at": session.expires_at.isoformat(),
        "claim": _claim_view(claim),
    }


def _render_connect_page() -> HTMLResponse:
    """渲染 P0 连接手机页面。

    关键输入：无。
    关键输出：包含创建 session、展示 QR 文本、轮询 claim、批准/拒绝按钮的 HTML。
    """
    return HTMLResponse(
        """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>XSpace Mobile Pairing</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 760px; margin: 40px auto; }
    pre { white-space: pre-wrap; word-break: break-all; background: #f4f4f4; padding: 12px; }
    button { margin-right: 8px; padding: 8px 12px; }
    .muted { color: #666; }
  </style>
</head>
<body>
  <h1>连接 XSpace Android</h1>
  <p id="status" class="muted">正在创建配对会话...</p>
  <h2>扫码内容</h2>
  <pre id="qr"></pre>
  <h2>复制链接</h2>
  <pre id="copy"></pre>
  <h2>待授权设备</h2>
  <pre id="claim">等待手机扫码...</pre>
  <button id="approve" disabled>批准</button>
  <button id="deny" disabled>拒绝</button>
  <script>
    const csrfHeaders = { "X-Requested-With": "XMLHttpRequest" };
    let pairingId = null;
    let claimId = null;

    async function api(path, options = {}) {
      const headers = Object.assign(
        { "Content-Type": "application/json" },
        csrfHeaders,
        options.headers || {},
      );
      const response = await fetch(path, Object.assign(
        { credentials: "same-origin", headers },
        options,
      ));
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(JSON.stringify(data));
      }
      return data;
    }

    async function createSession() {
      const data = await api("/api/xspace/mobile/pairing-sessions", {
        method: "POST",
        body: JSON.stringify({
          protocol_version: "1",
          client: "kongming-web",
          requested_scopes: ["webview", "thread.read", "approval.resolve"],
        }),
      });
      pairingId = data.pairing_id;
      document.getElementById("status").textContent = `pairing ${pairingId} 已创建`;
      document.getElementById("qr").textContent = data.qr_payload;
      document.getElementById("copy").textContent = data.copy_url;
      pollClaim();
    }

    async function pollClaim() {
      if (!pairingId) return;
      try {
        const data = await api(`/api/xspace/mobile/pairing-sessions/${pairingId}`, {
          method: "GET",
        });
        if (data.claim) {
          claimId = data.claim.claim_id;
          document.getElementById("claim").textContent = JSON.stringify(data.claim, null, 2);
          document.getElementById("approve").disabled = false;
          document.getElementById("deny").disabled = false;
          return;
        }
        document.getElementById("claim").textContent = "等待手机扫码...";
      } catch (error) {
        document.getElementById("claim").textContent = String(error);
      }
      setTimeout(pollClaim, 1000);
    }

    async function approve(approved) {
      if (!pairingId || !claimId) return;
      const data = await api(`/api/xspace/mobile/pairing-sessions/${pairingId}/approve`, {
        method: "POST",
        body: JSON.stringify({ claim_id: claimId, approved }),
      });
      document.getElementById("status").textContent = `状态：${data.status}`;
      document.getElementById("approve").disabled = true;
      document.getElementById("deny").disabled = true;
    }

    document.getElementById("approve").onclick = () => approve(true);
    document.getElementById("deny").onclick = () => approve(false);
    createSession().catch((error) => {
      document.getElementById("status").textContent = String(error);
    });
  </script>
</body>
</html>
        """.strip()
    )


def _render_pair_page(request: Request) -> HTMLResponse:
    """渲染系统相机 fallback 页面。

    关键输入：带 pairing_id/nonce/v 查询参数的 request。
    关键输出：展示 deeplink 和复制 URL 的公开 HTML。
    """
    origin = _server_origin(request)
    pairing_id = request.query_params.get("pairing_id", "")
    nonce = request.query_params.get("nonce", "")
    version = request.query_params.get("v", "1")
    query = urlencode(
        {
            "v": version,
            "server": origin,
            "pairing_id": pairing_id,
            "nonce": nonce,
        }
    )
    deeplink = f"xspace://pair-kongming?{query}"
    copy_url = str(request.url)
    return HTMLResponse(
        f"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Open XSpace</title>
</head>
<body>
  <h1>打开 XSpace</h1>
  <p><a href="{html.escape(deeplink)}">打开 XSpace Android</a></p>
  <h2>Deeplink</h2>
  <pre>{html.escape(deeplink)}</pre>
  <h2>Copy URL</h2>
  <pre>{html.escape(copy_url)}</pre>
</body>
</html>
        """.strip()
    )


@router.get("/api/xspace/mobile/capabilities")
async def get_capabilities() -> dict[str, Any]:
    """返回 XSpace mobile pairing 能力探测信息。"""
    return {
        "protocol_versions": ["1"],
        "mobile_pairing": True,
        "requires_desktop_approval": True,
        "session_handoff": True,
    }


@router.post("/api/xspace/mobile/pairing-sessions")
async def create_pairing_session(
    payload: CreatePairingSessionRequest,
    request: Request,
) -> JSONResponse:
    """创建配对会话。"""
    origin = _server_origin(request)
    try:
        result = _manager(request).create_pairing_session(
            protocol_version=payload.protocol_version,
            client=payload.client,
            requested_scopes=payload.requested_scopes,
            server_origin=origin,
        )
    except errors.MobilePairingError as exc:
        logger.warning(
            "xspace_mobile.create_pairing_session failed code=%s origin=%s client=%s scopes=%s",
            errors.normalize_mobile_pairing_error_code(exc.code),
            origin,
            payload.client,
            ",".join(payload.requested_scopes),
        )
        return _mobile_error(exc)
    logger.info(
        "xspace_mobile.create_pairing_session success pairing_id=%s origin=%s client=%s scopes=%s expires_at=%s",
        result.pairing_id,
        origin,
        payload.client,
        ",".join(payload.requested_scopes),
        result.expires_at.isoformat(),
    )
    return JSONResponse(content=result.model_dump(mode="json", exclude={"nonce"}))


@router.get("/api/xspace/mobile/pairing-sessions/{pairing_id}")
async def get_pairing_session(pairing_id: str, request: Request) -> JSONResponse:
    """返回 Web connect 页轮询用 pairing 状态。"""
    try:
        session, claim = _manager(request).get_pairing_view(pairing_id)
    except errors.MobilePairingError as exc:
        return _mobile_error(exc)
    return JSONResponse(content=_pairing_view(session, claim))


@router.post("/api/xspace/mobile/pairing-sessions/{pairing_id}/claim")
async def claim_pairing_session(
    pairing_id: str,
    payload: ClaimPairingSessionRequest,
    request: Request,
) -> JSONResponse:
    """Android claim 配对会话。"""
    try:
        result = _manager(request).claim_session(
            pairing_id=pairing_id,
            protocol_version=payload.protocol_version,
            nonce=payload.nonce,
            device=payload.device,
            capabilities=payload.capabilities,
        )
    except errors.MobilePairingError as exc:
        return _mobile_error(exc)
    return JSONResponse(
        content={
            "pairing_id": result.pairing_id,
            "claim_id": result.claim_id,
            "status": result.status.value,
            "poll_after_ms": result.poll_after_ms,
        }
    )


@router.post("/api/xspace/mobile/pairing-sessions/{pairing_id}/approve")
async def approve_pairing_session(
    pairing_id: str,
    payload: ApprovePairingSessionRequest,
    request: Request,
) -> JSONResponse:
    """桌面批准或拒绝 claim。"""
    try:
        result = _manager(request).approve_claim(
            pairing_id=pairing_id,
            claim_id=payload.claim_id,
            approved=payload.approved,
        )
    except errors.MobilePairingError as exc:
        return _mobile_error(exc)
    status: Literal["approved", "denied"] = "approved" if result.approved else "denied"
    return JSONResponse(content={"status": status})


@router.post("/api/xspace/mobile/pairing-sessions/{pairing_id}/exchange")
async def exchange_pairing_session(
    pairing_id: str,
    payload: ExchangePairingSessionRequest,
    request: Request,
) -> JSONResponse:
    """Android 轮询并兑换 device token。"""
    try:
        result = _manager(request).exchange_pairing(
            pairing_id=pairing_id,
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
    server = _server_origin(request)
    session, _claim = _manager(request).get_pairing_view(pairing_id)
    return JSONResponse(
        content={
            "status": "approved",
            "server": server,
            "device_token": result.device_token,
            "token_type": "Bearer",
            "scopes": session.requested_scopes,
            "device_id": result.device_id,
            "instance": {"alias": "Kongming", "url": server},
            "web_session_url": _web_session_url(request, result.handoff_token),
        }
    )


@router.post("/api/xspace/mobile/session-handoff")
async def create_session_handoff(request: Request) -> JSONResponse:
    """用 Android device token 换取一次性 WebView handoff URL。"""
    try:
        device_token = _bearer_token(request)
        result = _token_service(request).issue_handoff_for_device_token(
            device_token,
            user_id="mobile-device",
        )
    except errors.MobilePairingError as exc:
        return _mobile_error(exc)
    return JSONResponse(
        content={
            "web_session_url": _web_session_url(request, result.handoff_token),
            "expires_at": result.expires_at.isoformat(),
        }
    )


@router.get("/api/xspace/mobile/devices")
async def list_mobile_devices(request: Request) -> dict[str, Any]:
    """列出当前已授权移动设备。"""
    devices = _manager(request).list_devices()
    return {
        "devices": [
            {
                "device_id": device.device_id,
                "label": device.label,
                "platform": device.platform,
                "app_version": device.app_version,
                "scopes": device.scopes,
                "last_seen_at": device.last_seen_at.isoformat()
                if device.last_seen_at is not None
                else None,
                "created_at": device.created_at.isoformat(),
            }
            for device in devices
        ]
    }


@router.delete("/api/xspace/mobile/devices/{device_id}")
async def revoke_mobile_device(device_id: str, request: Request) -> Response:
    """吊销移动设备。"""
    try:
        _manager(request).revoke_device(device_id)
    except errors.MobilePairingError as exc:
        return _mobile_error(exc)
    return Response(status_code=204)


@router.get("/-/xspace/mobile/connect")
async def mobile_connect_page(request: Request) -> Response:
    """返回已登录桌面用户使用的连接手机页面。"""
    auth_response = _require_logged_in(request)
    if auth_response is not None:
        return auth_response
    return _render_connect_page()


@router.get("/-/xspace/mobile/pair")
async def mobile_pair_page(request: Request) -> HTMLResponse:
    """返回公开复制链接 fallback 页面。"""
    return _render_pair_page(request)


@router.get("/-/xspace/mobile/session/consume")
async def consume_mobile_session(request: Request, handoff_token: str) -> Response:
    """消费 handoff token，设置 Web session cookie 并跳转首页。"""
    try:
        context = _token_service(request).consume_handoff_token(handoff_token)
    except errors.MobilePairingError as exc:
        return _mobile_error(exc)
    response = RedirectResponse("/", status_code=302)
    serializer = getattr(request.app.state, "serializer", None)
    if serializer is None:
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal", "message": "auth not configured"}},
        )
    issue_session_cookie(
        response,
        serializer,
        request_scheme=request.url.scheme,
        user_id=context.user_id,
    )
    return response
