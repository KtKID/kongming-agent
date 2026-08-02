"""ApprovalLlmReviewer 的离线合同。"""

from __future__ import annotations

import asyncio

import pytest

from core.contracts import LLMRequest, LLMResponse
from core.message import Message
from safety.approval.llm_reviewer import ApprovalLlmReviewer, LlmReviewDecision


class _Provider:
    """返回固定文本并记录审批复核请求的 fake provider。"""

    def __init__(self, content: str, *, wait_forever: bool = False) -> None:
        self._content = content
        self._wait_forever = wait_forever
        self.requests: list[LLMRequest] = []
        self.closed = False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """返回预置结果，按需模拟永不结束的网络请求。"""
        self.requests.append(request)
        if self._wait_forever:
            await asyncio.Event().wait()
        return LLMResponse(message=Message.assistant(self._content))

    async def aclose(self) -> None:
        """记录 Reviewer 是否关闭了底层 provider。"""
        self.closed = True


def _reviewer(provider: _Provider, *, timeout_seconds: float = 0.05) -> ApprovalLlmReviewer:
    """构造固定模型名的 reviewer。"""
    return ApprovalLlmReviewer(
        provider=provider,
        model="reviewer-small",
        timeout_seconds=timeout_seconds,
    )


@pytest.mark.unit
async def test_allow_is_parsed_and_sensitive_arguments_are_redacted() -> None:
    """allow 保持唯一自动出口，凭据字段不会进入模型请求。"""
    provider = _Provider('{"decision":"allow","reason":"工作区内正常编辑"}')

    result = await _reviewer(provider).review(
        cwd="/workspace",
        tool_name="write_file",
        tool_input={
            "path": "README.md",
            "api_key": "raw-key",
            "nested": {"password": "raw-password"},
        },
        matched_rule="default:ask",
    )

    assert result.decision is LlmReviewDecision.ALLOW
    payload = provider.requests[0].messages[1].content or ""
    assert "raw-key" not in payload
    assert "raw-password" not in payload
    assert payload.count("[REDACTED]") == 2


@pytest.mark.unit
@pytest.mark.parametrize(
    "content",
    [
        '{"decision":"deny","reason":"风险高"}',
        '{"decision":"review_required","reason":"信息不足"}',
    ],
)
async def test_reviewer_only_returns_allow_or_human_review(content: str) -> None:
    """模型拒绝建议统一收敛为人工审批，避免小模型直接拒绝。"""
    result = await _reviewer(_Provider(content)).review(
        cwd="/workspace",
        tool_name="read_file",
        tool_input={"path": "README.md"},
        matched_rule="default:ask",
    )

    assert result.decision is LlmReviewDecision.REVIEW_REQUIRED


@pytest.mark.unit
@pytest.mark.parametrize("content", ["not json", "[]", '{"decision":1}'])
async def test_invalid_output_fails_closed(content: str) -> None:
    """格式错误交由 Manager 保持用户审批 pending。"""
    with pytest.raises(ValueError):
        await _reviewer(_Provider(content)).review(
            cwd="/workspace",
            tool_name="read_file",
            tool_input={"path": "README.md"},
            matched_rule="default:ask",
        )


@pytest.mark.unit
async def test_timeout_and_close_are_explicit() -> None:
    """超时失败关闭，进程关闭时释放独立 provider。"""
    provider = _Provider("", wait_forever=True)
    reviewer = _reviewer(provider, timeout_seconds=0.01)

    with pytest.raises(RuntimeError, match="timed out"):
        await reviewer.review(
            cwd="/workspace",
            tool_name="read_file",
            tool_input={"path": "README.md"},
            matched_rule="default:ask",
        )
    await reviewer.aclose()
    assert provider.closed is True
