"""联网正文读取 builtin tool。

本模块提供 `web_fetch` 原子工具：输入 URL，内部完成 SSRF 防护、HTTP 请求、
redirect 前安全复查、正文抽取、垃圾页识别、关键词窗口和分页，输出结构化
ToolResult。调用方只依赖 Tool 入口，模块内 helper 保持私有。
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, ClassVar, cast
from urllib.parse import urlparse

import httpcore
import httpx
import trafilatura
from httpcore._backends.anyio import AnyIOBackend
from httpcore._backends.base import SOCKET_OPTION, AsyncNetworkBackend, AsyncNetworkStream

from core.contracts import PreparedToolCall, Tool, ToolContext, ToolResult

# 正文进入模型上下文的 token 粗略预算，后续按 eval 结果调参。
_FETCH_TOKEN_BUDGET = 4000
# token 到字符的粗略换算比例，用于预算截断。
_CHAR_PER_TOKEN = 4
# HTTP 单次请求超时秒数，控制外部页面卡顿时的等待上限。
_HTTP_TIMEOUT_S = 10.0
# 请求网页时发送的 User-Agent，便于站点日志识别 Kongming 抓取来源。
_USER_AGENT = "kongming-agent/1.0 (+research)"
# 是否允许访问私网、loopback、link-local、reserved 地址，默认关闭。
_ALLOW_PRIVATE_NETWORK = False
# 抽取正文短于该字符数时判定为疑似登录墙、订阅墙或 JS 壳。
_JUNK_MIN_CHARS = 200
# 命中 query 关键词时截取的前后窗口字符数。
_KEYWORD_WINDOW_CHARS = 300
# 关键词窗口相邻距离小于该字符数时合并，减少重复上下文。
_KEYWORD_WINDOW_MERGE_GAP_CHARS = 50
# redirect 最多跟随次数，防止跳转环拖住 tool call。
_MAX_REDIRECTS = 5
# offset 字符串最多允许的十进制位数，避免超长数字触发 Python int 保护异常。
_OFFSET_MAX_DIGITS = 12
# 可作为正文抽取输入的响应媒体类型；空 content-type 会继续按正文尝试。
_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "text/markdown",
        "application/xml",
        "text/xml",
    }
)

_WALL_SIGNS = ("请登录", "订阅后阅读", "sign in to", "subscribe to continue", "enable javascript")
_AddressResolver = Callable[[str], tuple[str, ...]]


@dataclass(frozen=True)
class _DownloadedPage:
    """下载成功后的页面快照，输入为最终 URL 和 HTML，输出给抽取流程。"""

    url: str
    html: str


@dataclass(frozen=True)
class _FetchPayload:
    """fetch 结构化结果，输入为状态字段，输出给 ToolResult。"""

    status: str
    url: str
    content: str = ""
    total_chars: int = 0
    has_more: bool = False
    next_offset: int | None = None
    reason: str | None = None
    suggestion: str | None = None

    def to_data(self) -> dict[str, Any]:
        """转换为 ToolResult.data，输入内部 payload，输出结构化 dict。"""
        data: dict[str, Any] = {"status": self.status, "url": self.url}
        if self.status == "ok":
            data.update(
                {
                    "content": self.content,
                    "content_text": self.content,
                    "total_chars": self.total_chars,
                    "has_more": self.has_more,
                    "next_offset": self.next_offset,
                }
            )
        if self.reason is not None:
            data["reason"] = self.reason
        if self.suggestion is not None:
            data["suggestion"] = self.suggestion
        return data


class _SafeAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """绑定安全 DNS 解析结果的 HTTP transport。"""

    def __init__(self, *, resolver: _AddressResolver) -> None:
        """初始化安全 transport，输入 resolver，输出禁代理的连接池。"""
        super().__init__(trust_env=False)
        self._pool._network_backend = _SafeNetworkBackend(resolver=resolver)


class _SafeNetworkBackend(AsyncNetworkBackend):
    """httpcore network backend：实际 TCP 连接使用安全校验后的 IP。"""

    def __init__(
        self,
        *,
        resolver: _AddressResolver,
        delegate: AsyncNetworkBackend | None = None,
    ) -> None:
        """初始化 network backend，输入 resolver/delegate，输出可连接实例。"""
        self._resolver = resolver
        self._delegate = delegate or AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        """连接 TCP，输入原始 host，输出连接到安全 IP 的 stream。"""
        safe, reason, addresses = _safe_resolved_addresses(host, resolver=self._resolver)
        if not safe:
            raise httpcore.ConnectError(f"safe_network_blocked:{reason}")
        if not addresses:
            raise httpcore.ConnectError("safe_network_blocked:dns_fail")
        try:
            return await self._delegate.connect_tcp(
                addresses[0],
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )
        except OSError as exc:
            raise httpcore.ConnectError(str(exc)) from exc

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        """连接 Unix socket，输入 path，输出 delegate stream。"""
        return await self._delegate.connect_unix_socket(
            path,
            timeout=timeout,
            socket_options=socket_options,
        )

    async def sleep(self, seconds: float) -> None:
        """休眠，输入秒数，输出 None。"""
        await self._delegate.sleep(seconds)


class WebFetchTool:
    """把 URL 正文读取能力暴露为 Kongming Tool。"""

    name = "web_fetch"
    description = (
        "Fetch a URL, extract readable markdown content, and return a paginated structured result."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "HTTP or HTTPS URL to fetch."},
            "query": {
                "type": "string",
                "description": "Optional question/query used to focus long pages.",
            },
            "offset": {
                "anyOf": [
                    {"type": "integer", "minimum": 0},
                    {"type": "string", "pattern": f"^[0-9]{{1,{_OFFSET_MAX_DIGITS}}}$"},
                ],
                "description": "Character offset for paginated fetch continuation.",
            },
        },
        "required": ["url"],
    }

    def __init__(self, *, engine: _WebFetchEngine | None = None) -> None:
        """初始化 Tool，输入可选内部 engine，输出可注册实例。"""
        self._engine = engine or _WebFetchEngine()

    def prepare(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> PreparedToolCall:
        """审批前校验 URL/offset 并冻结规范化参数。"""
        del context
        url = _optional_str(arguments.get("url"))
        if url is None:
            raise ValueError("web_fetch requires a non-empty URL")
        offset = _parse_non_negative_int(arguments.get("offset", 0))
        if offset is None:
            raise ValueError("web_fetch offset must be an integer >= 0")
        return PreparedToolCall(
            arguments={
                "url": url,
                "query": _optional_str(arguments.get("query")),
                "offset": offset,
            }
        )

    async def execute(
        self,
        prepared: PreparedToolCall,
        ctx: ToolContext,
    ) -> ToolResult:
        """执行 web_fetch，输入已准备调用/context，输出 ToolResult。"""
        del ctx
        url = str(prepared.arguments["url"])
        offset = int(prepared.arguments["offset"])
        raw_query = prepared.arguments.get("query")
        query = raw_query if isinstance(raw_query, str) else None
        payload = await self._engine.fetch(url, query=query, offset=offset)
        if payload.status != "ok":
            return _failed_tool_result(payload)
        return ToolResult(
            ok=True,
            content=payload.content,
            data=payload.to_data(),
        )


class _WebFetchEngine:
    """web_fetch 内部编排器，封装安全、HTTP、抽取和分页。"""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: _AddressResolver | None = None,
    ) -> None:
        """初始化内部 engine，输入可选 transport/resolver，输出可复用实例。"""
        self._transport = transport
        self._resolver = resolver or _resolve_host_addresses

    async def fetch(self, url: str, *, query: str | None, offset: int) -> _FetchPayload:
        """读取 URL 正文，输入 URL/query/offset，输出结构化 payload。"""
        safe, reason = _is_safe_url(url, resolver=self._resolver)
        if not safe:
            if reason == "invalid_url":
                return _invalid_url_payload(url=url)
            return _blocked_payload(url=url, reason=reason)

        downloaded = await self._download(url)
        if isinstance(downloaded, _FetchPayload):
            return downloaded

        try:
            content = (
                trafilatura.extract(
                    downloaded.html,
                    output_format="markdown",
                    include_comments=False,
                )
                or ""
            )
        except Exception as exc:
            return _error_payload(
                url=downloaded.url,
                reason=f"extract:{type(exc).__name__}",
                suggestion="try another source",
            )
        if _looks_like_junk(content):
            return _blocked_payload(
                url=downloaded.url,
                reason="paywall_or_js_shell",
                suggestion="pick another source",
            )

        if query and len(content) // _CHAR_PER_TOKEN > _FETCH_TOKEN_BUDGET:
            picked = _keyword_windows(content, query)
            if picked:
                content = picked

        budget = _FETCH_TOKEN_BUDGET * _CHAR_PER_TOKEN
        chunk = content[offset : offset + budget]
        has_more = offset + budget < len(content)
        return _FetchPayload(
            status="ok",
            url=downloaded.url,
            content=chunk,
            total_chars=len(content),
            has_more=has_more,
            next_offset=offset + len(chunk) if has_more else None,
        )

    async def _download(self, url: str) -> _DownloadedPage | _FetchPayload:
        """下载页面并处理 redirect，输入起始 URL，输出页面快照或失败 payload。"""
        headers = {"User-Agent": _USER_AGENT}
        transport = self._transport or _SafeAsyncHTTPTransport(resolver=self._resolver)
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT_S,
            headers=headers,
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        ) as client:
            current_url = url
            for _redirect_count in range(_MAX_REDIRECTS + 1):
                try:
                    response = await client.get(current_url)
                except (httpx.InvalidURL, ValueError):
                    return _invalid_url_payload(url=current_url)
                except httpx.HTTPError as exc:
                    blocked_reason = _safe_network_blocked_reason(exc)
                    if blocked_reason is not None:
                        return _blocked_payload(url=current_url, reason=blocked_reason)
                    return _error_payload(
                        url=current_url,
                        reason=f"http:{type(exc).__name__}",
                        suggestion="try another source",
                    )

                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return _error_payload(
                            url=current_url,
                            reason="redirect_missing_location",
                            suggestion="try another source",
                        )
                    try:
                        next_url = str(response.url.join(location))
                        safe, reason = _is_safe_url(next_url, resolver=self._resolver)
                    except (httpx.InvalidURL, ValueError):
                        return _invalid_url_payload(url=current_url)
                    if not safe:
                        if reason == "invalid_url":
                            return _invalid_url_payload(url=next_url)
                        return _blocked_payload(url=next_url, reason=reason)
                    current_url = next_url
                    continue

                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    return _error_payload(
                        url=str(response.url),
                        reason=f"http:{type(exc).__name__}",
                        suggestion="try another source",
                    )
                unsupported_reason = _unsupported_content_type_reason(response)
                if unsupported_reason is not None:
                    return _error_payload(
                        url=str(response.url),
                        reason=unsupported_reason,
                        suggestion="try another source",
                    )
                return _DownloadedPage(url=str(response.url), html=response.text)

        return _error_payload(
            url=url,
            reason="too_many_redirects",
            suggestion="try another source",
        )


def build_web_fetch_tool(*, enabled: bool = True) -> list[Tool]:
    """构造 web_fetch 工具列表，输入开关，输出可注册 Tool 列表。"""
    if not enabled:
        return []
    return [cast(Tool, WebFetchTool())]


def _failed_tool_result(payload: _FetchPayload) -> ToolResult:
    """构造失败 ToolResult，输入 payload，输出结构化失败结果。"""
    content = f"web_fetch {payload.status}: {payload.reason or 'unknown'}"
    if payload.suggestion:
        content = f"{content}. suggestion: {payload.suggestion}"
    return ToolResult(
        ok=False,
        content=content,
        data=payload.to_data(),
        error_message=payload.reason,
    )


def _blocked_payload(
    *,
    url: str,
    reason: str,
    suggestion: str = "pick another source",
) -> _FetchPayload:
    """构造阻断 payload，输入 URL/reason，输出 blocked 状态。"""
    return _FetchPayload(status="blocked", url=url, reason=reason, suggestion=suggestion)


def _error_payload(*, url: str, reason: str, suggestion: str) -> _FetchPayload:
    """构造错误 payload，输入 URL/reason/suggestion，输出 error 状态。"""
    return _FetchPayload(status="error", url=url, reason=reason, suggestion=suggestion)


def _invalid_url_payload(*, url: str) -> _FetchPayload:
    """构造非法 URL payload，输入 URL，输出 error 状态。"""
    return _error_payload(
        url=url,
        reason="invalid_url",
        suggestion="provide a valid http or https URL",
    )


def _is_safe_url(url: str, *, resolver: _AddressResolver) -> tuple[bool, str]:
    """检查 URL 是否可请求，输入 URL，输出安全布尔值和原因。"""
    safe, reason, _addresses = _safe_url_parts(url, resolver=resolver)
    return safe, reason


def _safe_url_parts(
    url: str,
    *,
    resolver: _AddressResolver,
) -> tuple[bool, str, tuple[str, ...]]:
    """检查 URL 并返回安全 IP，输入 URL，输出状态、原因和 IP 元组。"""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        _port = parsed.port
    except ValueError:
        return False, "invalid_url", ()
    if parsed.scheme not in ("http", "https"):
        return False, f"scheme_blocked:{parsed.scheme}", ()
    if not hostname:
        return False, "no_host", ()
    return _safe_resolved_addresses(hostname, resolver=resolver)


def _safe_resolved_addresses(
    hostname: str,
    *,
    resolver: _AddressResolver,
) -> tuple[bool, str, tuple[str, ...]]:
    """解析并校验 hostname，输入主机名，输出状态、原因和安全 IP。"""
    try:
        addresses = resolver(hostname)
    except socket.gaierror:
        return False, "dns_fail", ()
    except OSError:
        return False, "dns_fail", ()
    safe_addresses: list[str] = []
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            return False, f"ip_parse_fail:{address}", ()
        if not _ALLOW_PRIVATE_NETWORK and (
            ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        ):
            return False, f"internal_ip_blocked:{ip}", ()
        safe_addresses.append(address)
    return True, "ok", tuple(safe_addresses)


def _resolve_host_addresses(hostname: str) -> tuple[str, ...]:
    """解析 hostname，输入主机名，输出真实 IP 字符串元组。"""
    infos = socket.getaddrinfo(hostname, None)
    return tuple(str(info[4][0]) for info in infos)


def _unsupported_content_type_reason(response: httpx.Response) -> str | None:
    """检查响应媒体类型，输入 HTTP 响应，输出不支持原因或 None。"""
    media_type = _response_media_type(response.headers.get("content-type"))
    if media_type is None:
        return None
    if media_type in _ALLOWED_CONTENT_TYPES or media_type.endswith("+xml"):
        return None
    return f"unsupported_content_type:{media_type}"


def _response_media_type(content_type: str | None) -> str | None:
    """解析 content-type 媒体类型，输入响应头，输出小写媒体类型或 None。"""
    if content_type is None:
        return None
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type or None


def _safe_network_blocked_reason(exc: httpx.HTTPError) -> str | None:
    """从连接异常提取安全阻断原因，输入 HTTP 异常，输出 reason 或 None。"""
    message = str(exc)
    marker = "safe_network_blocked:"
    if marker not in message:
        return None
    return message.split(marker, 1)[1].split("'", 1)[0].strip()


def _looks_like_junk(text: str) -> bool:
    """识别低质量正文，输入抽取文本，输出是否应阻断。"""
    if len(text.strip()) < _JUNK_MIN_CHARS:
        return True
    lowered = text.lower()
    return any(sign in text or sign in lowered for sign in _WALL_SIGNS)


def _keyword_windows(text: str, query: str) -> str:
    """按 query 关键词截取上下文窗口，输入正文和查询，输出合并片段。"""
    terms = [term for term in query.split() if len(term) >= 2]
    lowered = text.lower()
    spans: list[tuple[int, int]] = []
    for term in terms:
        start = 0
        lowered_term = term.lower()
        while True:
            index = lowered.find(lowered_term, start)
            if index == -1:
                break
            spans.append(
                (
                    max(0, index - _KEYWORD_WINDOW_CHARS),
                    min(len(text), index + len(term) + _KEYWORD_WINDOW_CHARS),
                )
            )
            start = index + len(term)
    if not spans:
        return ""
    spans.sort()
    merged = [spans[0]]
    for start, end in spans[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end + _KEYWORD_WINDOW_MERGE_GAP_CHARS:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return "\n...\n".join(text[start:end] for start, end in merged)


def _parse_non_negative_int(value: object) -> int | None:
    """解析非负整数，输入任意参数值，输出 int 或 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped.isdecimal():
            return None
        if len(stripped) > _OFFSET_MAX_DIGITS:
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def _optional_str(value: object) -> str | None:
    """把可选参数转成字符串，输入任意值，输出非空字符串或 None。"""
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


__all__ = ["WebFetchTool", "build_web_fetch_tool"]
