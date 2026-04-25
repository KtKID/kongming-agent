"""unit：BaseLLMProvider httpx client 懒加载 / 复用 / aclose 生命周期。"""

from __future__ import annotations

import httpx
import pytest

from config_loader.models import ModelConfig
from core.contracts import LLMRequest, LLMResponse
from core.message import Message
from executors.llm.anthropic_messages import AnthropicMessagesProvider
from executors.llm.base import BaseLLMProvider
from executors.llm.openai_responses import OpenAIResponsesProvider


def _make_config(**overrides: object) -> ModelConfig:
    defaults = dict(name="test-model", base_url="http://127.0.0.1:1234/v1", api_key="")
    defaults.update(overrides)
    return ModelConfig(**defaults)  # type: ignore[arg-type]


def _make_base_provider(**overrides: object) -> BaseLLMProvider:
    """实例化一个会真正调用 _ensure_client 的 BaseLLMProvider 子类。"""

    class _Concrete(BaseLLMProvider):
        async def _do_complete(self, request: LLMRequest) -> LLMResponse:
            # 模拟真实 provider 行为：触发 client 懒加载
            self._ensure_client()
            return LLMResponse(
                message=Message.assistant("ok"),
                finish_reason="stop",
                usage={},
            )

    return _Concrete(model_config=_make_config(**overrides))


# ---------------------------------------------------------------------------
# #1 懒加载 + 同实例
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ensure_client_lazy_init() -> None:
    """_client 首次为 None；_ensure_client() 后非 None；重复调用返回同实例。"""
    provider = _make_base_provider()

    assert provider._client is None

    c1 = provider._ensure_client()
    assert c1 is not None
    assert isinstance(c1, httpx.AsyncClient)

    c2 = provider._ensure_client()
    assert c2 is c1


# ---------------------------------------------------------------------------
# #2 跨调用复用（monkeypatch __init__ 计数）
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_client_reused_across_calls() -> None:
    """同 provider 连续两次 _do_complete 只构造 1 个 httpx.AsyncClient。"""
    init_count = 0
    original_init = httpx.AsyncClient.__init__

    def _counting_init(self: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        nonlocal init_count
        init_count += 1
        original_init(self, *args, **kwargs)

    provider = _make_base_provider()
    request = LLMRequest(
        model="test-model",
        messages=(Message.user("hi"),),
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx.AsyncClient, "__init__", _counting_init)
        await provider.complete(request)
        await provider.complete(request)

    assert init_count == 1

    await provider.aclose()


# ---------------------------------------------------------------------------
# #3 aclose 关闭 client
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_aclose_closes_client() -> None:
    """aclose() 后 _client is None，旧 client 已 aclose()。"""
    provider = _make_base_provider()
    client = provider._ensure_client()

    # spy on the real aclose
    original_aclose = client.aclose
    closed = False

    async def _spy_aclose() -> None:
        nonlocal closed
        closed = True
        await original_aclose()

    client.aclose = _spy_aclose  # type: ignore[assignment]

    await provider.aclose()

    assert provider._client is None
    assert closed


# ---------------------------------------------------------------------------
# #4 aclose 幂等
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_aclose_idempotent() -> None:
    """连续两次 aclose() 不抛。"""
    provider = _make_base_provider()
    provider._ensure_client()

    await provider.aclose()
    await provider.aclose()  # 第二次不应抛


# ---------------------------------------------------------------------------
# #5 多实例隔离
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_new_provider_instance_new_client() -> None:
    """两个 provider 实例 → 两个独立 client（多 agent 隔离验证）。"""
    p1 = _make_base_provider()
    p2 = _make_base_provider()

    c1 = p1._ensure_client()
    c2 = p2._ensure_client()

    assert c1 is not c2


# ---------------------------------------------------------------------------
# #6 per-request timeout 在同 client 下生效
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_timeout_per_request() -> None:
    """不同 LLMRequest.timeout_seconds 在同一 client 下都生效。"""
    provider = _make_base_provider()
    request_short = LLMRequest(
        model="test-model",
        messages=(Message.user("hi"),),
        timeout_seconds=5.0,
    )
    request_long = LLMRequest(
        model="test-model",
        messages=(Message.user("hello"),),
        timeout_seconds=30.0,
    )

    r1 = await provider.complete(request_short)
    r2 = await provider.complete(request_long)

    assert r1.message.content == "ok"
    assert r2.message.content == "ok"
    # 同一 client 被复用（_do_complete 内部调了 _ensure_client）
    assert provider._client is not None

    await provider.aclose()


# ---------------------------------------------------------------------------
# #7 OpenAI + Anthropic 各自独立复用
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_both_providers_reuse() -> None:
    """OpenAI 和 Anthropic 各自独立复用，不互相污染。"""
    openai_provider = OpenAIResponsesProvider(
        model_config=_make_config(name="gpt-4o"),
    )
    anthropic_provider = AnthropicMessagesProvider(
        model_config=_make_config(name="claude-3-opus", base_url="http://127.0.0.1:1234/v1"),
    )

    oc1 = openai_provider._ensure_client()
    oc2 = openai_provider._ensure_client()
    assert oc1 is oc2

    ac1 = anthropic_provider._ensure_client()
    ac2 = anthropic_provider._ensure_client()
    assert ac1 is ac2

    # 跨 provider 隔离
    assert oc1 is not ac1


# ---------------------------------------------------------------------------
# #8 BaseLLMProvider 直接用（防御测试）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_base_provider_direct_usage() -> None:
    """BaseLLMProvider 直接用也能走 _ensure_client（防御测试）。"""
    provider = _make_base_provider()

    assert provider._client is None
    client = provider._ensure_client()
    assert client is not None
    assert isinstance(client, httpx.AsyncClient)
