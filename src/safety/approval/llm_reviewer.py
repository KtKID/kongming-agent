"""default:ask 的小模型复核器。"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from core.contracts import LLMProvider, LLMRequest
from core.message import Message

_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|cookie|credential|password|secret|token)",
    re.IGNORECASE,
)
_DEFAULT_PROMPT = """你是工具调用的审批复核器。
只输出 JSON：{"decision":"allow"|"review_required","reason":"不超过120字"}。
allow 只表示该调用可进入用户可中断的倒计时窗口；不确定、拒绝建议和格式错误都输出 review_required。
"""


@runtime_checkable
class _AsyncClosable(Protocol):
    """可释放异步网络资源的 provider 最小合同。"""

    async def aclose(self) -> None:
        """关闭 provider 持有的连接池。"""
        ...


class LlmReviewDecision(StrEnum):
    """小模型复核的有限出口。"""

    ALLOW = "allow"
    REVIEW_REQUIRED = "review_required"


@dataclass(frozen=True, slots=True)
class LlmReviewResult:
    """已验证的小模型复核结论。"""

    decision: LlmReviewDecision
    reason: str
    model: str


class ApprovalLlmReviewer:
    """对 default:ask 返回 allow 或转人工审批的复核门户。"""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        model: str,
        timeout_seconds: float,
        prompt_template_path: Path | None = None,
    ) -> None:
        """绑定独立 provider、模型名、超时和可选提示词模板。"""
        self._provider = provider
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._prompt = _load_prompt(prompt_template_path)

    @property
    def model(self) -> str:
        """返回审计使用的模型标识。"""
        return self._model

    async def review(
        self,
        *,
        cwd: str,
        tool_name: str,
        tool_input: Mapping[str, object],
        matched_rule: str,
    ) -> LlmReviewResult:
        """复核一次 default:ask 请求，异常交由调用方回落人审。"""
        payload = {
            "cwd": cwd,
            "tool_name": tool_name,
            "arguments": _redact(tool_input),
            "matched_rule": matched_rule,
        }
        request = LLMRequest(
            model=self._model,
            messages=(
                Message.system(self._prompt),
                Message.user(json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            ),
            timeout_seconds=self._timeout_seconds,
        )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                response = await self._provider.complete(request)
        except TimeoutError as exc:
            raise RuntimeError("approval llm reviewer timed out") from exc

        content = response.message.content
        if not isinstance(content, str):
            raise ValueError("approval llm reviewer returned no text")
        return _parse_result(content, model=self._model)

    async def aclose(self) -> None:
        """释放 reviewer provider 的可选连接池。"""
        if isinstance(self._provider, _AsyncClosable):
            await self._provider.aclose()


def build_approval_llm_reviewer(config: object) -> ApprovalLlmReviewer | None:
    """从全局 Config 构造独立审批复核器，缺少配置时关闭 LLM 分支。"""
    from infrastructure.config.model_provider_catalog import (
        CatalogSource,
        ProviderProtocol,
        ResolvedModelConfig,
        ResolvedModelCredential,
    )
    from infrastructure.config.models import Config
    from infrastructure.llm_providers.provider_factory import build_provider

    if not isinstance(config, Config):
        raise TypeError("approval reviewer requires infrastructure Config")
    llm_config = config.safety.approval.llm
    if llm_config is None:
        return None
    protocol = (
        ProviderProtocol.ANTHROPIC
        if llm_config.provider == "anthropic"
        else ProviderProtocol.OPENAI
    )
    runtime = ResolvedModelConfig(
        catalog_version=2,
        catalog_source=CatalogSource.USER,
        provider_id="safety-approval",
        preset_id="safety-approval",
        protocol=protocol,
        name=llm_config.model,
        base_url=llm_config.base_url,
        api_key_env=None,
        fallback_api_key_envs=(),
        api_key_header=llm_config.api_key_header,
        timeout=llm_config.timeout_seconds,
        max_tokens=4096,
        temperature=0.7,
        context_window_tokens=None,
        reasoning=None,
        default_reasoning_effort=None,
    )
    credential = ResolvedModelCredential(
        value=llm_config.api_key,
        env_name=None,
        header=llm_config.api_key_header,
    )
    return ApprovalLlmReviewer(
        provider=build_provider(config, resolved_model=runtime, credential=credential),
        model=llm_config.model,
        timeout_seconds=llm_config.timeout_seconds,
        prompt_template_path=llm_config.prompt_template_path,
    )


def _load_prompt(path: Path | None) -> str:
    """加载可选提示词模板，缺省使用受限 JSON 合同。"""
    if path is None:
        return _DEFAULT_PROMPT
    prompt = path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError(f"approval llm prompt template is empty: {path}")
    return prompt


def _redact(value: object) -> object:
    """递归遮蔽敏感字段，避免将凭据传给审批模型。"""
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value


def _parse_result(content: str, *, model: str) -> LlmReviewResult:
    """校验模型 JSON；除 allow 外统一收敛为转人工审批。"""
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("approval llm reviewer returned invalid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("approval llm reviewer JSON must be an object")
    raw_decision = raw.get("decision")
    reason = raw.get("reason", "")
    if not isinstance(raw_decision, str) or not isinstance(reason, str):
        raise ValueError("approval llm reviewer JSON fields are invalid")
    decision = (
        LlmReviewDecision.ALLOW
        if raw_decision == LlmReviewDecision.ALLOW.value
        else LlmReviewDecision.REVIEW_REQUIRED
    )
    return LlmReviewResult(decision=decision, reason=reason.strip()[:120], model=model)


__all__ = [
    "ApprovalLlmReviewer",
    "LlmReviewDecision",
    "LlmReviewResult",
    "build_approval_llm_reviewer",
]
