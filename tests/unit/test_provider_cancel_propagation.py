"""unit：LLM provider 必须把 ``CancelledError`` 透传给上游（interrupt-run-v0.1）。

两层钉子：

1. **静态 AST 钉子**：扫 ``src/executors/llm/*.py``，检查没有 ``except BaseException``
   或 ``except (BaseException, ...)`` —— 此类写法会吞掉 ``asyncio.CancelledError``
   导致用户 interrupt 时 runner 顶层 except 永远等不到 CancelledError，
   ``Result(status="cancelled")`` 退化为 ``Result(status="failed", error=ProviderError(...))``。

2. **行为钉子**：mock httpx transport 让 stream 进入"永远 hang"状态，外部
   ``task.cancel()`` 后，必须抛 ``asyncio.CancelledError``，**不**能抛
   ``ProviderError``。

Python 3.8+ ``CancelledError`` 不继承 ``Exception``，所以现状 ``except Exception``
路径不会吞它；本测试是防御未来重构。
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from config_loader.models import ModelConfig
from core.contracts import LLMRequest
from core.message import Message
from executors.llm.anthropic_messages import AnthropicMessagesProvider

_PROVIDER_DIR = Path(__file__).resolve().parents[2] / "src" / "executors" / "llm"


# ---------------------------------------------------------------------------
# 1. 静态 AST 钉子：禁止 except BaseException 吞 CancelledError
# ---------------------------------------------------------------------------


def _iter_except_types(node: ast.AST) -> list[ast.AST]:
    """返回所有 ExceptHandler 的 type 节点（Name / Tuple / Attribute）。"""
    types: list[ast.AST] = []
    for n in ast.walk(node):
        if isinstance(n, ast.ExceptHandler) and n.type is not None:
            types.append(n.type)
    return types


def _names_in_except_type(t: ast.AST) -> list[str]:
    """从 except type 节点提取所有 Name id（Tuple 内多个 / 直接 Name）。"""
    if isinstance(t, ast.Name):
        return [t.id]
    if isinstance(t, ast.Tuple):
        return [elt.id for elt in t.elts if isinstance(elt, ast.Name)]
    if isinstance(t, ast.Attribute):
        # 例如 ``except asyncio.CancelledError`` — Attribute 不在禁列
        return []
    return []


@pytest.mark.unit
def test_no_provider_module_catches_baseexception() -> None:
    """provider 层（src/executors/llm/）不许 ``except BaseException``（任何形式）。

    BaseException 是 ``Exception`` + ``KeyboardInterrupt`` + ``SystemExit`` +
    ``asyncio.CancelledError`` 的共同祖先；catch 它会让用户 interrupt 失效。
    """
    offenders: list[tuple[str, int, str]] = []
    for py_file in _PROVIDER_DIR.glob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue
        for t in _iter_except_types(tree):
            names = _names_in_except_type(t)
            if "BaseException" in names:
                offenders.append((py_file.name, t.lineno, ast.unparse(t)))
    assert not offenders, (
        "BaseException 被 catch（会吞 CancelledError），违反 interrupt-run-v0.1 钉子：\n"
        + "\n".join(f"  {f}:{ln}  except {expr}" for f, ln, expr in offenders)
    )


@pytest.mark.unit
def test_no_provider_module_catches_cancelled_error_silently() -> None:
    """provider 层不许 ``except asyncio.CancelledError`` 然后**不**重新 raise。

    如果将来必须 catch（比如做 cleanup），必须 ``raise`` 或 ``raise CancelledError`` 重抛。
    """
    offenders: list[tuple[str, int]] = []
    for py_file in _PROVIDER_DIR.glob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue
        for handler in ast.walk(tree):
            if not isinstance(handler, ast.ExceptHandler):
                continue
            t = handler.type
            if t is None:
                continue

            # 命中 ``CancelledError`` 的写法：
            # - Name(id="CancelledError") （from asyncio import CancelledError）
            # - Attribute(attr="CancelledError")（asyncio.CancelledError）
            # - Tuple 内含上述任一
            def _mentions_cancelled(node: ast.AST) -> bool:
                if isinstance(node, ast.Name) and node.id == "CancelledError":
                    return True
                if isinstance(node, ast.Attribute) and node.attr == "CancelledError":
                    return True
                if isinstance(node, ast.Tuple):
                    return any(_mentions_cancelled(e) for e in node.elts)
                return False

            if not _mentions_cancelled(t):
                continue
            # 检查 handler.body 是否含 raise（无参 raise 或 raise CancelledError(...)）
            has_raise = any(isinstance(stmt, ast.Raise) for stmt in ast.walk(handler))
            if not has_raise:
                offenders.append((py_file.name, handler.lineno))
    assert not offenders, (
        "provider 模块 catch 了 CancelledError 但没 raise，会让 interrupt 哑火：\n"
        + "\n".join(f"  {f}:{ln}" for f, ln in offenders)
    )


# ---------------------------------------------------------------------------
# 2. 行为钉子：stream 跑到一半被 cancel → CancelledError 透传
# ---------------------------------------------------------------------------


def _make_provider() -> AnthropicMessagesProvider:
    return AnthropicMessagesProvider(
        model_config=ModelConfig(
            provider="anthropic",
            name="claude-test",
            base_url="https://api.anthropic.com",
            api_key="sk-test",
        )
    )


class _HangingTransport(httpx.AsyncBaseTransport):
    """mock transport：handle_async_request 永远 await，不返回。

    模拟 LLM 服务端长时间不响应 / 网络挂起，runner 此时 task.cancel()。
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise RuntimeError("unreachable")


@pytest.mark.unit
async def test_anthropic_stream_cancel_propagates_cancelled_error() -> None:
    """task.cancel() AnthropicMessagesProvider.stream → CancelledError 透传。

    **不**应该被 ``except Exception`` 翻译成 ProviderError；runner 顶层接住
    CancelledError 才能走 ``Result(status="cancelled")``。
    """
    provider = _make_provider()
    provider._client = httpx.AsyncClient(transport=_HangingTransport())

    request = LLMRequest(model="claude-test", messages=(Message.user("hi"),))

    async def _consume() -> Any:
        async for _chunk in provider.stream(request):
            pass

    task: asyncio.Task[Any] = asyncio.create_task(_consume())
    await asyncio.sleep(0.05)  # 让 stream 进入 client.stream(POST...) 阻塞
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    await provider.aclose()
