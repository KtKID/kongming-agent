"""XSpace Mobile 配对后端核心公开入口。

本脚本控制 ``hosts.web.xspace_mobile`` 包的公开导出，作用是让后续 Router 只依赖
门户类、公开 DTO 和公开错误类型。关键流程是外部 import ``MobilePairingManager``、
``MobilePairingRepository``、``MobileDeviceTokenService`` 以及 models/errors 中的
合同类型。关键类/函数职责由各子模块承担，本文件只维护包边界。
"""

from __future__ import annotations

from hosts.web.xspace_mobile.errors import (
    MobilePairingError,
)
from hosts.web.xspace_mobile.login_qr_auth_service import LoginQrAuthService
from hosts.web.xspace_mobile.login_qr_manager import LoginQrManager
from hosts.web.xspace_mobile.manager import MobilePairingManager
from hosts.web.xspace_mobile.models import (
    DeviceTokenIssueResult,
    HandoffIssueResult,
    HandoffLoginContext,
    HandoffTokenRecord,
    LoginQrClaimRecord,
    LoginQrClaimResult,
    LoginQrClaimStatus,
    LoginQrConfirmResult,
    LoginQrExchangeResult,
    LoginQrSessionCreateResult,
    LoginQrSessionRecord,
    LoginQrSessionStatus,
    MobileDeviceDescriptor,
    MobileDeviceRecord,
    PairingApprovalResult,
    PairingClaimRecord,
    PairingClaimResult,
    PairingClaimStatus,
    PairingExchangeResult,
    PairingSessionCreateResult,
    PairingSessionRecord,
    PairingSessionStatus,
)
from hosts.web.xspace_mobile.repository import MobilePairingRepository
from hosts.web.xspace_mobile.server_origin import LoginQrOriginView, ServerOriginConfig
from hosts.web.xspace_mobile.token_service import MobileDeviceTokenService

__all__ = [
    "DeviceTokenIssueResult",
    "HandoffIssueResult",
    "HandoffLoginContext",
    "HandoffTokenRecord",
    "LoginQrClaimRecord",
    "LoginQrClaimResult",
    "LoginQrClaimStatus",
    "LoginQrAuthService",
    "LoginQrConfirmResult",
    "LoginQrExchangeResult",
    "LoginQrManager",
    "LoginQrOriginView",
    "LoginQrSessionCreateResult",
    "LoginQrSessionRecord",
    "LoginQrSessionStatus",
    "MobileDeviceDescriptor",
    "MobileDeviceRecord",
    "MobileDeviceTokenService",
    "MobilePairingError",
    "MobilePairingManager",
    "MobilePairingRepository",
    "PairingApprovalResult",
    "PairingClaimRecord",
    "PairingClaimResult",
    "PairingClaimStatus",
    "PairingExchangeResult",
    "PairingSessionCreateResult",
    "PairingSessionRecord",
    "PairingSessionStatus",
    "ServerOriginConfig",
]
