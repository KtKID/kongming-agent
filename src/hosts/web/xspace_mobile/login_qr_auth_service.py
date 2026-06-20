"""XSpace Mobile 登录二维码授权服务。

本脚本实现 ``LoginQrAuthService``，作用是为登录二维码确认阶段复用 Web 登录密码
校验和限流。关键流程是按客户端 IP 检查限流，使用 ``verify_password`` 校验密码，
失败记录失败次数，成功清理失败计数并调用 ``LoginQrManager.confirm_login_qr`` 写入
授权证据。关键类职责：AuthService 只负责授权前置，token 与 handoff 签发仍由
``LoginQrManager.exchange_login_qr`` 统一触发。
"""

from __future__ import annotations

from datetime import datetime

from hosts.web.auth.secrets import verify_password
from hosts.web.rate_limit import LoginRateLimiter
from hosts.web.rate_limit import RateLimitedError as _RateLimitedError
from hosts.web.xspace_mobile import errors
from hosts.web.xspace_mobile.login_qr_manager import LoginQrManager
from hosts.web.xspace_mobile.models import LoginQrConfirmResult


class LoginQrAuthService:
    """登录二维码授权服务门户。

    职责：复用登录密码和限流能力，确认扫码登录授权证据。
    关键输入：``LoginQrManager`` 和 ``LoginRateLimiter``。
    关键输出：confirmed DTO 或稳定移动端错误。
    """

    def __init__(
        self,
        *,
        manager: LoginQrManager,
        rate_limiter: LoginRateLimiter,
    ) -> None:
        """初始化授权服务。

        关键输入：登录二维码 Manager 和登录限流器。
        关键输出：可执行密码确认的 AuthService 实例。
        """
        self._manager = manager
        self._rate_limiter = rate_limiter

    async def confirm_with_password(
        self,
        *,
        login_qr_id: str,
        claim_id: str,
        browser_token: str,
        password: str,
        password_hash: str,
        client_ip: str,
        now: datetime | None = None,
    ) -> LoginQrConfirmResult:
        """使用 Web 登录密码确认扫码登录。

        关键输入：登录二维码 ID、claim ID、browser token、明文密码、密码 hash 和客户端 IP。
        关键输出：``LoginQrConfirmResult``；密码或限流失败时抛 ``MobilePairingError``。
        """
        try:
            await self._rate_limiter.check(client_ip)
        except _RateLimitedError as exc:
            raise errors.rate_limited(exc.retry_after_seconds) from exc

        if not verify_password(password, password_hash):
            await self._rate_limiter.record_failure(client_ip)
            raise errors.invalid_credentials()

        await self._rate_limiter.record_success(client_ip)
        return self._manager.confirm_login_qr(
            login_qr_id=login_qr_id,
            claim_id=claim_id,
            browser_token=browser_token,
            authorization_method="password",
            authorized_user_id="default",
            now=now,
        )


__all__ = ["LoginQrAuthService"]
