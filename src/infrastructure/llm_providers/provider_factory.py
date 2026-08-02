"""LLM provider 工厂：catalog snapshot 解析、credential 注入与 provider 构造。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.model_provider_catalog import (
    ResolvedModelConfig,
    ResolvedModelCredential,
)
from infrastructure.config.models import (
    Config,
    ReasoningEffortInput,
)

if TYPE_CHECKING:
    from core.contracts import AssetBytesReader, LLMProvider

__all__ = ["build_provider", "resolve_model_config"]


def resolve_model_config(
    config: Config,
    *,
    catalog_manager: ModelCatalogManager | None = None,
    preset_id: str | None = None,
    reasoning_effort: ReasoningEffortInput | None = None,
) -> ResolvedModelConfig:
    """通过统一 Manager 解析单次 runtime 的 immutable 模型快照。"""
    manager = catalog_manager or ModelCatalogManager()
    return manager.resolve_runtime(
        config.model,
        preset_id=preset_id,
        reasoning_effort=reasoning_effort,
    )


def build_provider(
    config: Config,
    *,
    asset_reader: AssetBytesReader | None = None,
    catalog_manager: ModelCatalogManager | None = None,
    resolved_model: ResolvedModelConfig | None = None,
    credential: ResolvedModelCredential | None = None,
) -> LLMProvider:
    """按 immutable snapshot 构造 provider，并在此阶段解析 credential。"""
    from infrastructure.llm_providers.anthropic_messages import AnthropicMessagesProvider
    from infrastructure.llm_providers.openai_responses import OpenAIResponsesProvider

    manager = catalog_manager or ModelCatalogManager()
    runtime = resolved_model or manager.resolve_runtime(config.model)
    resolved_credential = credential or manager.resolve_credential(runtime)
    if runtime.effective_provider == "anthropic":
        return AnthropicMessagesProvider(
            model_config=runtime,
            credential=resolved_credential,
            max_retries=config.retry.max_retries,
            retry_backoff=config.retry.retry_backoff,
            enable_raw_dump=config.trace.raw_llm,
            stream_read_timeout=config.stream.read_timeout,
            asset_reader=asset_reader,
        )
    return OpenAIResponsesProvider(
        model_config=runtime,
        credential=resolved_credential,
        max_retries=config.retry.max_retries,
        retry_backoff=config.retry.retry_backoff,
        enable_raw_dump=config.trace.raw_llm,
        stream_read_timeout=config.stream.read_timeout,
    )
