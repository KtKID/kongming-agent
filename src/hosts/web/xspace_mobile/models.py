"""XSpace Mobile 配对核心数据模型。

本脚本定义移动扫码配对后端核心的状态枚举、持久化记录和请求/结果 DTO。
作用是为 Repository、TokenService、Manager 提供单一数据合同。关键流程是
Manager 接收请求 DTO，Repository 读写 Record DTO，TokenService 返回 token/handoff
结果 DTO。关键类职责：状态枚举固定状态机取值，Record 类映射 SQLite 表，Result
类承载后续 Router 和 Android 对接需要的公开字段。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hosts.web.xspace_mobile.server_origin import LoginQrOriginView


class PairingSessionStatus(StrEnum):
    """配对会话状态枚举。

    职责：固定 PairingSession 的状态机取值。
    关键输入：数据库或业务层传入的状态字符串。
    关键输出：类型安全的会话状态。
    """

    PENDING_SCAN = "pending_scan"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    EXCHANGED = "exchanged"


class PairingClaimStatus(StrEnum):
    """配对 claim 状态枚举。

    职责：固定 PairingClaim 的审批和兑换状态。
    关键输入：数据库或业务层传入的状态字符串。
    关键输出：类型安全的 claim 状态。
    """

    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    DENIED = "denied"
    EXCHANGED = "exchanged"


class MobileDeviceDescriptor(BaseModel):
    """移动设备请求描述。

    职责：承载 Android claim/exchange 传入的设备身份和展示信息。
    关键输入：``device_id``、展示名、平台、App 版本。
    关键输出：Manager 可验证和持久化的设备描述。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str
    label: str
    platform: Literal["android"] = "android"
    app_version: str


class PairingSessionRecord(BaseModel):
    """配对会话持久化记录。

    职责：映射 ``pairing_sessions`` 表的一行。
    关键输入：会话 ID、nonce hash、server origin、scope、状态和时间字段。
    关键输出：Repository/Manager 使用的会话快照。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pairing_id: str
    protocol_version: str
    client: str
    nonce_hash: str
    server_origin: str
    requested_scopes: list[str]
    status: PairingSessionStatus
    expires_at: datetime
    created_at: datetime
    approved_at: datetime | None = None
    denied_at: datetime | None = None


class PairingClaimRecord(BaseModel):
    """配对 claim 持久化记录。

    职责：映射 ``pairing_claims`` 表的一行。
    关键输入：claim ID、所属 pairing、设备信息、能力声明、状态和创建时间。
    关键输出：Repository/Manager 使用的 claim 快照。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    pairing_id: str
    device_id: str
    label: str
    platform: Literal["android"]
    app_version: str
    capabilities: dict[str, bool]
    status: PairingClaimStatus
    created_at: datetime


class MobileDeviceRecord(BaseModel):
    """移动设备持久化记录。

    职责：映射 ``mobile_devices`` 表的一行，保存 token hash 和吊销状态。
    关键输入：设备身份、授权 scope、token hash 和时间字段。
    关键输出：TokenService 校验和 Manager 展示使用的设备快照。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str
    label: str
    platform: Literal["android"]
    app_version: str
    scopes: list[str]
    token_hash: str
    created_at: datetime
    last_seen_at: datetime | None = None
    revoked_at: datetime | None = None


class HandoffTokenRecord(BaseModel):
    """一次性 handoff token 持久化记录。

    职责：映射 ``handoff_tokens`` 表的一行。
    关键输入：handoff ID、token hash、设备 ID、scope、用户 ID 和时间字段。
    关键输出：TokenService 消费 handoff 后生成登录上下文。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    handoff_id: str
    token_hash: str
    device_id: str
    scopes: list[str]
    user_id: str
    expires_at: datetime
    consumed_at: datetime | None = None
    created_at: datetime


class PairingSessionCreateResult(BaseModel):
    """创建配对会话结果 DTO。

    职责：返回二维码、复制链接和 nonce 明文。
    关键输入：Manager 生成的 session record 和 nonce。
    关键输出：后续 Router 可直接序列化的创建结果。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pairing_id: str
    nonce: str
    expires_at: datetime
    qr_payload: str
    copy_url: str
    status: PairingSessionStatus = PairingSessionStatus.PENDING_SCAN


class PairingClaimResult(BaseModel):
    """claim 会话结果 DTO。

    职责：返回 Android claim 成功后的 claim ID 和当前状态。
    关键输入：Repository 原子 claim 后的 claim record。
    关键输出：Android 轮询 exchange 所需的 claim 信息。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pairing_id: str
    claim_id: str
    status: PairingClaimStatus
    poll_after_ms: int = Field(default=1000, ge=0)


class PairingApprovalResult(BaseModel):
    """审批结果 DTO。

    职责：返回桌面批准或拒绝后的状态。
    关键输入：Repository 审批事务后的 session 和 claim。
    关键输出：Web 页面展示和后续 Router 序列化所需状态。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pairing_id: str
    claim_id: str
    approved: bool
    session_status: PairingSessionStatus
    claim_status: PairingClaimStatus


class DeviceTokenIssueResult(BaseModel):
    """device token 签发结果 DTO。

    职责：返回只展示一次的 device token 明文和设备快照。
    关键输入：TokenService 生成的 opaque token 和已持久化设备记录。
    关键输出：Manager exchange 结果中的 device token 部分。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_token: str
    device: MobileDeviceRecord


class HandoffIssueResult(BaseModel):
    """handoff token 签发结果 DTO。

    职责：返回一次性 handoff token 明文和过期时间。
    关键输入：TokenService 生成的 opaque token 和 handoff record。
    关键输出：Android WebView 打开 URL 所需 token。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    handoff_token: str
    handoff_id: str
    expires_at: datetime


class HandoffLoginContext(BaseModel):
    """handoff 消费后的登录上下文 DTO。

    职责：为后续 Router 设置 cookie 提供最小上下文。
    关键输入：已消费 handoff 记录和关联设备记录。
    关键输出：包含 device_id、scope 和 user_id 的登录上下文。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str
    scopes: list[str]
    user_id: str


class PairingExchangeResult(BaseModel):
    """exchange 配对结果 DTO。

    职责：返回 Android 最终需要保存和打开的 token 信息。
    关键输入：已批准 claim、device token 签发结果和 handoff 签发结果。
    关键输出：device token 明文、handoff token 明文和当前状态。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pairing_id: str
    claim_id: str
    device_id: str
    device_token: str
    handoff_token: str
    handoff_expires_at: datetime


class LoginQrSessionStatus(StrEnum):
    """登录二维码会话状态枚举。

    职责：固定登录二维码从创建、扫码、确认到 exchange 的状态机取值。
    关键输入：数据库或业务层传入的状态字符串。
    关键输出：类型安全的登录二维码会话状态。
    """

    PENDING_SCAN = "pending_scan"
    PENDING_CONFIRM = "pending_confirm"
    CONFIRMED = "confirmed"
    EXCHANGED = "exchanged"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class LoginQrClaimStatus(StrEnum):
    """登录二维码 claim 状态枚举。

    职责：固定 APK claim 的确认和兑换状态。
    关键输入：数据库或业务层传入的状态字符串。
    关键输出：类型安全的登录二维码 claim 状态。
    """

    PENDING_CONFIRM = "pending_confirm"
    APPROVED = "approved"
    DENIED = "denied"
    EXCHANGED = "exchanged"


class LoginQrSessionRecord(BaseModel):
    """登录二维码会话持久化记录。

    职责：映射 ``login_qr_sessions`` 表的一行。
    关键输入：会话 ID、origin 快照、nonce/browser token hash、状态和授权证据。
    关键输出：Repository/Manager 使用的登录二维码会话快照。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    login_qr_id: str
    purpose: Literal["login"] = "login"
    protocol_version: str
    origin_mode: Literal["public_https", "lan_ip"]
    server_origin: str
    origin_scheme: Literal["https", "http"]
    origin_host: str
    origin_port: int | None = None
    nonce_hash: str
    browser_token_hash: str
    requested_scopes: list[str]
    status: LoginQrSessionStatus
    claim_id: str | None = None
    authorization_method: Literal["password", "device_token", "admin_session"] | None = None
    authorized_user_id: str | None = None
    expires_at: datetime
    created_at: datetime
    claimed_at: datetime | None = None
    confirmed_at: datetime | None = None
    exchanged_at: datetime | None = None


class LoginQrClaimRecord(BaseModel):
    """登录二维码 claim 持久化记录。

    职责：映射 ``login_qr_claims`` 表的一行。
    关键输入：claim ID、登录二维码 ID、设备信息、能力声明和状态。
    关键输出：Repository/Manager 使用的登录二维码 claim 快照。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    login_qr_id: str
    device_id: str
    label: str
    platform: Literal["android"]
    app_version: str
    capabilities: dict[str, bool]
    status: LoginQrClaimStatus
    created_at: datetime


class LoginQrSessionCreateResult(BaseModel):
    """创建登录二维码结果 DTO。

    职责：返回二维码、复制链接、nonce 明文和浏览器私密 token。
    关键输入：Manager 生成的 session record、nonce 和 browser token。
    关键输出：Router 可直接序列化的创建结果。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    login_qr_id: str
    browser_token: str
    nonce: str
    expires_at: datetime
    server_origin: LoginQrOriginView
    server: str
    qr_payload: str
    copy_url: str
    status: LoginQrSessionStatus = LoginQrSessionStatus.PENDING_SCAN


class LoginQrClaimResult(BaseModel):
    """APK claim 登录二维码结果 DTO。

    职责：返回 claim ID 和等待登录页确认状态。
    关键输入：Repository 原子 claim 后的 claim record。
    关键输出：APK 轮询 exchange 所需的 claim 信息。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    login_qr_id: str
    claim_id: str
    status: LoginQrClaimStatus
    poll_after_ms: int = Field(default=1000, ge=0)


class LoginQrConfirmResult(BaseModel):
    """登录页确认登录二维码结果 DTO。

    职责：返回密码确认后的 confirmed 状态。
    关键输入：已确认的 session 和 claim。
    关键输出：登录页和 APK 轮询提示使用的确认结果。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    login_qr_id: str
    claim_id: str
    status: LoginQrSessionStatus
    poll_after_ms: int = Field(default=1000, ge=0)


class LoginQrExchangeResult(BaseModel):
    """登录二维码 exchange 结果 DTO。

    职责：返回 APK 最终需要保存和打开的 token 信息。
    关键输入：已确认 claim、device token 签发结果和 handoff 签发结果。
    关键输出：device token 明文、handoff token 明文和 server origin 快照。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    login_qr_id: str
    claim_id: str
    device_id: str
    device_token: str
    handoff_token: str
    handoff_expires_at: datetime
    server_origin: LoginQrOriginView
    scopes: list[str]
    status: PairingSessionStatus = PairingSessionStatus.EXCHANGED


__all__ = [
    "DeviceTokenIssueResult",
    "HandoffIssueResult",
    "HandoffLoginContext",
    "HandoffTokenRecord",
    "MobileDeviceDescriptor",
    "MobileDeviceRecord",
    "PairingApprovalResult",
    "PairingClaimRecord",
    "PairingClaimResult",
    "PairingClaimStatus",
    "PairingExchangeResult",
    "PairingSessionCreateResult",
    "PairingSessionRecord",
    "PairingSessionStatus",
]
