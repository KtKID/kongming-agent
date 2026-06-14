"""XSpace Mobile 配对业务门户。

本脚本实现 ``MobilePairingManager``，作用是编排创建 pairing session、手机
claim、桌面 approve/deny、Android exchange、设备吊销等状态机。关键流程是
create 生成 pairing_id/nonce/QR，claim 校验协议/nonce/过期并原子占用会话，
approve 推动审批状态，exchange 签发 device token 和一次性 handoff token。
关键类/函数职责：Manager 是后续 Router 唯一业务入口，Repository 负责持久化，
TokenService 负责 token 生成与校验。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode

from hosts.web.xspace_mobile import errors
from hosts.web.xspace_mobile.models import (
    MobileDeviceDescriptor,
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
from hosts.web.xspace_mobile.token_service import MobileDeviceTokenService

_SUPPORTED_PROTOCOL_VERSION = "1"


def _utc_now() -> datetime:
    """生成当前 UTC 时间。

    关键输入：系统时钟。
    关键输出：带 UTC 时区的 ``datetime``。
    """
    return datetime.now(UTC)


def _hash_nonce(nonce: str) -> str:
    """计算 nonce 的 SHA-256 hash。

    关键输入：nonce 明文。
    关键输出：十六进制 SHA-256 摘要。
    """
    nonce = _require_text(nonce, "nonce")
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


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


def _new_pairing_id() -> str:
    """生成配对会话 ID。

    关键输入：系统安全随机源。
    关键输出：``pr_`` 前缀 URL 安全 ID。
    """
    return f"pr_{secrets.token_urlsafe(18)}"


def _new_claim_id() -> str:
    """生成 claim ID。

    关键输入：系统安全随机源。
    关键输出：``cl_`` 前缀 URL 安全 ID。
    """
    return f"cl_{secrets.token_urlsafe(18)}"


def _new_nonce() -> str:
    """生成配对 nonce。

    关键输入：系统安全随机源。
    关键输出：URL 安全 nonce 明文。
    """
    return secrets.token_urlsafe(24)


class MobilePairingManager:
    """移动扫码配对业务门户。

    职责：维护配对状态机，对外提供任务级方法。
    关键输入：Repository 和 TokenService。
    关键输出：公开 DTO 或稳定业务错误。
    """

    def __init__(
        self,
        repository: MobilePairingRepository,
        token_service: MobileDeviceTokenService | None = None,
    ) -> None:
        """初始化 Manager。

        关键输入：持久化 repository，可选 token service。
        关键输出：可执行配对业务方法的 Manager 实例。
        """
        self._repository = repository
        self._token_service = token_service or MobileDeviceTokenService(repository)

    def create_pairing_session(
        self,
        *,
        protocol_version: str,
        client: str,
        requested_scopes: list[str],
        server_origin: str,
        ttl_seconds: int = 60,
        now: datetime | None = None,
    ) -> PairingSessionCreateResult:
        """创建配对会话。

        关键输入：协议版本、创建方、请求 scope、服务端 origin 和 TTL。
        关键输出：包含 nonce 明文、QR payload 和 copy URL 的创建结果。
        """
        self._ensure_supported_protocol(protocol_version)
        client = _require_text(client, "client")
        server_origin = _require_text(server_origin, "server_origin")
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise errors.invalid_request("ttl_seconds must be positive")
        if not isinstance(requested_scopes, list):
            raise errors.invalid_request("requested_scopes must be a list")
        now = now or _utc_now()
        pairing_id = _new_pairing_id()
        nonce = _new_nonce()
        expires_at = now + timedelta(seconds=ttl_seconds)
        normalized_origin = server_origin.rstrip("/")
        record = PairingSessionRecord(
            pairing_id=pairing_id,
            protocol_version=protocol_version,
            client=client,
            nonce_hash=_hash_nonce(nonce),
            server_origin=normalized_origin,
            requested_scopes=list(requested_scopes),
            status=PairingSessionStatus.PENDING_SCAN,
            expires_at=expires_at,
            created_at=now,
        )
        self._repository.create_pairing_session(record)
        query = urlencode(
            {
                "server": normalized_origin,
                "pairing_id": pairing_id,
                "nonce": nonce,
                "v": protocol_version,
            },
            quote_via=quote,
        )
        qr_payload = f"xspace://pair-kongming?{query}"
        copy_url = f"{normalized_origin}/-/xspace/mobile/pair?{query}"
        return PairingSessionCreateResult(
            pairing_id=pairing_id,
            nonce=nonce,
            expires_at=expires_at,
            qr_payload=qr_payload,
            copy_url=copy_url,
        )

    def claim_session(
        self,
        *,
        pairing_id: str,
        protocol_version: str,
        nonce: str,
        device: MobileDeviceDescriptor,
        capabilities: dict[str, bool],
        now: datetime | None = None,
    ) -> PairingClaimResult:
        """手机 claim 配对会话。

        关键输入：pairing ID、协议版本、nonce、设备描述和能力声明。
        关键输出：claim ID 和 pending_approval 状态。
        """
        self._ensure_supported_protocol(protocol_version)
        pairing_id = _require_text(pairing_id, "pairing_id")
        nonce = _require_text(nonce, "nonce")
        device = _require_device(device)
        if not isinstance(capabilities, dict):
            raise errors.invalid_request("capabilities must be an object")
        now = now or _utc_now()
        session = self._load_open_session(pairing_id, now=now)
        if session.nonce_hash != _hash_nonce(nonce):
            raise errors.nonce_mismatch(pairing_id)
        claim = PairingClaimRecord(
            claim_id=_new_claim_id(),
            pairing_id=pairing_id,
            device_id=device.device_id,
            label=device.label,
            platform=device.platform,
            app_version=device.app_version,
            capabilities=dict(capabilities),
            status=PairingClaimStatus.PENDING_APPROVAL,
            created_at=now,
        )
        stored = self._repository.claim_if_open(claim, now=now)
        return PairingClaimResult(
            pairing_id=pairing_id,
            claim_id=stored.claim_id,
            status=stored.status,
        )

    def approve_claim(
        self,
        *,
        pairing_id: str,
        claim_id: str,
        approved: bool,
        now: datetime | None = None,
    ) -> PairingApprovalResult:
        """批准或拒绝 claim。

        关键输入：pairing ID、claim ID、批准布尔值和当前时间。
        关键输出：审批后的 session/claim 状态。
        """
        pairing_id = _require_text(pairing_id, "pairing_id")
        claim_id = _require_text(claim_id, "claim_id")
        if type(approved) is not bool:
            raise errors.invalid_request("approved must be boolean")
        session, claim = self._repository.approve_claim(
            pairing_id,
            claim_id,
            approved=approved,
            now=now or _utc_now(),
        )
        return PairingApprovalResult(
            pairing_id=pairing_id,
            claim_id=claim_id,
            approved=approved,
            session_status=session.status,
            claim_status=claim.status,
        )

    def deny_claim(
        self,
        *,
        pairing_id: str,
        claim_id: str,
        now: datetime | None = None,
    ) -> PairingApprovalResult:
        """拒绝 claim。

        关键输入：pairing ID、claim ID 和当前时间。
        关键输出：拒绝后的 session/claim 状态。
        """
        return self.approve_claim(
            pairing_id=pairing_id,
            claim_id=claim_id,
            approved=False,
            now=now,
        )

    def exchange_pairing(
        self,
        *,
        pairing_id: str,
        claim_id: str,
        nonce: str,
        device_id: str,
        now: datetime | None = None,
    ) -> PairingExchangeResult:
        """兑换 device token 和 handoff token。

        关键输入：pairing ID、claim ID、nonce、设备 ID 和当前时间。
        关键输出：device token 明文和一次性 handoff token 明文。
        """
        pairing_id = _require_text(pairing_id, "pairing_id")
        claim_id = _require_text(claim_id, "claim_id")
        nonce = _require_text(nonce, "nonce")
        device_id = _require_text(device_id, "device_id")
        now = now or _utc_now()
        session = self._load_existing_session(pairing_id, now=now)
        if session.nonce_hash != _hash_nonce(nonce):
            raise errors.nonce_mismatch(pairing_id)
        claim = self._repository.get_claim(claim_id)
        if claim is None or claim.pairing_id != pairing_id or claim.device_id != device_id:
            raise errors.claim_not_found(pairing_id, claim_id)
        if claim.status == PairingClaimStatus.PENDING_APPROVAL:
            raise errors.approval_pending(pairing_id)
        if claim.status == PairingClaimStatus.DENIED:
            raise errors.approval_denied(pairing_id)
        if claim.status == PairingClaimStatus.EXCHANGED:
            raise errors.invalid_token("pairing already exchanged")
        if session.status == PairingSessionStatus.DENIED:
            raise errors.approval_denied(pairing_id)
        if session.status != PairingSessionStatus.APPROVED:
            raise errors.approval_pending(pairing_id)

        device = MobileDeviceDescriptor(
            device_id=claim.device_id,
            label=claim.label,
            platform=claim.platform,
            app_version=claim.app_version,
        )
        device_issue, handoff_issue = self._token_service.issue_exchange_tokens(
            pairing_id=pairing_id,
            claim_id=claim_id,
            device=device,
            scopes=session.requested_scopes,
            user_id=device.device_id,
            now=now,
        )
        return PairingExchangeResult(
            pairing_id=pairing_id,
            claim_id=claim_id,
            device_id=device.device_id,
            device_token=device_issue.device_token,
            handoff_token=handoff_issue.handoff_token,
            handoff_expires_at=handoff_issue.expires_at,
        )

    def revoke_device(self, device_id: str) -> None:
        """吊销移动设备。

        关键输入：设备 ID。
        关键输出：设备记录写入 ``revoked_at``。
        """
        device_id = _require_text(device_id, "device_id")
        self._repository.revoke_device(device_id)

    def _ensure_supported_protocol(self, protocol_version: str) -> None:
        """校验协议版本。

        关键输入：客户端协议版本。
        关键输出：支持时返回 ``None``，不支持时抛错误。
        """
        protocol_version = _require_text(protocol_version, "protocol_version")
        if protocol_version != _SUPPORTED_PROTOCOL_VERSION:
            raise errors.unsupported_protocol(protocol_version)

    def _load_open_session(
        self,
        pairing_id: str,
        *,
        now: datetime,
    ) -> PairingSessionRecord:
        """读取仍可 claim 的配对会话。

        关键输入：pairing ID 和当前时间。
        关键输出：未过期 session；失败时抛配对错误。
        """
        session = self._load_existing_session(pairing_id, now=now)
        if session.status != PairingSessionStatus.PENDING_SCAN:
            raise errors.pairing_already_claimed(pairing_id)
        return session

    def _load_existing_session(
        self,
        pairing_id: str,
        *,
        now: datetime,
    ) -> PairingSessionRecord:
        """读取存在且未过期的配对会话。

        关键输入：pairing ID 和当前时间。
        关键输出：session record；缺失或过期时抛错误。
        """
        session = self._repository.get_pairing_session(pairing_id)
        if session is None:
            raise errors.pairing_not_found(pairing_id)
        terminal_statuses = {
            PairingSessionStatus.DENIED,
            PairingSessionStatus.EXCHANGED,
        }
        if session.status in terminal_statuses:
            return session
        if session.expires_at <= now:
            self._repository.mark_pairing_expired(pairing_id)
            raise errors.pairing_expired(pairing_id)
        return session


__all__ = ["MobilePairingManager"]
