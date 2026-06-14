"""XSpace Mobile device token 与 handoff token 服务。

本脚本实现 ``MobileDeviceTokenService``，作用是生成 opaque 随机 token、保存
SHA-256 hash、校验设备吊销状态，并签发/消费一次性 handoff token。关键流程是
exchange 阶段签发 ``kgm_dt_`` device token，冷启动阶段用 Bearer device token
换取 handoff token，WebView 打开 consume URL 后单次消费 handoff 并返回登录上下文。
关键类/函数职责：TokenService 是 token 能力门户，hash 函数保证明文不落盘。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from hosts.web.xspace_mobile import errors
from hosts.web.xspace_mobile.models import (
    DeviceTokenIssueResult,
    HandoffIssueResult,
    HandoffLoginContext,
    HandoffTokenRecord,
    MobileDeviceDescriptor,
    MobileDeviceRecord,
)
from hosts.web.xspace_mobile.repository import MobilePairingRepository

_DEVICE_TOKEN_PREFIX = "kgm_dt_"
_HANDOFF_TOKEN_PREFIX = "kgm_ht_"
_HANDOFF_TTL_SECONDS = 60


def _utc_now() -> datetime:
    """生成当前 UTC 时间。

    关键输入：系统时钟。
    关键输出：带 UTC 时区的 ``datetime``。
    """
    return datetime.now(UTC)


def hash_token(token: str) -> str:
    """计算 token 的 SHA-256 hash。

    关键输入：token 明文。
    关键输出：十六进制 SHA-256 摘要。
    """
    if not isinstance(token, str) or not token:
        raise errors.invalid_token("invalid token")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _opaque_token(prefix: str) -> str:
    """生成 URL 安全 opaque token。

    关键输入：业务前缀。
    关键输出：带前缀的随机 token 明文。
    """
    return f"{prefix}{secrets.token_urlsafe(32)}"


def _require_token(value: str, *, prefix: str, message: str) -> str:
    """校验公开 token 输入。

    关键输入：调用方传入的 token、期望前缀和错误说明。
    关键输出：合法 token 字符串；非法时抛稳定错误。
    """
    if not isinstance(value, str) or not value.startswith(prefix):
        raise errors.invalid_token(message)
    return value


def _require_positive_ttl(ttl_seconds: int) -> None:
    """校验 token TTL。

    关键输入：TTL 秒数。
    关键输出：正整数通过；非法时抛稳定错误。
    """
    if type(ttl_seconds) is not int or ttl_seconds <= 0:
        raise errors.invalid_request("ttl_seconds must be positive")


def _require_device(value: MobileDeviceDescriptor) -> MobileDeviceDescriptor:
    """校验公开设备输入。

    关键输入：调用方传入的设备 DTO。
    关键输出：合法设备 DTO；非法时抛稳定错误。
    """
    if not isinstance(value, MobileDeviceDescriptor):
        raise errors.invalid_request("device is required")
    if not value.device_id or not value.label or not value.app_version:
        raise errors.invalid_request("device fields are required")
    return value


class MobileDeviceTokenService:
    """移动设备 token 服务门户。

    职责：签发 device token、校验 token hash、签发和消费 handoff token。
    关键输入：``MobilePairingRepository``。
    关键输出：token 结果 DTO、设备记录和登录上下文。
    """

    def __init__(self, repository: MobilePairingRepository) -> None:
        """初始化 token 服务。

        关键输入：移动配对 repository。
        关键输出：可签发和校验 token 的服务实例。
        """
        self._repository = repository

    def issue_device_token(
        self,
        *,
        device: MobileDeviceDescriptor,
        scopes: list[str],
        now: datetime | None = None,
    ) -> DeviceTokenIssueResult:
        """签发 device token 并保存 hash。

        关键输入：设备描述、授权 scope 和当前时间。
        关键输出：只返回一次的 device token 明文和持久化设备记录。
        """
        device = _require_device(device)
        now = now or _utc_now()
        token = _opaque_token(_DEVICE_TOKEN_PREFIX)
        record = MobileDeviceRecord(
            device_id=device.device_id,
            label=device.label,
            platform=device.platform,
            app_version=device.app_version,
            scopes=list(scopes),
            token_hash=hash_token(token),
            created_at=now,
            last_seen_at=now,
            revoked_at=None,
        )
        stored = self._repository.upsert_device(record)
        return DeviceTokenIssueResult(device_token=token, device=stored)

    def validate_device_token(self, device_token: str) -> MobileDeviceRecord:
        """校验 device token 并返回设备记录。

        关键输入：Bearer device token 明文。
        关键输出：未吊销的设备记录。
        """
        device_token = _require_token(
            device_token,
            prefix=_DEVICE_TOKEN_PREFIX,
            message="invalid device token prefix",
        )
        device = self._repository.get_device_by_token_hash(hash_token(device_token))
        if device is None:
            raise errors.invalid_token("invalid device token")
        if device.revoked_at is not None:
            raise errors.device_revoked(device.device_id)
        return device

    def issue_handoff_token(
        self,
        *,
        device_id: str,
        scopes: list[str],
        user_id: str = "mobile-device",
        ttl_seconds: int = _HANDOFF_TTL_SECONDS,
        now: datetime | None = None,
    ) -> HandoffIssueResult:
        """签发一次性 handoff token。

        关键输入：设备 ID、scope、用户 ID、TTL 和当前时间。
        关键输出：handoff token 明文、记录 ID 和过期时间。
        """
        _require_positive_ttl(ttl_seconds)
        now = now or _utc_now()
        token = _opaque_token(_HANDOFF_TOKEN_PREFIX)
        handoff_id = f"ho_{secrets.token_urlsafe(18)}"
        expires_at = now + timedelta(seconds=ttl_seconds)
        record = HandoffTokenRecord(
            handoff_id=handoff_id,
            token_hash=hash_token(token),
            device_id=device_id,
            scopes=list(scopes),
            user_id=user_id,
            expires_at=expires_at,
            consumed_at=None,
            created_at=now,
        )
        self._repository.create_handoff_token(record)
        return HandoffIssueResult(
            handoff_token=token,
            handoff_id=handoff_id,
            expires_at=expires_at,
        )

    def consume_handoff_token(
        self,
        handoff_token: str,
        *,
        now: datetime | None = None,
    ) -> HandoffLoginContext:
        """消费一次性 handoff token。

        关键输入：handoff token 明文和当前时间。
        关键输出：Router 设置 cookie 所需登录上下文。
        """
        handoff_token = _require_token(
            handoff_token,
            prefix=_HANDOFF_TOKEN_PREFIX,
            message="invalid handoff token prefix",
        )
        return self._repository.consume_handoff_by_hash(
            hash_token(handoff_token),
            now=now or _utc_now(),
        )

    def issue_exchange_tokens(
        self,
        *,
        pairing_id: str,
        claim_id: str,
        device: MobileDeviceDescriptor,
        scopes: list[str],
        user_id: str,
        ttl_seconds: int = _HANDOFF_TTL_SECONDS,
        now: datetime | None = None,
    ) -> tuple[DeviceTokenIssueResult, HandoffIssueResult]:
        """为 exchange 原子签发 device token 和 handoff token。

        关键输入：pairing/claim、设备描述、scope、用户 ID、TTL 和当前时间。
        关键输出：明文 token DTO；数据库写入由 repository 单事务完成。
        """
        device = _require_device(device)
        _require_positive_ttl(ttl_seconds)
        now = now or _utc_now()
        device_token = _opaque_token(_DEVICE_TOKEN_PREFIX)
        handoff_token = _opaque_token(_HANDOFF_TOKEN_PREFIX)
        handoff_id = f"ho_{secrets.token_urlsafe(18)}"
        expires_at = now + timedelta(seconds=ttl_seconds)
        device_record = MobileDeviceRecord(
            device_id=device.device_id,
            label=device.label,
            platform=device.platform,
            app_version=device.app_version,
            scopes=list(scopes),
            token_hash=hash_token(device_token),
            created_at=now,
            last_seen_at=now,
            revoked_at=None,
        )
        handoff_record = HandoffTokenRecord(
            handoff_id=handoff_id,
            token_hash=hash_token(handoff_token),
            device_id=device.device_id,
            scopes=list(scopes),
            user_id=user_id,
            expires_at=expires_at,
            consumed_at=None,
            created_at=now,
        )
        stored_device, stored_handoff = self._repository.complete_exchange(
            pairing_id=pairing_id,
            claim_id=claim_id,
            device=device_record,
            handoff=handoff_record,
        )
        return (
            DeviceTokenIssueResult(device_token=device_token, device=stored_device),
            HandoffIssueResult(
                handoff_token=handoff_token,
                handoff_id=stored_handoff.handoff_id,
                expires_at=stored_handoff.expires_at,
            ),
        )

    def issue_handoff_for_device_token(
        self,
        device_token: str,
        *,
        user_id: str = "mobile-device",
        ttl_seconds: int = _HANDOFF_TTL_SECONDS,
        now: datetime | None = None,
    ) -> HandoffIssueResult:
        """用 device token 换取一次性 handoff token。

        关键输入：Bearer device token、用户 ID、TTL 和当前时间。
        关键输出：新的 handoff token 签发结果。
        """
        now = now or _utc_now()
        device = self.validate_device_token(device_token)
        self._repository.touch_device(device.device_id, now=now)
        return self.issue_handoff_token(
            device_id=device.device_id,
            scopes=device.scopes,
            user_id=user_id,
            ttl_seconds=ttl_seconds,
            now=now,
        )


__all__ = ["MobileDeviceTokenService", "hash_token"]
