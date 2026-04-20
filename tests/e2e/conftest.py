"""e2e 测试专用 fixture 仓库。

这里集中提供 e2e 需要的 stub / recorder 组件：

- :class:`StubLLMProvider`：脚本化的 :class:`core.contracts.LLMProvider` 实现。
  测试用 ``script(content=..., tool_calls=...)`` 排队多轮响应，由 runner 按顺序
  消费，完全不碰真实网络。
- :class:`MemoryEventSink`：把所有 Event 收集到内存里便于断言。
- :class:`RecordingApproval`：把所有 ApprovalRequest 记录下来便于断言；默认
  ``approved``，可切 ``rejected``。
- :func:`local_model_config`：构造一个指向本地模型基线
  (``http://127.0.0.1:1234`` + ``gemma-4-e4b-it`` + 空 ``api_key``) 的
  :class:`config_loader.models.Config`。

所有 e2e 默认走 stub provider，测试不需要真实模型服务就能跑；真实模型路径
仅通过环境变量 ``KONGMING_E2E_REAL_MODEL=1`` 显式开启。
"""

from __future__ import annotations

import pytest

from config_loader.models import ApprovalConfig, Config, ModelConfig, RunnerConfig
from core.contracts import (
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
    Event,
    LLMRequest,
    LLMResponse,
)
from core.message import Message, ToolCall

# ---------------------------------------------------------------------------
# Stub LLM Provider
# ---------------------------------------------------------------------------


class StubLLMProvider:
    """脚本化 LLMProvider。

    用法：

        stub = StubLLMProvider()
        stub.script(content="hello")                                # 纯文本一轮
        stub.script(tool_calls=[ToolCall("c1", "read_file", {})])   # 工具调用一轮

    每次 :meth:`complete` 被调用时弹出队首一条响应。队列耗尽后默认返回一条空的
    assistant 消息以终止 runner（避免测试无限循环）。

    结构上满足 :class:`core.contracts.LLMProvider` Protocol（单个 async
    ``complete`` 方法）。
    """

    def __init__(
        self,
        responses: list[tuple[str | None, list[ToolCall] | None]] | None = None,
    ) -> None:
        self._responses: list[tuple[str | None, list[ToolCall] | None]] = list(responses or [])
        self.calls: list[LLMRequest] = []

    def script(
        self,
        content: str | None = None,
        tool_calls: list[ToolCall] | None = None,
    ) -> None:
        """排队一条将要由下一次 ``complete`` 返回的响应。"""
        self._responses.append((content, tool_calls))

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if not self._responses:
            # 默认终止：返回空 assistant 消息，runner 视为 stop。
            return LLMResponse(
                message=Message(role="assistant", content=""),
                finish_reason="stop",
            )
        content, tool_calls = self._responses.pop(0)
        tool_calls_tuple: tuple[ToolCall, ...] | None = tuple(tool_calls) if tool_calls else None
        msg = Message(
            role="assistant",
            content=content,
            tool_calls=tool_calls_tuple,
        )
        finish = "tool_calls" if tool_calls_tuple else "stop"
        return LLMResponse(message=msg, finish_reason=finish)


# ---------------------------------------------------------------------------
# Memory Event Sink
# ---------------------------------------------------------------------------


class MemoryEventSink:
    """把所有 Event 收集到内存，便于断言事件流。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        """返回事件 kind 序列，方便 ``assert "run.start" in sink.kinds()``。"""
        return [e.kind for e in self.events]

    def of_kind(self, kind: str) -> list[Event]:
        """返回指定 kind 的事件子集。"""
        return [e for e in self.events if e.kind == kind]


# ---------------------------------------------------------------------------
# Recording Approval
# ---------------------------------------------------------------------------


class RecordingApproval:
    """记录所有 approval 请求；按 ``outcome`` 返回固定决定。

    默认 ``"approved"``。需要反向测试时传 ``"rejected"`` / ``"cancelled"``。
    """

    def __init__(self, outcome: ApprovalOutcome = "approved") -> None:
        self._outcome: ApprovalOutcome = outcome
        self.requests: list[ApprovalRequest] = []

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(
            outcome=self._outcome,
            reason=f"recording-{self._outcome}",
            metadata={"test": True, "tool_name": request.tool_name},
        )


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_llm() -> StubLLMProvider:
    """一个空的 StubLLMProvider，测试自己 ``script(...)`` 排队响应。"""
    return StubLLMProvider()


@pytest.fixture
def memory_sink() -> MemoryEventSink:
    return MemoryEventSink()


@pytest.fixture
def recording_approval() -> RecordingApproval:
    return RecordingApproval()


@pytest.fixture
def local_model_config() -> Config:
    """构造指向本地模型基线的 :class:`Config`。

    该 fixture 仅用于装配路径验证，不会发起真实 HTTP 请求。
    """
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="gemma-4-e4b-it",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        runner=RunnerConfig(max_turns=5),
        approval=ApprovalConfig(mode="auto_allow"),
    )


__all__ = [
    "MemoryEventSink",
    "RecordingApproval",
    "StubLLMProvider",
]
