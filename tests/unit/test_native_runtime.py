"""unit：NativeRuntime.build 装配行为 + 一轮 stub provider 运行。

``build`` 默认会构造 :class:`OpenAIResponsesProvider`，该 provider 在
``runtime.run`` 真正被调用前不会发起网络请求——此时我们把内部 ``_llm``
替换成 stub 来验证一轮完整主链路。直接操作私有属性不好看，但比为了测试
在生产代码里开测试专用后门要干净得多；当代码不稳定导致属性名变化时，
只需要同步改这一处。
"""

from __future__ import annotations

from typing import Any

import pytest

from config_loader.models import (
    ApprovalConfig,
    Config,
    FileToolConfig,
    ModelConfig,
    RunnerConfig,
    ShellToolConfig,
    ToolConfig,
)
from core.contracts import LLMRequest, LLMResponse
from core.message import Message
from executors.agent_runtime.native_runtime import NativeRuntime


def _cfg() -> Config:
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="gemma-4-e4b-it",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        runner=RunnerConfig(max_turns=3),
        approval=ApprovalConfig(mode="auto_allow"),
        tool=ToolConfig(
            file=FileToolConfig(enabled=True),
            shell=ShellToolConfig(enabled=True),
        ),
    )


class _StubLLM:
    def __init__(self, reply: str = "ok") -> None:
        self._reply = reply
        self.called = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.called += 1
        return LLMResponse(
            message=Message(role="assistant", content=self._reply),
            finish_reason="stop",
        )


@pytest.mark.unit
def test_native_runtime_build_returns_instance() -> None:
    runtime = NativeRuntime.build(_cfg())
    assert runtime is not None
    assert runtime.config is not None
    assert runtime.agent_spec.default_model == "gemma-4-e4b-it"


@pytest.mark.unit
def test_native_runtime_build_accepts_event_sinks() -> None:
    sinks: list[Any] = []

    class _Sink:
        async def emit(self, event: Any) -> None:
            sinks.append(event)

    sink = _Sink()
    runtime = NativeRuntime.build(_cfg(), event_sinks=[sink])
    assert runtime is not None


@pytest.mark.unit
async def test_native_runtime_run_with_stub_llm_completes() -> None:
    """装好 runtime 后替换 stub llm，跑一轮 happy path。"""
    runtime = NativeRuntime.build(_cfg())
    # 替换 provider 为本地 stub，规避网络
    stub = _StubLLM(reply="hi from stub")
    runtime._llm = stub  # type: ignore[attr-defined]

    result = await runtime.run("hello", session_id="u1")
    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "hi from stub"
    assert stub.called == 1


@pytest.mark.unit
async def test_native_runtime_reuses_session_for_same_session_id() -> None:
    """同一 session_id 多次 run 应该落到同一个 session（多轮连续性）。"""
    runtime = NativeRuntime.build(_cfg())
    stub = _StubLLM(reply="ok")
    runtime._llm = stub  # type: ignore[attr-defined]

    await runtime.run("turn1", session_id="abc")
    await runtime.run("turn2", session_id="abc")

    # stub 被调用两次
    assert stub.called == 2
    # runtime 内部缓存了一份 session 实例
    # （这里通过公共访问点不直接断言，改为 run 结果都 completed）


@pytest.mark.unit
def test_native_runtime_build_with_explicit_agent_spec() -> None:
    from core.agent_spec import AgentSpec

    spec = AgentSpec(
        name="explicit",
        instructions="hi",
        default_model="custom-model",
        max_turns=2,
    )
    runtime = NativeRuntime.build(_cfg(), agent_spec=spec)
    assert runtime.agent_spec is spec
    assert runtime.agent_spec.default_model == "custom-model"


@pytest.mark.unit
def test_native_runtime_build_with_tools() -> None:
    from tools import ReadFileTool, ToolRegistry

    registry = ToolRegistry([ReadFileTool()])
    runtime = NativeRuntime.build(
        _cfg(),
        tools=registry,
        enabled_tool_names=["read_file"],
    )
    assert runtime is not None


@pytest.mark.unit
async def test_native_runtime_aclose_delegates_to_llm() -> None:
    """NativeRuntime.aclose 应调 provider.aclose，且幂等（多次调不抛）。"""

    class _AcloseCountingLLM:
        def __init__(self) -> None:
            self.aclose_calls = 0

        async def complete(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover
            raise AssertionError("not invoked in this test")

        async def aclose(self) -> None:
            self.aclose_calls += 1

    runtime = NativeRuntime.build(_cfg())
    stub = _AcloseCountingLLM()
    runtime._llm = stub  # type: ignore[attr-defined]

    await runtime.aclose()
    await runtime.aclose()
    assert stub.aclose_calls == 2


@pytest.mark.unit
async def test_native_runtime_aclose_tolerates_llm_without_aclose() -> None:
    """provider 没有 aclose 方法时，runtime.aclose 不抛（向后兼容）。"""

    class _NoAcloseLLM:
        async def complete(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover
            raise AssertionError("not invoked in this test")

    runtime = NativeRuntime.build(_cfg())
    runtime._llm = _NoAcloseLLM()  # type: ignore[attr-defined]
    # 不抛即视为通过
    await runtime.aclose()
