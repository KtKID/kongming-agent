"""Web 通用错误响应（v0.1.5）。

- :class:`KongmingWebError` 父类 + 7 个子类
- :func:`kongming_error_handler` — FastAPI exception handler，把
  :class:`KongmingWebError` 翻译成 :class:`web.protocol.ErrorResponseDTO`

设计要点：

- ``error_code`` 复用 :data:`web.protocol.ErrorCode` 枚举（与 WS error 帧对齐）；
  v0.1.5 的 5 类不能完全覆盖 HTTP 错误（例如 401 / 403 / 404 都映射到 ``internal``），
  这是预期取舍 —— 前端按 HTTP status code 区分用户场景，``error_code`` 仅用于
  日志聚类。
- ``status_code`` 由各子类自己声明，调用方不传。
- ``message`` 是给开发者 / 高级用户看的；前端实际显示按 status code 翻译。

子类清单：

| 子类 | status | error_code | 用途 |
|---|---|---|---|
| AuthFailedError | 401 | internal | 密码错 |
| RateLimitedError | 429 | internal | 登录限流 |
| NotAuthenticatedError | 401 | internal | 未带合法 cookie |
| CSRFGuardError | 403 | internal | 缺 X-Requested-With |
| ThreadNotFoundError | 404 | internal | thread_id 不存在 |
| InvalidThreadIdError | 422 | internal | thread_id 正则不匹配 |
| InvalidRequestError | 422 | internal | 请求字段不合法 |
| WebAuthNotConfiguredError | 500 | internal | 启动时缺 password |
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from starlette.responses import JSONResponse

from hosts.web.protocol import ErrorResponseDTO

if TYPE_CHECKING:
    from starlette.requests import Request

    from hosts.web.protocol import ErrorCode

from infrastructure.config.model_provider_catalog import ModelProviderCatalogError

logger = logging.getLogger(__name__)


class KongmingWebError(Exception):
    """Web 层统一错误父类。

    Attributes:
        error_code: :data:`web.protocol.ErrorCode` 枚举值。
        message: 错误描述（给开发者）。
        status_code: HTTP 状态码。
    """

    error_code: ErrorCode = "internal"
    status_code: int = 400

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


class AuthFailedError(KongmingWebError):
    """登录密码错误。"""

    status_code = 401


class RateLimitedError(KongmingWebError):
    """登录失败次数超阈值，IP 被锁。

    Attributes:
        retry_after_seconds: ``Retry-After`` HTTP 头值。
    """

    status_code = 429

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class NotAuthenticatedError(KongmingWebError):
    """缺合法 cookie / cookie 过期。"""

    status_code = 401


class CSRFGuardError(KongmingWebError):
    """缺 ``X-Requested-With: XMLHttpRequest`` 头。"""

    status_code = 403


class ThreadNotFoundError(KongmingWebError):
    """thread_id 不存在。"""

    status_code = 404


class InvalidThreadIdError(KongmingWebError):
    """thread_id 不匹配 ``^thread-[a-f0-9]{12}$``。"""

    status_code = 422


class InvalidRequestError(KongmingWebError):
    """请求字段不合法。"""

    status_code = 422


class WebAuthNotConfiguredError(KongmingWebError):
    """启动时缺密码 hash + env。"""

    status_code = 500


class ModelCatalogWebError(KongmingWebError):
    """把 catalog typed error 映射到统一 REST error envelope。"""

    status_code = 422

    def __init__(self, error: ModelProviderCatalogError) -> None:
        super().__init__(str(error))
        self.error_code = cast("ErrorCode", error.code.value)
        self.details = dict(error.details)


async def kongming_error_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """FastAPI exception handler。

    把 :class:`KongmingWebError` 翻译成统一 JSON 响应；同时为
    :class:`RateLimitedError` 加 ``Retry-After`` HTTP 头。

    非 :class:`KongmingWebError` 也允许直接传入（便于 ``@app.exception_handler``
    注册时 starlette 把所有 Exception 都路由到这里），那种情况降级为 500。
    """
    del request  # 未使用
    if not isinstance(exc, KongmingWebError):
        # 防御兜底：未知异常归 500
        logger.exception("unhandled exception in kongming_error_handler", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=ErrorResponseDTO(
                error_code="internal",
                message=f"internal error: {type(exc).__name__}",
            ).model_dump(),
        )

    headers: dict[str, str] = {}
    if isinstance(exc, RateLimitedError):
        headers["Retry-After"] = str(exc.retry_after_seconds)

    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponseDTO(
            error_code=exc.error_code,
            message=exc.message,
            details=getattr(exc, "details", None),
        ).model_dump(),
        headers=headers,
    )


__all__ = [
    "AuthFailedError",
    "CSRFGuardError",
    "InvalidRequestError",
    "InvalidThreadIdError",
    "KongmingWebError",
    "ModelCatalogWebError",
    "NotAuthenticatedError",
    "RateLimitedError",
    "ThreadNotFoundError",
    "WebAuthNotConfiguredError",
    "kongming_error_handler",
]
