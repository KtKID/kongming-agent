"""认证路由：登录 / 登出 / 当前用户。

端点：

- ``POST /api/auth/login`` — body :class:`web.protocol.LoginRequest`
- ``POST /api/auth/logout`` — 无 body
- ``GET /api/auth/me`` — 已通过 :class:`web.auth.middleware.AuthMiddleware`，到这里必然认证

设计要点：

- 登录限流：``request.client.host`` 取 IP；不信任 ``X-Forwarded-For``（部署侧
  反代时由 nginx trust proxy 配置；v0.1.5 默认本地直连）。
- 登录成功必须同步 ``rate_limiter.record_success(ip)`` 清失败计数。
- ``/api/auth/login`` 是 :class:`web.auth.middleware.AuthMiddleware` 白名单（cookie 可缺）；
  但 :class:`web.auth.middleware.CSRFMiddleware` 不豁免 —— 浏览器统一在 axios interceptor
  加 ``X-Requested-With``。
- ``/api/auth/logout`` 同 login 白名单（cookie 已过期时也允许调）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response

from web.auth.middleware import (
    clear_session_cookie,
    issue_session_cookie,
    verify_session_cookie,
)
from web.auth.secrets import hash_password, verify_password
from web.errors import (
    AuthFailedError,
    NotAuthenticatedError,
    RateLimitedError,
)
from web.protocol import LoginRequest
from web.protocol.rest_models import ResetPasswordRequest
from web.rate_limit import LoginRateLimiter
from web.rate_limit import RateLimitedError as _RLError

if TYPE_CHECKING:
    from itsdangerous import URLSafeTimedSerializer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    """从 :class:`Request` 取客户端 IP。

    v0.1.5 不信任 ``X-Forwarded-For`` —— 反代部署应通过 uvicorn ``--proxy-headers``
    + ``--forwarded-allow-ips`` 让 ``request.client.host`` 已经是真实 IP。
    """
    if request.client is None:
        return "unknown"
    return request.client.host or "unknown"


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
) -> dict[str, bool]:
    """登录端点。

    流程：

    1. 限流 check（5 fails / 5min）
    2. bcrypt verify_password
    3. 失败 → record_failure + 401
    4. 成功 → record_success + 设 cookie + 200
    """
    ip = _client_ip(request)
    rate_limiter: LoginRateLimiter = request.app.state.rate_limiter
    try:
        await rate_limiter.check(ip)
    except _RLError as exc:
        raise RateLimitedError(
            "too many failed login attempts; please try later",
            retry_after_seconds=exc.retry_after_seconds,
        ) from exc

    password_hash: str = request.app.state.password_hash
    if not verify_password(body.password, password_hash):
        await rate_limiter.record_failure(ip)
        raise AuthFailedError("invalid credentials")

    await rate_limiter.record_success(ip)

    serializer: URLSafeTimedSerializer = request.app.state.serializer
    issue_session_cookie(
        response,
        serializer,
        request_scheme=request.url.scheme,
    )
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    """登出端点：清除会话 cookie；幂等。"""
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me")
async def me(request: Request) -> dict[str, object]:
    """当前用户信息（v0.1.5 单用户：``user_id="default"``）。

    实现注意：``/api/auth/me`` **不在** :class:`web.auth.middleware.AuthMiddleware` 白名单，
    middleware 已挡掉缺 cookie 的请求；这里只在 middleware 跳过（如 dev_mode
    / 测试场景）时做兜底校验。
    """
    payload = getattr(request.state, "session_payload", None)
    if payload is None:
        # 兜底：直接验 cookie（生产路径不会走到这里）
        serializer: URLSafeTimedSerializer = request.app.state.serializer
        raw = request.cookies.get("kongming_session")
        payload = verify_session_cookie(raw, serializer)
        if payload is None:
            raise NotAuthenticatedError("not authenticated")
    return {
        "authenticated": True,
        "user_id": payload.user_id,
        "iat": payload.iat,
    }


@router.post("/reset-password")
async def reset_password(
    body: ResetPasswordRequest,
    request: Request,
) -> dict[str, bool]:
    """重置密码端点。无需登录，本地单用户工具，直接重置。"""
    new_hash = hash_password(body.new_password)

    # 更新内存中的 hash
    request.app.state.password_hash = new_hash

    # 写回 app 当前使用的 kongming_home，保证启动读源与重置写源一致。
    from web.auth.secrets import PASSWORD_HASH_FILENAME, _ensure_web_dir, _write_secret_text

    home = request.app.state.kongming_home
    _ensure_web_dir(home)
    path = home / "web" / PASSWORD_HASH_FILENAME
    _write_secret_text(path, new_hash)

    logger.info("Password has been reset successfully")

    return {"ok": True}


__all__ = ["router"]
