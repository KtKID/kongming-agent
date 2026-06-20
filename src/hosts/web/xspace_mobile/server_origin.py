"""XSpace 扫码登录 server origin 校验门户。

本脚本实现 ``ServerOriginConfig``，作用是把 Web 配置中的服务器访问地址
归一化为登录 QR 可直接使用的 origin 视图。关键流程是读取单一 origin 字符串，
按公网 HTTPS 域名或局域网 HTTP 私网 IP 两种模式校验，输出 ``LoginQrOriginView``。
关键类职责：``ServerOriginConfig`` 是对外门户，``LoginQrOriginView`` 是 Router、
Manager 和前端共享的只读 DTO。
"""

from __future__ import annotations

import ipaddress
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

from hosts.web.xspace_mobile import errors

_LAN_NETWORKS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)


class LoginQrOriginView(BaseModel):
    """登录 QR server origin 视图。

    关键输入：已校验的 mode、origin、scheme、host 和端口。
    关键输出：Router response、QR payload 和 Manager 状态快照可共用的 DTO。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["public_https", "lan_ip"]
    origin: str
    scheme: Literal["https", "http"]
    host: str
    port: int | None = None


class ServerOriginConfig:
    """扫码登录 server origin 校验门户。"""

    def require_login_qr_origin(self, raw_origin: str | None) -> LoginQrOriginView:
        """返回可用于登录 QR 的规范化 origin。

        关键输入：配置或运行参数中的 origin 字符串。
        关键输出：``LoginQrOriginView``；配置错误时抛稳定 ``MobilePairingError``。
        """
        if raw_origin is None or not raw_origin.strip():
            raise errors.server_origin_required()
        origin = raw_origin.strip().rstrip("/")
        parsed = urlparse(origin)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        if not scheme or not host:
            raise errors.server_origin_required()
        if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
            raise errors.server_origin_invalid_scheme(scheme)
        try:
            port = parsed.port
        except ValueError as exc:
            raise errors.server_origin_invalid_scheme(scheme) from exc
        if scheme == "https":
            return self._require_public_https(origin, host, port)
        if scheme == "http":
            return self._require_lan_ip(origin, host, port)
        raise errors.server_origin_invalid_scheme(scheme)

    def _require_public_https(
        self,
        origin: str,
        host: str,
        port: int | None,
    ) -> LoginQrOriginView:
        """校验公网 HTTPS 域名模式。"""
        if _is_loopback_or_local(host):
            raise errors.server_origin_loopback(host)
        if _parse_ip(host) is not None:
            raise errors.server_origin_public_host_invalid(host)
        if "." not in host or any(not part for part in host.split(".")):
            raise errors.server_origin_public_host_invalid(host)
        return LoginQrOriginView(
            mode="public_https",
            origin=origin,
            scheme="https",
            host=host,
            port=port,
        )

    def _require_lan_ip(
        self,
        origin: str,
        host: str,
        port: int | None,
    ) -> LoginQrOriginView:
        """校验局域网 HTTP 私网 IP 模式。"""
        if _is_loopback_or_local(host):
            raise errors.server_origin_loopback(host)
        ip = _parse_ip(host)
        if not isinstance(ip, ipaddress.IPv4Address):
            raise errors.server_origin_not_lan_ip(host)
        if not any(ip in network for network in _LAN_NETWORKS):
            raise errors.server_origin_not_lan_ip(host)
        return LoginQrOriginView(
            mode="lan_ip",
            origin=origin,
            scheme="http",
            host=host,
            port=port,
        )


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """解析 host 为 IP 地址，域名返回 None。"""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_loopback_or_local(host: str) -> bool:
    """判断 host 是否属于本机、链路本地或未指定地址。"""
    if host in {"localhost", "0.0.0.0"}:
        return True
    ip = _parse_ip(host)
    if ip is None:
        return False
    return ip.is_loopback or ip.is_unspecified or ip.is_link_local


__all__ = ["LoginQrOriginView", "ServerOriginConfig"]
