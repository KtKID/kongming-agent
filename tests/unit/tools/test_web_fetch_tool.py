"""web_fetch builtin tool 单元测试。

本脚本验证 URL 安全校验、redirect 拦截、HTTP 错误、垃圾页识别、关键词窗口、
分页和默认注册。关键执行流程：用 httpx.MockTransport 固定 HTTP 响应，用可注入
resolver 固定 DNS 解析结果，再通过 WebFetchTool.execute 断言 ToolResult 契约。
"""

from __future__ import annotations

from collections.abc import Callable

import httpcore
import httpx
import pytest

import tools.builtin.web_fetch_tool as web_fetch_tool
from core.contracts import ToolContext
from tests.support.tool_calls import execute_prepared_tool
from tools import build_default_registry, builtin
from tools.builtin.web_fetch_tool import (
    WebFetchTool,
    _SafeNetworkBackend,
    _WebFetchEngine,
    build_web_fetch_tool,
)


def _ctx() -> ToolContext:
    """构造测试 ToolContext，输入为空，输出固定上下文。"""
    return ToolContext(run_id="r", session_id="s", turn=1, call_id="c")


def _public_resolver(_hostname: str) -> tuple[str, ...]:
    """模拟公网 DNS 解析，输入 hostname，输出公网 IP。"""
    return ("8.8.8.8",)


def _tool(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    resolver: Callable[[str], tuple[str, ...]] = _public_resolver,
) -> WebFetchTool:
    """构造带 mock transport 的 web_fetch，输入 HTTP handler，输出 Tool。"""
    transport = httpx.MockTransport(handler)
    engine = _WebFetchEngine(transport=transport, resolver=resolver)
    return WebFetchTool(engine=engine)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_web_fetch_returns_paginated_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证成功路径分页，输入 HTML 和抽取正文，输出 ok + content/has_more。"""
    monkeypatch.setattr(web_fetch_tool, "_FETCH_TOKEN_BUDGET", 5)
    monkeypatch.setattr(web_fetch_tool, "_CHAR_PER_TOKEN", 4)
    monkeypatch.setattr(web_fetch_tool, "_JUNK_MIN_CHARS", 1)
    content = "0123456789" * 6
    monkeypatch.setattr(
        web_fetch_tool.trafilatura,
        "extract",
        lambda html, output_format, include_comments: content,
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>ok</html>", request=request)

    result = await execute_prepared_tool(
        _tool(_handler), {"url": "https://example.test/article"}, _ctx()
    )

    assert result.ok is True
    assert result.content == content[:20]
    assert result.data is not None
    assert result.data["status"] == "ok"
    assert result.data["url"] == "https://example.test/article"
    assert result.data["content"] == content[:20]
    assert result.data["content_text"] == content[:20]
    assert result.data["total_chars"] == len(content)
    assert result.data["has_more"] is True
    assert result.data["next_offset"] == 20


@pytest.mark.asyncio
@pytest.mark.unit
async def test_web_fetch_blocks_private_network_before_request() -> None:
    """验证请求前 SSRF 拦截，输入私网解析结果，输出 blocked。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"blocked URL should not be requested: {request.url}")

    tool = _tool(_handler, resolver=lambda _hostname: ("127.0.0.1",))

    result = await execute_prepared_tool(tool, {"url": "http://localhost/metadata"}, _ctx())

    assert result.ok is False
    assert result.error_message == "internal_ip_blocked:127.0.0.1"
    assert result.data is not None
    assert result.data["status"] == "blocked"
    assert result.data["reason"] == "internal_ip_blocked:127.0.0.1"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_web_fetch_blocks_redirect_target_before_following() -> None:
    """验证 redirect 目标安全复查，输入跳转到 loopback，输出 blocked。"""
    requested_urls: list[str] = []

    def _resolver(hostname: str) -> tuple[str, ...]:
        if hostname == "start.test":
            return ("8.8.8.8",)
        return ("127.0.0.1",)

    def _handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if request.url.host == "start.test":
            return httpx.Response(
                302,
                headers={"location": "http://127.0.0.1/secret"},
                request=request,
            )
        raise AssertionError(f"redirect target should be blocked before request: {request.url}")

    result = await execute_prepared_tool(
        _tool(_handler, resolver=_resolver),
        {"url": "https://start.test/article"},
        _ctx(),
    )

    assert result.ok is False
    assert requested_urls == ["https://start.test/article"]
    assert result.data is not None
    assert result.data["status"] == "blocked"
    assert result.data["url"] == "http://127.0.0.1/secret"
    assert result.data["reason"] == "internal_ip_blocked:127.0.0.1"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_web_fetch_returns_http_error_status() -> None:
    """验证 HTTP 错误，输入 500 响应，输出结构化 error。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom", request=request)

    result = await execute_prepared_tool(
        _tool(_handler), {"url": "https://example.test/500"}, _ctx()
    )

    assert result.ok is False
    assert result.data is not None
    assert result.data["status"] == "error"
    assert result.data["reason"] == "http:HTTPStatusError"
    assert result.data["suggestion"] == "try another source"


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    (
        "http://[::1",
        "http://example.com:99999/",
    ),
)
async def test_web_fetch_returns_invalid_url_for_malformed_urls(url: str) -> None:
    """验证畸形 URL，输入无法解析或请求的 URL，输出结构化 invalid_url。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"malformed URL should not be requested: {request.url}")

    result = await execute_prepared_tool(_tool(_handler), {"url": url}, _ctx())

    assert result.ok is False
    assert result.error_message == "invalid_url"
    assert result.data is not None
    assert result.data["status"] == "error"
    assert result.data["reason"] == "invalid_url"
    assert result.data["suggestion"] == "provide a valid http or https URL"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_web_fetch_rejects_unsupported_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 content-type 异常，输入 PDF 响应，输出结构化 error。"""

    def _extract_should_not_run(html: str, output_format: str, include_comments: bool) -> str:
        raise AssertionError("unsupported content-type should stop before extraction")

    monkeypatch.setattr(web_fetch_tool.trafilatura, "extract", _extract_should_not_run)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"%PDF-1.7",
            headers={"content-type": "application/pdf"},
            request=request,
        )

    result = await execute_prepared_tool(
        _tool(_handler), {"url": "https://example.test/file.pdf"}, _ctx()
    )

    assert result.ok is False
    assert result.data is not None
    assert result.data["status"] == "error"
    assert result.data["reason"] == "unsupported_content_type:application/pdf"
    assert result.data["suggestion"] == "try another source"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_web_fetch_blocks_paywall_or_js_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证垃圾正文识别，输入 JS 壳文本，输出 blocked。"""
    monkeypatch.setattr(web_fetch_tool, "_JUNK_MIN_CHARS", 1)
    monkeypatch.setattr(
        web_fetch_tool.trafilatura,
        "extract",
        lambda html, output_format, include_comments: "enable javascript to continue",
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>shell</html>", request=request)

    result = await execute_prepared_tool(
        _tool(_handler), {"url": "https://example.test/shell"}, _ctx()
    )

    assert result.ok is False
    assert result.data is not None
    assert result.data["status"] == "blocked"
    assert result.data["reason"] == "paywall_or_js_shell"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_web_fetch_uses_keyword_windows_when_over_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证长文 query 窗口，输入超预算正文，输出关键词附近片段。"""
    monkeypatch.setattr(web_fetch_tool, "_FETCH_TOKEN_BUDGET", 10)
    monkeypatch.setattr(web_fetch_tool, "_CHAR_PER_TOKEN", 4)
    monkeypatch.setattr(web_fetch_tool, "_JUNK_MIN_CHARS", 1)
    monkeypatch.setattr(web_fetch_tool, "_KEYWORD_WINDOW_CHARS", 8)
    content = ("x" * 100) + " needle answer " + ("y" * 100)
    monkeypatch.setattr(
        web_fetch_tool.trafilatura,
        "extract",
        lambda html, output_format, include_comments: content,
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>long</html>", request=request)

    result = await execute_prepared_tool(
        _tool(_handler),
        {"url": "https://example.test/long", "query": "needle"},
        _ctx(),
    )

    assert result.ok is True
    assert "needle" in result.content
    assert "answer" in result.content
    assert len(result.content) < len(content)
    assert result.data is not None
    assert result.data["has_more"] is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_web_fetch_rejects_invalid_offset() -> None:
    """验证 offset 参数校验，输入负数字符串，输出结构化错误。"""
    result = await execute_prepared_tool(
        WebFetchTool(),
        {"url": "https://example.test/article", "offset": "-1"},
        _ctx(),
    )

    assert result.ok is False
    assert result.data is None
    assert result.error_message == "web_fetch offset must be an integer >= 0"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_web_fetch_rejects_oversized_offset_string() -> None:
    """验证超长 offset 字符串，输入 5000 位数字，输出结构化错误。"""
    result = await execute_prepared_tool(
        WebFetchTool(),
        {"url": "https://example.test/article", "offset": "9" * 5000},
        _ctx(),
    )

    assert result.ok is False
    assert result.data is None
    assert result.error_message == "web_fetch offset must be an integer >= 0"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_web_fetch_converts_extract_exception_to_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证正文抽取异常，输入 extractor 抛错，输出结构化 error。"""

    def _explode(html: str, output_format: str, include_comments: bool) -> str:
        raise RuntimeError("extract failed")

    monkeypatch.setattr(web_fetch_tool.trafilatura, "extract", _explode)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>bad</html>", request=request)

    result = await execute_prepared_tool(
        _tool(_handler), {"url": "https://example.test/bad"}, _ctx()
    )

    assert result.ok is False
    assert result.data is not None
    assert result.data["status"] == "error"
    assert result.data["reason"] == "extract:RuntimeError"
    assert result.data["suggestion"] == "try another source"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_safe_network_backend_connects_resolved_safe_ip() -> None:
    """验证实际 TCP 连接使用已校验 IP，输入域名，输出 delegate 收到公网 IP。"""

    class _RecordingBackend:
        """记录 connect_tcp 参数的 fake backend。"""

        def __init__(self) -> None:
            self.host: str | None = None

        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: object = None,
        ) -> object:
            del port, timeout, local_address, socket_options
            self.host = host
            raise RuntimeError("stop after recording")

    delegate = _RecordingBackend()
    backend = _SafeNetworkBackend(
        resolver=lambda _hostname: ("8.8.8.8",),
        delegate=delegate,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="stop after recording"):
        await backend.connect_tcp("example.test", 443)

    assert delegate.host == "8.8.8.8"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_safe_network_backend_blocks_rebound_private_ip() -> None:
    """验证连接时 DNS 变私网会阻断，输入私网 IP，输出 safe_network_blocked。"""

    class _FailIfCalledBackend:
        """被调用即失败的 fake backend。"""

        async def connect_tcp(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("unsafe address should not reach delegate")

    backend = _SafeNetworkBackend(
        resolver=lambda _hostname: ("127.0.0.1",),
        delegate=_FailIfCalledBackend(),  # type: ignore[arg-type]
    )

    with pytest.raises(
        httpcore.ConnectError,
        match=r"safe_network_blocked:internal_ip_blocked:127\.0\.0\.1",
    ):
        await backend.connect_tcp("example.test", 443)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_web_fetch_converts_rebind_private_ip_to_blocked() -> None:
    """验证连接期 DNS rebind，输入先公网后私网，输出结构化 blocked。"""
    calls = 0

    def _resolver(_hostname: str) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        return ("8.8.8.8",) if calls == 1 else ("127.0.0.1",)

    tool = WebFetchTool(engine=_WebFetchEngine(resolver=_resolver))

    result = await execute_prepared_tool(tool, {"url": "https://example.test/article"}, _ctx())

    assert result.ok is False
    assert result.data is not None
    assert result.data["status"] == "blocked"
    assert result.data["reason"] == "internal_ip_blocked:127.0.0.1"


@pytest.mark.unit
def test_build_web_fetch_tool_enabled_switch() -> None:
    """验证 web_fetch 构造开关，输入 enabled，输出工具列表。"""
    enabled = build_web_fetch_tool(enabled=True)
    disabled = build_web_fetch_tool(enabled=False)

    assert [tool.name for tool in enabled] == ["web_fetch"]
    assert disabled == []


@pytest.mark.unit
def test_web_fetch_tool_schema_bounds_offset_string_digits() -> None:
    """验证 offset schema 限制字符串位数，输入 schema，输出有限 pattern。"""
    offset_schema = WebFetchTool.input_schema["properties"]["offset"]

    assert offset_schema["anyOf"][1]["pattern"] == "^[0-9]{1,12}$"


@pytest.mark.unit
def test_builtin_package_exports_web_fetch_tool() -> None:
    """验证 builtin 子包门面导出 web_fetch，输入 lazy export，输出工具符号。"""
    assert builtin.WebFetchTool is WebFetchTool
    assert [tool.name for tool in builtin.build_web_fetch_tool()] == ["web_fetch"]


@pytest.mark.unit
def test_default_registry_registers_web_fetch() -> None:
    """验证默认 registry 注册 web_fetch，输入默认开关，输出工具可查找。"""
    enabled = build_default_registry(file_enabled=False, shell_enabled=False)
    disabled = build_default_registry(
        file_enabled=False,
        shell_enabled=False,
        web_fetch_enabled=False,
    )

    assert "web_fetch" in enabled
    assert "web_fetch" not in disabled
