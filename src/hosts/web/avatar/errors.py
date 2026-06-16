"""Avatar 消息模块稳定错误定义。

本脚本定义 Avatar message registry 的公开错误码和异常类型。关键流程是
Repository、Manager 或 Router 抛出 AvatarMessageError，Router 将其转换为
稳定 JSON 错误响应。关键函数职责：便捷构造函数为常见业务失败生成统一错误。
"""

from __future__ import annotations

from dataclasses import dataclass

HTTP_STATUS_BY_AVATAR_ERROR_CODE: dict[str, int] = {
    "avatar_forbidden": 403,
    "avatar_message_not_found": 404,
    "avatar_invalid_cursor": 400,
    "avatar_invalid_filter": 400,
    "avatar_capability_disabled": 501,
    "avatar_invalid_request": 400,
    "avatar_thread_not_found": 404,
    "avatar_invalid_thread": 400,
    "avatar_preset_required": 400,
    "avatar_runtime_refresh_failed": 503,
    "avatar_run_failed": 500,
}


@dataclass(slots=True)
class AvatarMessageError(Exception):
    """Avatar 消息模块错误父类。

    职责：承载公开错误码、可读消息和 HTTP status。
    关键输入：错误码、错误说明和可选 HTTP status。
    关键输出：可被 Router 捕获并序列化的异常实例。
    """

    code: str
    message: str
    status_code: int | None = None

    def __str__(self) -> str:
        """返回开发者可读错误字符串。"""
        return f"{self.code}: {self.message}"

    @property
    def http_status(self) -> int:
        """返回错误对应的 HTTP status。"""
        return self.status_code or HTTP_STATUS_BY_AVATAR_ERROR_CODE[self.code]


def avatar_error_body(error: AvatarMessageError) -> dict[str, dict[str, str]]:
    """生成 Avatar API 错误响应体。

    关键输入：AvatarMessageError。
    关键输出：``{"error": {"code", "message"}}`` 格式响应体。
    """
    return {"error": {"code": error.code, "message": error.message}}


def forbidden(message: str = "avatar scope required") -> AvatarMessageError:
    """构造权限不足错误。"""
    return AvatarMessageError("avatar_forbidden", message)


def message_not_found(message_id: str) -> AvatarMessageError:
    """构造消息缺失错误。"""
    return AvatarMessageError(
        "avatar_message_not_found",
        f"avatar message not found: {message_id}",
    )


def invalid_cursor(cursor: str) -> AvatarMessageError:
    """构造 cursor 非法错误。"""
    return AvatarMessageError("avatar_invalid_cursor", f"invalid avatar cursor: {cursor}")


def invalid_filter(message: str) -> AvatarMessageError:
    """构造过滤参数非法错误。"""
    return AvatarMessageError("avatar_invalid_filter", message)


def capability_disabled(capability: str) -> AvatarMessageError:
    """构造能力关闭错误。"""
    return AvatarMessageError(
        "avatar_capability_disabled",
        f"avatar capability disabled: {capability}",
    )


def invalid_request(message: str) -> AvatarMessageError:
    """构造请求参数错误。"""
    return AvatarMessageError("avatar_invalid_request", message)


def thread_not_found(thread_id: str) -> AvatarMessageError:
    """构造 Avatar thread 缺失错误。"""
    return AvatarMessageError("avatar_thread_not_found", f"avatar thread not found: {thread_id}")


def invalid_thread(thread_id: str) -> AvatarMessageError:
    """构造 Avatar thread 类型非法错误。"""
    return AvatarMessageError(
        "avatar_invalid_thread",
        f"avatar thread is not generic_chat: {thread_id}",
    )


def preset_required() -> AvatarMessageError:
    """构造首发创建缺少 preset 错误。"""
    return AvatarMessageError("avatar_preset_required", "presetId is required")


def runtime_refresh_failed(thread_id: str) -> AvatarMessageError:
    """构造 runtime preset 刷新失败错误。"""
    return AvatarMessageError(
        "avatar_runtime_refresh_failed",
        f"avatar runtime refresh failed: {thread_id}",
    )


def run_failed(message: str) -> AvatarMessageError:
    """构造 Avatar run 启动失败错误。"""
    return AvatarMessageError("avatar_run_failed", message)


__all__ = [
    "HTTP_STATUS_BY_AVATAR_ERROR_CODE",
    "AvatarMessageError",
    "avatar_error_body",
    "capability_disabled",
    "forbidden",
    "invalid_cursor",
    "invalid_filter",
    "invalid_request",
    "invalid_thread",
    "message_not_found",
    "preset_required",
    "run_failed",
    "runtime_refresh_failed",
    "thread_not_found",
]
