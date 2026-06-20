"""XSpace Mobile 登录二维码 server origin 配置测试。

本脚本验证 ``ServerOriginConfig``，作用是固定公网 HTTPS 域名和局域网 HTTP 私网 IP
两类扫码登录入口的配置合同。关键流程是传入配置 origin，断言输出
``LoginQrOriginView`` 或稳定 ``MobilePairingError``。
"""

from __future__ import annotations

import pytest

from hosts.web.xspace_mobile import ServerOriginConfig, errors


def test_public_https_domain_origin_is_accepted() -> None:
    """公网 HTTPS 域名返回 public_https 视图。"""
    view = ServerOriginConfig().require_login_qr_origin("https://kongming.example.com/")

    assert view.mode == "public_https"
    assert view.origin == "https://kongming.example.com"
    assert view.scheme == "https"
    assert view.host == "kongming.example.com"
    assert view.port is None


def test_lan_http_private_ip_origin_is_accepted() -> None:
    """局域网 HTTP 私网 IP 返回 lan_ip 视图。"""
    view = ServerOriginConfig().require_login_qr_origin("http://192.168.31.23:8765")

    assert view.mode == "lan_ip"
    assert view.origin == "http://192.168.31.23:8765"
    assert view.scheme == "http"
    assert view.host == "192.168.31.23"
    assert view.port == 8765


@pytest.mark.parametrize(
    ("origin", "code"),
    [
        (None, "server_origin_required"),
        ("", "server_origin_required"),
        ("ftp://kongming.example.com", "server_origin_invalid_scheme"),
        ("https://kongming.example.com/login", "server_origin_invalid_scheme"),
        ("http://127.0.0.1:8765", "server_origin_loopback"),
        ("https://localhost:8765", "server_origin_loopback"),
        ("http://8.8.8.8:8765", "server_origin_not_lan_ip"),
        ("http://kongming.example.com", "server_origin_not_lan_ip"),
        ("https://192.168.31.23:8765", "server_origin_public_host_invalid"),
        ("https://kongming", "server_origin_public_host_invalid"),
    ],
)
def test_invalid_origin_returns_stable_error(origin: str | None, code: str) -> None:
    """错误配置映射到稳定公开错误码。"""
    with pytest.raises(errors.MobilePairingError) as exc_info:
        ServerOriginConfig().require_login_qr_origin(origin)

    assert exc_info.value.code == code
    assert errors.mobile_pairing_http_status(exc_info.value) == 400
