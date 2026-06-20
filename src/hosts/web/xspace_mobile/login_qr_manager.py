"""XSpace Mobile 登录二维码业务门户。

本脚本实现 ``LoginQrManager``，作用是编排登录页创建二维码、APK claim、登录页确认、
APK exchange 和状态查询。关键流程是 create 生成 ``login_qr_id``、nonce 和浏览器
私密 token，claim 校验 nonce 后进入待确认，confirm 写入授权证据，exchange 原子签发
device token 与 handoff token。关键类职责：Manager 是 Router 唯一业务入口，
Repository 负责持久化，TokenService 负责 token 生成与校验。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode

from hosts.web.xspace_mobile import errors
from hosts.web.xspace_mobile.models import (
    LoginQrClaimRecord,
    LoginQrClaimResult,
    LoginQrClaimStatus,
    LoginQrConfirmResult,
    LoginQrExchangeResult,
    LoginQrSessionCreateResult,
    LoginQrSessionRecord,
    LoginQrSessionStatus,
    MobileDeviceDescriptor,
)
from hosts.web.xspace_mobile.repository import MobilePairingRepository
from hosts.web.xspace_mobile.server_origin import LoginQrOriginView, ServerOriginConfig
from hosts.web.xspace_mobile.token_service import MobileDeviceTokenService

_SUPPORTED_PROTOCOL_VERSION = "1"
_DEFAULT_AUTHORIZED_USER_ID = "default"


def _utc_now() -> datetime:
    """生成当前 UTC 时间。

    关键输入：系统时钟。
    关键输出：带 UTC 时区的 ``datetime``。
    """
    return datetime.now(UTC)


def _hash_secret(value: str, field: str) -> str:
    """计算公开 secret 的 SHA-256 hash。

    关键输入：nonce 或 browser token 明文。
    关键输出：十六进制 SHA-256 摘要。
    """
    value = _require_text(value, field)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_text(value: str, field: str) -> str:
    """校验公开字符串输入。

    关键输入：调用方传入的字符串和字段名。
    关键输出：非空字符串；非法时抛稳定错误。
    """
    if not isinstance(value, str) or not value:
        raise errors.invalid_request(f"{field} is required")
    return value


def _require_device(value: MobileDeviceDescriptor) -> MobileDeviceDescriptor:
    """校验公开设备输入。

    关键输入：调用方传入的设备 DTO。
    关键输出：合法设备 DTO；非法时抛稳定错误。
    """
    if not isinstance(value, MobileDeviceDescriptor):
        raise errors.invalid_request("device is required")
    _require_text(value.device_id, "device.device_id")
    _require_text(value.label, "device.label")
    _require_text(value.app_version, "device.app_version")
    return value


def _new_login_qr_id() -> str:
    """生成登录二维码会话 ID。"""
    return f"lq_{secrets.token_urlsafe(18)}"


def _new_claim_id() -> str:
    """生成登录二维码 claim ID。"""
    return f"cl_{secrets.token_urlsafe(18)}"


def _new_nonce() -> str:
    """生成登录二维码 nonce。"""
    return secrets.token_urlsafe(24)


def _new_browser_token() -> str:
    """生成登录页私密 token。"""
    return f"kgm_lqt_{secrets.token_urlsafe(32)}"


def _origin_view_from_session(session: LoginQrSessionRecord) -> LoginQrOriginView:
    """从持久化 session 还原 origin 视图。"""
    return LoginQrOriginView(
        mode=session.origin_mode,
        origin=session.server_origin,
        scheme=session.origin_scheme,
        host=session.origin_host,
        port=session.origin_port,
    )


class LoginQrManager:
    """登录二维码业务门户。

    职责：维护扫码登录状态机，对外提供任务级方法。
    关键输入：Repository、TokenService 和 ServerOriginConfig。
    关键输出：公开 DTO 或稳定业务错误。
    """

    def __init__(
        self,
        repository: MobilePairingRepository,
        token_service: MobileDeviceTokenService | None = None,
        server_origin_config: ServerOriginConfig | None = None,
    ) -> None:
        """初始化 Manager。

        关键输入：持久化 repository，可选 token service 和 origin 配置门户。
        关键输出：可执行扫码登录业务方法的 Manager 实例。
        """
        self._repository = repository
        self._token_service = token_service or MobileDeviceTokenService(repository)
        self._server_origin_config = server_origin_config or ServerOriginConfig()

    def create_login_qr_session(
        self,
        *,
        protocol_version: str,
        client: str,
        requested_scopes: list[str],
        raw_server_origin: str | None,
        ttl_seconds: int = 60,
        now: datetime | None = None,
    ) -> LoginQrSessionCreateResult:
        """创建登录二维码会话。

        关键输入：协议版本、创建方、请求 scope、原始 server origin 和 TTL。
        关键输出：包含 nonce、browser token、QR payload 和 copy URL 的创建结果。
        """
        self._ensure_supported_protocol(protocol_version)
        _require_text(client, "client")
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise errors.invalid_request("ttl_seconds must be positive")
        if not isinstance(requested_scopes, list):
            raise errors.invalid_request("requested_scopes must be a list")

        origin = self._server_origin_config.require_login_qr_origin(raw_server_origin)
        now = now or _utc_now()
        login_qr_id = _new_login_qr_id()
        nonce = _new_nonce()
        browser_token = _new_browser_token()
        expires_at = now + timedelta(seconds=ttl_seconds)
        record = LoginQrSessionRecord(
            login_qr_id=login_qr_id,
            protocol_version=protocol_version,
            origin_mode=origin.mode,
            server_origin=origin.origin,
            origin_scheme=origin.scheme,
            origin_host=origin.host,
            origin_port=origin.port,
            nonce_hash=_hash_secret(nonce, "nonce"),
            browser_token_hash=_hash_secret(browser_token, "browser_token"),
            requested_scopes=list(requested_scopes),
            status=LoginQrSessionStatus.PENDING_SCAN,
            expires_at=expires_at,
            created_at=now,
        )
        self._repository.create_login_qr_session(record)
        query = urlencode(
            {
                "server": origin.origin,
                "origin_mode": origin.mode,
                "login_qr_id": login_qr_id,
                "nonce": nonce,
                "v": protocol_version,
                "purpose": "login",
            },
            quote_via=quote,
        )
        return LoginQrSessionCreateResult(
            login_qr_id=login_qr_id,
            browser_token=browser_token,
            nonce=nonce,
            expires_at=expires_at,
            server_origin=origin,
            server=origin.origin,
            qr_payload=f"xspace://login-kongming?{query}",
            copy_url=f"{origin.origin}/-/xspace/mobile/login?{query}",
        )

    def get_login_qr_view(
        self,
        login_qr_id: str,
        *,
        browser_token: str,
        now: datetime | None = None,
    ) -> tuple[LoginQrSessionRecord, LoginQrClaimRecord | None]:
        """读取登录页轮询所需登录二维码视图。

        关键输入：登录二维码 ID、browser token 和当前时间。
        关键输出：会话记录及其 claim 记录；过期会话返回 expired 快照。
        """
        now = now or _utc_now()
        session = self._load_existing_session_record(login_qr_id)
        self._require_browser_token(session, browser_token)
        if self._is_expirable(session) and session.expires_at <= now:
            session = self._repository.mark_login_qr_expired(session.login_qr_id)
        claim = self._repository.get_login_qr_claim_for_session(session.login_qr_id)
        return session, claim

    def claim_login_qr(
        self,
        *,
        login_qr_id: str,
        protocol_version: str,
        nonce: str,
        device: MobileDeviceDescriptor,
        capabilities: dict[str, bool],
        now: datetime | None = None,
    ) -> LoginQrClaimResult:
        """APK claim 登录二维码会话。

        关键输入：登录二维码 ID、协议版本、nonce、设备描述和能力声明。
        关键输出：claim ID 和 pending_confirm 状态。
        """
        self._ensure_supported_protocol(protocol_version)
        login_qr_id = _require_text(login_qr_id, "login_qr_id")
        nonce = _require_text(nonce, "nonce")
        device = _require_device(device)
        if not isinstance(capabilities, dict):
            raise errors.invalid_request("capabilities must be an object")
        now = now or _utc_now()
        session = self._load_open_session(login_qr_id, now=now)
        if session.nonce_hash != _hash_secret(nonce, "nonce"):
            raise errors.nonce_mismatch(login_qr_id)
        claim = LoginQrClaimRecord(
            claim_id=_new_claim_id(),
            login_qr_id=login_qr_id,
            device_id=device.device_id,
            label=device.label,
            platform=device.platform,
            app_version=device.app_version,
            capabilities=dict(capabilities),
            status=LoginQrClaimStatus.PENDING_CONFIRM,
            created_at=now,
        )
        stored = self._repository.claim_login_qr_if_open(claim, now=now)
        return LoginQrClaimResult(
            login_qr_id=login_qr_id,
            claim_id=stored.claim_id,
            status=stored.status,
        )

    def confirm_login_qr(
        self,
        *,
        login_qr_id: str,
        claim_id: str,
        browser_token: str,
        authorization_method: str = "password",
        authorized_user_id: str = _DEFAULT_AUTHORIZED_USER_ID,
        now: datetime | None = None,
    ) -> LoginQrConfirmResult:
        """登录页确认扫码登录。

        关键输入：登录二维码 ID、claim ID、browser token 和授权证据。
        关键输出：confirmed 状态。
        """
        login_qr_id = _require_text(login_qr_id, "login_qr_id")
        claim_id = _require_text(claim_id, "claim_id")
        authorization_method = _require_text(authorization_method, "authorization_method")
        authorized_user_id = _require_text(authorized_user_id, "authorized_user_id")
        if authorization_method not in {"password", "device_token", "admin_session"}:
            raise errors.invalid_request("authorization_method is invalid")
        session = self._load_existing_session(login_qr_id, now=now or _utc_now())
        self._require_browser_token(session, browser_token)
        updated_session, updated_claim = self._repository.confirm_login_qr(
            login_qr_id,
            claim_id,
            authorization_method=authorization_method,
            authorized_user_id=authorized_user_id,
            now=now or _utc_now(),
        )
        return LoginQrConfirmResult(
            login_qr_id=updated_session.login_qr_id,
            claim_id=updated_claim.claim_id,
            status=updated_session.status,
        )

    def cancel_login_qr(self, login_qr_id: str, *, browser_token: str) -> LoginQrSessionRecord:
        """取消登录二维码会话。

        关键输入：登录二维码 ID 和 browser token。
        关键输出：取消后的 session 快照。
        """
        session = self._load_existing_session_record(login_qr_id)
        self._require_browser_token(session, browser_token)
        return self._repository.cancel_login_qr(session.login_qr_id)

    def exchange_login_qr(
        self,
        *,
        login_qr_id: str,
        claim_id: str,
        nonce: str,
        device_id: str,
        now: datetime | None = None,
    ) -> LoginQrExchangeResult:
        """APK 兑换 device token 和 handoff token。

        关键输入：登录二维码 ID、claim ID、nonce、设备 ID 和当前时间。
        关键输出：device token 明文和一次性 handoff token 明文。
        """
        login_qr_id = _require_text(login_qr_id, "login_qr_id")
        claim_id = _require_text(claim_id, "claim_id")
        nonce = _require_text(nonce, "nonce")
        device_id = _require_text(device_id, "device_id")
        now = now or _utc_now()
        session = self._load_existing_session(login_qr_id, now=now)
        if session.nonce_hash != _hash_secret(nonce, "nonce"):
            raise errors.nonce_mismatch(login_qr_id)
        if session.status == LoginQrSessionStatus.EXCHANGED:
            raise errors.login_qr_already_exchanged(login_qr_id)
        if session.status == LoginQrSessionStatus.CANCELLED:
            raise errors.approval_denied(login_qr_id)
        if session.status != LoginQrSessionStatus.CONFIRMED:
            raise errors.approval_pending(login_qr_id)

        claim = self._repository.get_login_qr_claim(claim_id)
        if claim is None or claim.login_qr_id != login_qr_id or claim.device_id != device_id:
            raise errors.claim_not_found(login_qr_id, claim_id)
        if claim.status == LoginQrClaimStatus.PENDING_CONFIRM:
            raise errors.approval_pending(login_qr_id)
        if claim.status == LoginQrClaimStatus.DENIED:
            raise errors.approval_denied(login_qr_id)
        if claim.status == LoginQrClaimStatus.EXCHANGED:
            raise errors.login_qr_already_exchanged(login_qr_id)
        if not session.authorization_method or not session.authorized_user_id:
            raise errors.approval_pending(login_qr_id)

        device = MobileDeviceDescriptor(
            device_id=claim.device_id,
            label=claim.label,
            platform=claim.platform,
            app_version=claim.app_version,
        )
        device_issue, handoff_issue = self._token_service.issue_login_qr_exchange_tokens(
            login_qr_id=login_qr_id,
            claim_id=claim_id,
            device=device,
            scopes=session.requested_scopes,
            user_id=session.authorized_user_id,
            now=now,
        )
        return LoginQrExchangeResult(
            login_qr_id=login_qr_id,
            claim_id=claim_id,
            device_id=device.device_id,
            device_token=device_issue.device_token,
            handoff_token=handoff_issue.handoff_token,
            handoff_expires_at=handoff_issue.expires_at,
            server_origin=_origin_view_from_session(session),
            scopes=session.requested_scopes,
        )

    def _ensure_supported_protocol(self, protocol_version: str) -> None:
        """校验协议版本。"""
        protocol_version = _require_text(protocol_version, "protocol_version")
        if protocol_version != _SUPPORTED_PROTOCOL_VERSION:
            raise errors.unsupported_protocol(protocol_version)

    def _require_browser_token(self, session: LoginQrSessionRecord, browser_token: str) -> None:
        """校验浏览器私密 token。"""
        if session.browser_token_hash != _hash_secret(browser_token, "browser_token"):
            raise errors.browser_token_mismatch(session.login_qr_id)

    def _load_open_session(
        self,
        login_qr_id: str,
        *,
        now: datetime,
    ) -> LoginQrSessionRecord:
        """读取仍可 claim 的登录二维码会话。"""
        session = self._load_existing_session(login_qr_id, now=now)
        if session.status != LoginQrSessionStatus.PENDING_SCAN:
            raise errors.login_qr_already_claimed(login_qr_id)
        return session

    def _load_existing_session(
        self,
        login_qr_id: str,
        *,
        now: datetime,
    ) -> LoginQrSessionRecord:
        """读取存在且未过期的登录二维码会话。"""
        session = self._load_existing_session_record(login_qr_id)
        if self._is_expirable(session) and session.expires_at <= now:
            self._repository.mark_login_qr_expired(login_qr_id)
            raise errors.login_qr_expired(login_qr_id)
        return session

    def _load_existing_session_record(self, login_qr_id: str) -> LoginQrSessionRecord:
        """读取存在的登录二维码会话。"""
        login_qr_id = _require_text(login_qr_id, "login_qr_id")
        session = self._repository.get_login_qr_session(login_qr_id)
        if session is None:
            raise errors.login_qr_not_found(login_qr_id)
        return session

    def _is_expirable(self, session: LoginQrSessionRecord) -> bool:
        """判断登录二维码会话是否仍会按 TTL 过期。"""
        return session.status not in {
            LoginQrSessionStatus.CANCELLED,
            LoginQrSessionStatus.EXCHANGED,
            LoginQrSessionStatus.EXPIRED,
        }


__all__ = ["LoginQrManager"]
