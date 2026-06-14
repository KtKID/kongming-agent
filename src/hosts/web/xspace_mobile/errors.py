"""XSpace Mobile 配对错误定义。

本脚本定义移动扫码配对后端核心使用的公开错误类型，作用是把
Manager、Repository、TokenService 的失败场景统一为稳定 code、message 和
retryable 语义。关键流程是业务层抛出 :class:`MobilePairingError`，后续 Router
按 ``code`` 翻译 HTTP 状态码。关键类职责：``MobilePairingError`` 承载错误合同，
便捷构造函数为各状态机分支生成明确错误。
"""

from __future__ import annotations

from dataclasses import dataclass

PUBLIC_ERROR_CODE_BY_INTERNAL_CODE: dict[str, str] = {
    "invalid_request": "invalid_pairing_payload",
    "claim_not_found": "pairing_not_found",
    "handoff_expired": "token_expired",
    "handoff_consumed": "reauth_required",
    "invalid_token": "reauth_required",
}

HTTP_STATUS_BY_PUBLIC_ERROR_CODE: dict[str, int] = {
    "unsupported_protocol": 400,
    "invalid_pairing_payload": 400,
    "pairing_not_found": 404,
    "pairing_expired": 410,
    "nonce_mismatch": 403,
    "pairing_already_claimed": 409,
    "approval_pending": 202,
    "approval_denied": 403,
    "device_revoked": 401,
    "token_expired": 401,
    "reauth_required": 401,
}


@dataclass(slots=True)
class MobilePairingError(Exception):
    """移动配对错误父类。

    职责：承载稳定错误码、开发者可读消息和客户端是否适合重试。
    关键输入：``code`` 为协议错误码，``message`` 为错误说明，``retryable`` 为重试建议。
    关键输出：可被 Manager/Router 捕获的异常实例。
    """

    code: str
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        """返回开发者可读错误文本。

        关键输入：异常实例自身的 ``code`` 和 ``message``。
        关键输出：包含错误码前缀的字符串。
        """
        return f"{self.code}: {self.message}"


def normalize_mobile_pairing_error_code(code: str) -> str:
    """归一化移动端公开错误码。

    关键输入：Manager、Repository 或 TokenService 抛出的内部错误码。
    关键输出：Android client 可稳定解析的公开错误码。
    """
    return PUBLIC_ERROR_CODE_BY_INTERNAL_CODE.get(code, code)


def mobile_pairing_http_status(code_or_error: str | MobilePairingError) -> int:
    """查询移动配对 HTTP 状态码。

    关键输入：内部错误码、公开错误码或 ``MobilePairingError``。
    关键输出：qdev 错误合同定义的 HTTP status。
    """
    code = code_or_error.code if isinstance(code_or_error, MobilePairingError) else code_or_error
    public_code = normalize_mobile_pairing_error_code(code)
    return HTTP_STATUS_BY_PUBLIC_ERROR_CODE[public_code]


def mobile_pairing_error_body(
    error: MobilePairingError,
) -> dict[str, dict[str, str | bool]]:
    """生成 Android client 解析的错误响应体。

    关键输入：业务层抛出的 ``MobilePairingError``。
    关键输出：``body.error.code/message/retryable`` 结构。
    """
    return {
        "error": {
            "code": normalize_mobile_pairing_error_code(error.code),
            "message": error.message,
            "retryable": error.retryable,
        }
    }


def mobile_pairing_error_response(
    error: MobilePairingError,
) -> tuple[int, dict[str, dict[str, str | bool]]]:
    """生成 Router 可直接消费的 HTTP 错误合同。

    关键输入：业务层抛出的 ``MobilePairingError``。
    关键输出：HTTP status 和 Android client 错误响应体。
    """
    return mobile_pairing_http_status(error), mobile_pairing_error_body(error)


def unsupported_protocol(protocol_version: str) -> MobilePairingError:
    """构造协议版本错误。

    关键输入：客户端提交的协议版本。
    关键输出：``unsupported_protocol`` 错误。
    """
    return MobilePairingError(
        "unsupported_protocol",
        f"unsupported mobile pairing protocol: {protocol_version}",
    )


def pairing_not_found(pairing_id: str) -> MobilePairingError:
    """构造配对会话缺失错误。

    关键输入：查找失败的 ``pairing_id``。
    关键输出：``pairing_not_found`` 错误。
    """
    return MobilePairingError("pairing_not_found", f"pairing not found: {pairing_id}")


def claim_not_found(pairing_id: str, claim_id: str) -> MobilePairingError:
    """构造 claim 缺失错误。

    关键输入：查找失败的 ``pairing_id`` 和 ``claim_id``。
    关键输出：``claim_not_found`` 错误。
    """
    return MobilePairingError(
        "claim_not_found",
        f"claim not found: {pairing_id}/{claim_id}",
    )


def invalid_request(message: str) -> MobilePairingError:
    """构造请求参数错误。

    关键输入：参数错误说明。
    关键输出：``invalid_request`` 错误。
    """
    return MobilePairingError("invalid_request", message)


def pairing_expired(pairing_id: str) -> MobilePairingError:
    """构造配对过期错误。

    关键输入：已过期的 ``pairing_id``。
    关键输出：``pairing_expired`` 错误。
    """
    return MobilePairingError("pairing_expired", f"pairing expired: {pairing_id}")


def nonce_mismatch(pairing_id: str) -> MobilePairingError:
    """构造 nonce 校验失败错误。

    关键输入：校验失败的 ``pairing_id``。
    关键输出：``nonce_mismatch`` 错误。
    """
    return MobilePairingError("nonce_mismatch", f"nonce mismatch: {pairing_id}")


def pairing_already_claimed(pairing_id: str) -> MobilePairingError:
    """构造配对已被 claim 错误。

    关键输入：已被占用的 ``pairing_id``。
    关键输出：``pairing_already_claimed`` 错误。
    """
    return MobilePairingError(
        "pairing_already_claimed",
        f"pairing already claimed: {pairing_id}",
    )


def approval_pending(pairing_id: str) -> MobilePairingError:
    """构造等待批准错误。

    关键输入：仍在等待桌面批准的 ``pairing_id``。
    关键输出：带 retryable 标记的 ``approval_pending`` 错误。
    """
    return MobilePairingError(
        "approval_pending",
        f"approval pending: {pairing_id}",
        retryable=True,
    )


def approval_denied(pairing_id: str) -> MobilePairingError:
    """构造批准被拒错误。

    关键输入：被拒绝的 ``pairing_id``。
    关键输出：``approval_denied`` 错误。
    """
    return MobilePairingError("approval_denied", f"approval denied: {pairing_id}")


def device_revoked(device_id: str) -> MobilePairingError:
    """构造设备已吊销错误。

    关键输入：已吊销的 ``device_id``。
    关键输出：``device_revoked`` 错误。
    """
    return MobilePairingError("device_revoked", f"device revoked: {device_id}")


def handoff_expired(handoff_id: str) -> MobilePairingError:
    """构造 handoff 过期错误。

    关键输入：已过期的 handoff 记录 ID 或 token 摘要。
    关键输出：``handoff_expired`` 错误。
    """
    return MobilePairingError("handoff_expired", f"handoff expired: {handoff_id}")


def handoff_consumed(handoff_id: str) -> MobilePairingError:
    """构造 handoff 已消费错误。

    关键输入：已消费的 handoff 记录 ID 或 token 摘要。
    关键输出：``handoff_consumed`` 错误。
    """
    return MobilePairingError("handoff_consumed", f"handoff consumed: {handoff_id}")


def invalid_token(message: str = "invalid token") -> MobilePairingError:
    """构造 token 无效错误。

    关键输入：可选错误说明。
    关键输出：``invalid_token`` 错误。
    """
    return MobilePairingError("invalid_token", message)


__all__ = [
    "HTTP_STATUS_BY_PUBLIC_ERROR_CODE",
    "PUBLIC_ERROR_CODE_BY_INTERNAL_CODE",
    "MobilePairingError",
    "approval_denied",
    "approval_pending",
    "claim_not_found",
    "device_revoked",
    "handoff_consumed",
    "handoff_expired",
    "invalid_request",
    "invalid_token",
    "mobile_pairing_error_body",
    "mobile_pairing_error_response",
    "mobile_pairing_http_status",
    "nonce_mismatch",
    "normalize_mobile_pairing_error_code",
    "pairing_already_claimed",
    "pairing_expired",
    "pairing_not_found",
    "unsupported_protocol",
]
