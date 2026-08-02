"""API key HTTP header 构造工具。

职责：
- ``build_api_key_headers``：按配置把 API key 放进指定 HTTP header。
- ``api_key_header_label``：把 header 配置投影成前端展示标签。
"""

from __future__ import annotations

from infrastructure.config.models import ApiKeyHeader

__all__ = ["api_key_header_label", "build_api_key_headers"]


def build_api_key_headers(*, api_key: str, api_key_header: ApiKeyHeader) -> dict[str, str]:
    """按 ``api_key_header`` 构造鉴权 header。

    Args:
        api_key: API key 明文。空字符串表示本地或无鉴权 endpoint。
        api_key_header: API key 写入哪个 HTTP header。

    Returns:
        可直接合并到 HTTP headers 的 dict。``api_key`` 为空时返回空 dict。
    """
    if not api_key:
        return {}
    if api_key_header == "authorization-bearer":
        return {"Authorization": f"Bearer {api_key}"}
    return {"x-api-key": api_key}


def api_key_header_label(api_key_header: ApiKeyHeader) -> str:
    """返回面向 UI 的简短鉴权标签。"""
    if api_key_header == "authorization-bearer":
        return "Bearer"
    return "x-api-key"
