"""LLM provider 工厂：统一构造 + preset 覆盖。

避免 provider 选择逻辑在 native_runtime / web.run / scheduler 等多处重复。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config_loader.models import Config, LLMPresetConfig
    from core.contracts import LLMProvider

__all__ = ["apply_preset", "build_provider"]


def apply_preset(config: Config, preset: LLMPresetConfig) -> Config:
    """将 preset 的模型字段覆盖到 config，返回新 Config（不修改原对象）。"""
    api_key = ""
    if preset.api_key_env:
        api_key = os.environ.get(preset.api_key_env, "")

    model_overrides: dict[str, Any] = {
        "name": preset.model,
        "base_url": preset.base_url,
        "api_key": api_key,
    }
    if preset.provider is not None:
        model_overrides["provider"] = preset.provider
    if preset.reasoning_effort is not None:
        model_overrides["reasoning_effort"] = preset.reasoning_effort

    preset_model = config.model.model_copy(update=model_overrides)
    return config.model_copy(update={"model": preset_model})


def build_provider(config: Config) -> LLMProvider:
    """根据 config.model.effective_provider 构造对应的 LLM provider 实例。"""
    from executors.llm.anthropic_messages import AnthropicMessagesProvider
    from executors.llm.openai_responses import OpenAIResponsesProvider

    if config.model.effective_provider == "anthropic":
        return AnthropicMessagesProvider(
            model_config=config.model,
            max_retries=config.retry.max_retries,
            retry_backoff=config.retry.retry_backoff,
            enable_raw_dump=config.trace.raw_llm,
            stream_read_timeout=config.stream.read_timeout,
        )
    return OpenAIResponsesProvider(
        model_config=config.model,
        max_retries=config.retry.max_retries,
        retry_backoff=config.retry.retry_backoff,
        enable_raw_dump=config.trace.raw_llm,
        stream_read_timeout=config.stream.read_timeout,
    )
