"""XSpace Mobile 错误合同单元测试。

本脚本验证移动配对错误 helper，作用是固定内部错误码归一化、HTTP status 和
Android client 读取的 ``body.error.code/message/retryable`` 响应结构。关键流程是
构造 ``MobilePairingError``，再断言 Router 可复用 helper 输出稳定合同。
"""

from __future__ import annotations

import pytest

from hosts.web.xspace_mobile.errors import (
    HTTP_STATUS_BY_PUBLIC_ERROR_CODE,
    MobilePairingError,
    mobile_pairing_error_body,
    mobile_pairing_error_response,
    mobile_pairing_http_status,
    normalize_mobile_pairing_error_code,
)


@pytest.mark.parametrize(
    ("code", "status_code"),
    [
        ("server_origin_required", 400),
        ("server_origin_invalid_scheme", 400),
        ("server_origin_loopback", 400),
        ("server_origin_not_lan_ip", 400),
        ("server_origin_public_host_invalid", 400),
        ("unsupported_protocol", 400),
        ("invalid_pairing_payload", 400),
        ("invalid_credentials", 401),
        ("rate_limited", 429),
        ("login_qr_not_found", 404),
        ("login_qr_expired", 410),
        ("browser_token_mismatch", 403),
        ("login_qr_already_claimed", 409),
        ("login_qr_already_exchanged", 409),
        ("pairing_not_found", 404),
        ("pairing_expired", 410),
        ("nonce_mismatch", 403),
        ("pairing_already_claimed", 409),
        ("approval_pending", 202),
        ("approval_denied", 403),
        ("device_revoked", 401),
        ("token_expired", 401),
        ("reauth_required", 401),
    ],
)
def test_public_error_codes_have_contract_status(
    code: str,
    status_code: int,
) -> None:
    """验证公开错误码状态码清单。

    关键输入：qdev README 定义的公开错误码。
    关键输出：helper 返回对应 HTTP status。
    """
    assert HTTP_STATUS_BY_PUBLIC_ERROR_CODE[code] == status_code
    assert mobile_pairing_http_status(code) == status_code


@pytest.mark.parametrize(
    ("internal_code", "public_code", "status_code"),
    [
        ("invalid_request", "invalid_pairing_payload", 400),
        ("claim_not_found", "pairing_not_found", 404),
        ("handoff_expired", "token_expired", 401),
        ("handoff_consumed", "reauth_required", 401),
        ("invalid_token", "reauth_required", 401),
    ],
)
def test_internal_error_codes_normalize_to_public_contract(
    internal_code: str,
    public_code: str,
    status_code: int,
) -> None:
    """验证内部错误码归一化。

    关键输入：现有 Manager/Repository/TokenService 内部错误码。
    关键输出：公开错误码和 HTTP status 与 Android 合同一致。
    """
    assert normalize_mobile_pairing_error_code(internal_code) == public_code
    assert mobile_pairing_http_status(internal_code) == status_code


def test_error_body_matches_android_pairing_error_shape() -> None:
    """验证错误响应体结构。

    关键输入：内部 ``invalid_request`` 错误。
    关键输出：``body.error.code/message/retryable`` 三字段完整。
    """
    error = MobilePairingError(
        "invalid_request",
        "qr payload is invalid",
        retryable=False,
    )

    assert mobile_pairing_error_body(error) == {
        "error": {
            "code": "invalid_pairing_payload",
            "message": "qr payload is invalid",
            "retryable": False,
        }
    }


@pytest.mark.parametrize(
    ("error", "status_code", "public_code"),
    [
        (
            MobilePairingError(
                "approval_pending",
                "approval pending: pr_test",
                retryable=True,
            ),
            202,
            "approval_pending",
        ),
        (
            MobilePairingError(
                "approval_denied",
                "approval denied: pr_test",
                retryable=False,
            ),
            403,
            "approval_denied",
        ),
    ],
)
def test_error_response_preserves_retryable_semantics(
    error: MobilePairingError,
    status_code: int,
    public_code: str,
) -> None:
    """验证 retryable 语义进入响应体。

    关键输入：等待授权和拒绝授权两类业务错误。
    关键输出：HTTP status、公开 code 和 retryable 均稳定。
    """
    response_status, body = mobile_pairing_error_response(error)

    assert response_status == status_code
    assert body["error"]["code"] == public_code
    assert body["error"]["message"] == error.message
    assert body["error"]["retryable"] is error.retryable
