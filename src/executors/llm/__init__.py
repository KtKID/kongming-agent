"""executors.llm：LLM provider 适配实现。

- :class:`BaseLLMProvider`：provider 实现侧基类（重试 / 超时 / 错误归一化 /
  OpenAI chat 消息格式转换助手）。
- :class:`OpenAIResponsesProvider`：首个 OpenAI-compatible provider，第一版
  走 ``{base_url}/v1/chat/completions`` 端点，兼容 LM Studio / Ollama / vLLM /
  OpenAI 官方端点。

注意：:class:`BaseLLMProvider` 不是 :class:`core.contracts.LLMProvider`
Protocol 的重定义，只是 provider 实现方共用的便利基类。Protocol 真源在
``core/contracts.py``。
"""

from __future__ import annotations

from executors.llm.base import BaseLLMProvider
from executors.llm.openai_responses import OpenAIResponsesProvider

__all__ = ["BaseLLMProvider", "OpenAIResponsesProvider"]
