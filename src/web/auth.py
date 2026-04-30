"""Web 鉴权中间件 + cookie 签名（v0.1.5）。

提供：

- :func:`make_serializer` — itsdangerous URLSafeTimedSerializer factory
- :func:`issue_session_cookie` / :func:`clear_session_cookie` — 在 :class:`Response`
  上设置 / 清除会话 cookie
- :func:`verify_session_cookie` — 校验签名 + 过期；失败返回 ``None`` 不抛
- :class:`AuthMiddleware` — 强制要求受保护 path 带合法 cookie
- :class:`CSRFMiddleware` — 强制 mutating 方法带 ``X-Requested-With: XMLHttpRequest``

设计要点：

- Cookie 名固定 ``kongming_session``。HttpOnly + SameSite=Lax 必加；Secure 按
  ``request.url.scheme`` 自动 + ``cfg.web.trust_proxy_https`` 兜底（v0.1.5 仅
  按 scheme，不读 cfg.web；调用方需要时显式 force_secure=True 即可）。
- ``verify_session_cookie`` 失败一律返回 ``None``：不区分 BadSignature /
  SignatureExpired / 缺字段，统一交给上层"未登录"路径处理。
- AuthMiddleware **不**校验 WS / 静态 path —— WS 走 ws.py 自身的 cookie 验证；
  静态 path 走 static.py 的 SPA fallback；这里只挡 ``/api/*``。
- CSRFMiddleware **不**豁免任何 mutating path：包括 ``/api/auth/login``——
  浏览器统一在 axios interceptor 加 X-Requested-With。CSRF 在 401 之前执行
  顺序：CSRF → Auth → router（先注册的最外层）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from web.protocol import ErrorResponseDTO

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SESSION_COOKIE_NAME = "kongming_session"
"""签名会话 cookie 名（固定不可配）。"""

DEFAULT_SESSION_MAX_AGE_SECONDS = 24 * 60 * 60
"""默认 24 小时。"""

SESSION_SALT = "kongming.web.session.v1"
"""itsdangerous salt；版本号在 salt 里，未来 bump 时旧 cookie 自动失效。"""

CSRF_HEADER_NAME = "X-Requested-With"
CSRF_HEADER_VALUE = "XMLHttpRequest"
CSRF_PROTECTED_METHODS = frozenset({"POST", "PATCH", "DELETE", "PUT"})


# ---------------------------------------------------------------------------
# session payload
# ---------------------------------------------------------------------------


class SessionTokenPayload(BaseModel):
    """会话 cookie 内承载的最小载荷（v0.1.5）。

    Attributes:
        user_id: v0.1.5 单用户场景固定 ``"default"``。
        iat: 签发时间（unix 秒）；itsdangerous 自身已带过期校验，这里冗余
            存一份便于审计 / debug。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = "default"
    iat: int


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def make_serializer(secret: bytes) -> URLSafeTimedSerializer:
    """构造 itsdangerous URLSafeTimedSerializer。

    Args:
        secret: 32 字节裸 secret（来自 :func:`web.auth_secrets.load_or_init_session_secret`）。

    Returns:
        :class:`URLSafeTimedSerializer` 实例；签名 / 校验都通过它。
    """
    return URLSafeTimedSerializer(secret_key=secret, salt=SESSION_SALT)


def issue_session_cookie(
    response: Response,
    serializer: URLSafeTimedSerializer,
    *,
    request_scheme: str,
    max_age_seconds: int = DEFAULT_SESSION_MAX_AGE_SECONDS,
    user_id: str = "default",
    iat: int | None = None,
    force_secure: bool = False,
) -> None:
    """在 :class:`Response` 上设置签名会话 cookie。

    Args:
        response: starlette Response 对象。
        serializer: :func:`make_serializer` 产物。
        request_scheme: ``request.url.scheme``（用于决定 Secure 属性）。
        max_age_seconds: cookie max-age；默认 24h。
        user_id: 写入 payload；v0.1.5 固定 ``"default"``。
        iat: 签发时间；默认 0（itsdangerous 自带时间戳，iat 仅用于审计）。
        force_secure: 反代部署 https 但 ``request.url.scheme=http`` 时强制
            Secure；v0.1.5 默认 False，由调用方显式开启。
    """
    import time as _time

    payload = SessionTokenPayload(
        user_id=user_id,
        iat=iat if iat is not None else int(_time.time()),
    )
    signed = serializer.dumps(payload.model_dump())
    secure = force_secure or (request_scheme.lower() == "https")
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=signed,
        max_age=max_age_seconds,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """清除会话 cookie（设置 max-age=0）。"""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
    )


def verify_session_cookie(
    raw: str | None,
    serializer: URLSafeTimedSerializer,
    *,
    max_age_seconds: int = DEFAULT_SESSION_MAX_AGE_SECONDS,
) -> SessionTokenPayload | None:
    """校验签名 + 过期。失败一律返回 ``None``，不抛。

    Args:
        raw: 原始 cookie 值（未签名解码）。``None`` / 空字符串直接返回 None。
        serializer: :func:`make_serializer` 产物。
        max_age_seconds: 与 issue 时一致；默认 24h。

    Returns:
        合法 :class:`SessionTokenPayload`；签名 / 过期 / 字段不匹配均返回 None。
    """
    if not raw:
        return None
    try:
        data = serializer.loads(raw, max_age=max_age_seconds)
    except SignatureExpired:
        return None
    except BadSignature:
        return None
    except Exception as exc:
        logger.warning("verify_session_cookie unexpected error: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    try:
        return SessionTokenPayload.model_validate(data)
    except Exception as exc:
        logger.warning("verify_session_cookie payload validation failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Middlewares
# ---------------------------------------------------------------------------


def _is_path_allowlisted(path: str, *, allow_docs: bool) -> bool:
    """AuthMiddleware 白名单判断。

    白名单：

    - ``/api/auth/login``：登录入口本身不能要求 cookie
    - ``/api/auth/logout``：哪怕 cookie 已过期也允许 client 显式登出（清 cookie）
    - 静态 / SPA 路径：``/``、``/assets/...``、``/index.html`` 等所有非 ``/api/`` 非 ``/ws/`` 的
    - WS 路径：HTTP middleware 不处理 WS upgrade 路径，但 starlette 仍会送进来 GET 请求；
      这里挡掉 ``/ws/`` 让 WS endpoint 自己鉴权
    - dev 模式 docs：``/docs``、``/redoc``、``/openapi.json``
    """
    if path == "/api/auth/login":
        return True
    if path == "/api/auth/logout":
        return True
    if path == "/api/auth/reset-password":
        return True
    if path.startswith("/ws/"):
        # WS 自身鉴权；HTTP 路径前缀 /ws/ 也放行（其它非 WS 协议的 GET 落到 404）
        return True
    if not path.startswith("/api/"):
        # 静态 / SPA fallback
        return True
    return bool(allow_docs and path in ("/docs", "/redoc", "/openapi.json"))


class AuthMiddleware(BaseHTTPMiddleware):
    """要求受保护 ``/api/*`` 路径带合法 :data:`SESSION_COOKIE_NAME`。

    顺序：``CSRFMiddleware → AuthMiddleware → router``（CSRF 先注册即最外层）。
    依赖：``app.state.serializer``（由 :func:`web.app.create_app` 装配）。

    Attributes:
        allow_docs: 是否放行 ``/docs`` / ``/openapi.json``（dev_mode 时 True）。
    """

    def __init__(self, app: ASGIApp, *, allow_docs: bool = False) -> None:
        super().__init__(app)
        self._allow_docs = allow_docs

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if _is_path_allowlisted(request.url.path, allow_docs=self._allow_docs):
            return await call_next(request)
        # 受保护：必须验过 cookie
        serializer: URLSafeTimedSerializer | None = getattr(request.app.state, "serializer", None)
        if serializer is None:
            # 装配缺失；当作 500
            return JSONResponse(
                status_code=500,
                content=ErrorResponseDTO(
                    error_code="internal",
                    message="auth not configured (serializer missing)",
                ).model_dump(),
            )
        raw = request.cookies.get(SESSION_COOKIE_NAME)
        payload = verify_session_cookie(raw, serializer)
        if payload is None:
            return JSONResponse(
                status_code=401,
                content=ErrorResponseDTO(
                    error_code="internal",
                    message="not authenticated",
                ).model_dump(),
            )
        # 写到 request.state 供 router 用（v0.1.5 单用户其实不需要）
        request.state.session_payload = payload
        return await call_next(request)


class CSRFMiddleware(BaseHTTPMiddleware):
    """要求 mutating HTTP 方法带 ``X-Requested-With: XMLHttpRequest`` 头。

    防御策略：SameSite=Lax cookie + 自定义 header（浏览器跨域时不会自动带）。
    顺序：CSRF 必须在 Auth 前面（先注册 = 最外层），让 401 也走过 CSRF。

    豁免：

    - **safe methods**：GET / HEAD / OPTIONS / TRACE 不查 header
    - **WS 升级**：starlette WS handshake 走 GET，自然豁免
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        method = request.method.upper()
        if method in CSRF_PROTECTED_METHODS:
            header_val = request.headers.get(CSRF_HEADER_NAME)
            if header_val != CSRF_HEADER_VALUE:
                return JSONResponse(
                    status_code=403,
                    content=ErrorResponseDTO(
                        error_code="internal",
                        message=f"CSRF guard: {CSRF_HEADER_NAME} required",
                    ).model_dump(),
                )
        return await call_next(request)


__all__ = [
    "CSRF_HEADER_NAME",
    "CSRF_HEADER_VALUE",
    "CSRF_PROTECTED_METHODS",
    "DEFAULT_SESSION_MAX_AGE_SECONDS",
    "SESSION_COOKIE_NAME",
    "SESSION_SALT",
    "AuthMiddleware",
    "CSRFMiddleware",
    "SessionTokenPayload",
    "clear_session_cookie",
    "issue_session_cookie",
    "make_serializer",
    "verify_session_cookie",
]
