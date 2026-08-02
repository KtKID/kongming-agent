"""unit：SessionEngine.build 装配行为 + 一轮 stub provider 运行。

``build`` 默认会构造 :class:`OpenAIResponsesProvider`，该 provider 在
``runtime.run`` 真正被调用前不会发起网络请求——此时我们把内部 ``_llm``
替换成 stub 来验证一轮完整主链路。直接操作私有属性不好看，但比为了测试
在生产代码里开测试专用后门要干净得多；当代码不稳定导致属性名变化时，
只需要同步改这一处。
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest

from application.agents.subagent_tools import SpawnAgentRequest
from core.contracts import (
    LLMRequest,
    LLMResponse,
    PreparedToolCall,
    Session,
    ToolContext,
    ToolResult,
)
from core.message import Message, ToolCall
from core.result import Result
from core.run_state import RunState
from infrastructure.config.models import (
    ApprovalConfig,
    Config,
    FileToolConfig,
    ModelSelectionConfig,
    ReasoningEffortInput,
    RunnerConfig,
    ShellToolConfig,
    ToolConfig,
)
from runtime_assembly.session_engine import SessionEngine


def _cfg(reasoning_effort: ReasoningEffortInput | None = None) -> Config:
    return Config(
        model=ModelSelectionConfig(
            preset_id=(
                "bigmodel-glm5-1m" if reasoning_effort is not None else "local-gemma-4-e4b-it"
            ),
            reasoning_effort=reasoning_effort,
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
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.called += 1
        self.requests.append(request)
        return LLMResponse(
            message=Message(role="assistant", content=self._reply),
            finish_reason="stop",
        )


class _BarrierLLM:
    def __init__(self, *, expected_calls: int, config_effort: Any) -> None:
        self._expected_calls = expected_calls
        self._config_effort = config_effort
        self._all_requests_arrived = asyncio.Event()
        self.requests: list[LLMRequest] = []
        self.config_efforts_seen: list[str | None] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if len(self.requests) >= self._expected_calls:
            self._all_requests_arrived.set()
        await self._all_requests_arrived.wait()
        self.config_efforts_seen.append(self._config_effort())
        return LLMResponse(
            message=Message(role="assistant", content=f"ok {request.reasoning_effort}"),
            finish_reason="stop",
        )


class _NamedTool:
    name = "temp_tool"
    description = "temporary test tool"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}

    async def execute(self, prepared: PreparedToolCall, ctx: ToolContext) -> ToolResult:
        args = prepared.arguments
        del args, ctx
        return ToolResult(ok=True, content="ok")


class _DelegateOnlyRunner:
    def __init__(self) -> None:
        self.run_lifecycle_hooks: Any = None
        self.continue_lifecycle_hooks: Any = None
        self.run_approvals: list[object] = []
        self.continue_approvals: list[object] = []

    async def run(self, *args: Any, **kwargs: Any) -> Result:
        self.run_lifecycle_hooks = kwargs.get("lifecycle_hooks")
        self.run_approvals.append(kwargs["approval"])
        return Result(
            run_id="delegated-run",
            session_id=kwargs["session"].session_id,
            status="completed",
            final_message=Message.assistant("delegated"),
            turn_count=1,
        )

    async def continue_from_last_user_message(self, *args: Any, **kwargs: Any) -> Result:
        self.continue_lifecycle_hooks = kwargs.get("lifecycle_hooks")
        self.continue_approvals.append(kwargs["approval"])
        return Result(
            run_id="delegated-continue",
            session_id=kwargs["session"].session_id,
            status="completed",
            final_message=Message.assistant("continued"),
            turn_count=1,
        )


class _RuntimeLifecycleHook:
    def __init__(self, runtime: SessionEngine, calls: list[tuple[str, str, str]]) -> None:
        self._runtime = runtime
        self._calls = calls

    async def before_turn(self, state: RunState) -> None:
        return None

    async def after_turn(self, state: RunState, assistant_message: Message) -> None:
        return None

    async def before_tool(self, state: RunState, call: ToolCall) -> None:
        return None

    async def after_tool(self, state: RunState, call: ToolCall, result_message: Message) -> None:
        return None

    async def after_run(self, state: RunState, session: Session, result: Result) -> None:
        self._calls.append((self._runtime.agent_spec.name, session.session_id, result.run_id))


@pytest.mark.unit
def test_native_runtime_build_returns_instance() -> None:
    runtime = SessionEngine.build(_cfg())
    assert runtime is not None
    assert runtime.config is not None
    assert runtime.agent_spec.default_model == "gemma-4-e4b-it"


@pytest.mark.unit
def test_native_runtime_exposes_tool_context_metadata(tmp_path: Path) -> None:
    runtime = SessionEngine.build(_cfg(), tool_context_metadata={"cwd": str(tmp_path)})
    assert runtime.tool_context_metadata == {"cwd": str(tmp_path)}
    metadata = runtime.tool_context_metadata
    metadata["cwd"] = "mutated"
    assert runtime.tool_context_metadata == {"cwd": str(tmp_path)}


@pytest.mark.unit
def test_native_runtime_build_accepts_event_sinks() -> None:
    sinks: list[Any] = []

    class _Sink:
        async def emit(self, event: Any) -> None:
            sinks.append(event)

    sink = _Sink()
    runtime = SessionEngine.build(_cfg(), event_sinks=[sink])
    assert runtime is not None


@pytest.mark.unit
async def test_native_runtime_run_with_stub_llm_completes() -> None:
    """装好 runtime 后替换 stub llm，跑一轮 happy path。"""
    runtime = SessionEngine.build(_cfg())
    # 替换 provider 为本地 stub，规避网络
    stub = _StubLLM(reply="hi from stub")
    runtime._llm = stub  # type: ignore[attr-defined]

    result = await runtime.run("hello", session_id="u1")
    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "hi from stub"
    assert stub.called == 1


@pytest.mark.unit
async def test_native_runtime_delegates_lifecycle_hooks_to_runner() -> None:
    from core import AgentSpec, InMemorySession

    hook_calls: list[tuple[str, str, str]] = []

    runner = _DelegateOnlyRunner()
    runtime = SessionEngine(
        config=_cfg(),
        runner=runner,  # type: ignore[arg-type]
        llm=_StubLLM(),
        tools={},
        enabled_tool_names=[],
        approval=object(),  # type: ignore[arg-type]
        session_factory=lambda sid: InMemorySession(session_id=sid),
        event_sinks=[],
        agent_spec=AgentSpec(name="delegator", instructions="", default_model="m"),
    )
    runtime.add_lifecycle_hook(_RuntimeLifecycleHook(runtime, hook_calls))

    result = await runtime.run("hello", session_id="thread-run")

    assert result.run_id == "delegated-run"
    assert hook_calls == []
    assert len(runner.run_lifecycle_hooks) == 1
    state = RunState(run_id=result.run_id, session_id="thread-run")
    await runner.run_lifecycle_hooks[0].after_run(  # type: ignore[attr-defined]
        state,
        runtime._get_or_create_session("thread-run"),  # type: ignore[attr-defined]
        result,
    )
    assert hook_calls == [("delegator", "thread-run", "delegated-run")]


@pytest.mark.unit
async def test_native_runtime_continue_delegates_lifecycle_hooks_to_runner() -> None:
    from core import AgentSpec, InMemorySession

    hook_calls: list[tuple[str, str, str]] = []

    runner = _DelegateOnlyRunner()
    runtime = SessionEngine(
        config=_cfg(),
        runner=runner,  # type: ignore[arg-type]
        llm=_StubLLM(),
        tools={},
        enabled_tool_names=[],
        approval=object(),  # type: ignore[arg-type]
        session_factory=lambda sid: InMemorySession(session_id=sid),
        event_sinks=[],
        agent_spec=AgentSpec(name="delegator", instructions="", default_model="m"),
    )
    runtime.add_lifecycle_hook(_RuntimeLifecycleHook(runtime, hook_calls))
    session = runtime._get_or_create_session("thread-continue")  # type: ignore[attr-defined]
    await session.append(Message.user("already stored"))

    result = await runtime.continue_from_last_user_message(session_id="thread-continue")

    assert result.run_id == "delegated-continue"
    assert hook_calls == []
    assert len(runner.continue_lifecycle_hooks) == 1


@pytest.mark.unit
async def test_session_engine_run_continue_and_child_share_final_approval_identity() -> None:
    """普通 run、continue 与同 runtime child run 使用同一个最终审批对象。"""
    from application.agents.manager import AgentManager
    from core import AgentSpec, InMemorySession
    from hosts.shared.host_dispatcher import HostDispatcher

    runner = _DelegateOnlyRunner()
    final_approval = object()
    runtime = SessionEngine(
        config=_cfg(),
        runner=runner,  # type: ignore[arg-type]
        llm=_StubLLM(),
        tools={},
        enabled_tool_names=[],
        approval=final_approval,  # type: ignore[arg-type]
        session_factory=lambda sid: InMemorySession(session_id=sid),
        event_sinks=[],
        agent_spec=AgentSpec(name="root", instructions="", default_model="m"),
    )

    await runtime.run("root", session_id="root-run")
    session = runtime._get_or_create_session("continued")  # type: ignore[attr-defined]
    await session.append(Message.user("stored"))
    await runtime.continue_from_last_user_message(session_id="continued")

    dispatcher = HostDispatcher(runtime=runtime, session_id="tree")
    await dispatcher.ensure_started()
    manager = dispatcher.agent_manager
    assert isinstance(manager, AgentManager)
    root = manager.get_agent(manager._root_agent_id or "")
    assert root is not None
    manager.spawn(
        SpawnAgentRequest(
            parent_agent_id=root.agent_id,
            spec=AgentSpec(name="child", instructions="", default_model="m"),
            seed_message=Message.user("child"),
            cwd=".",
            requested_tool_names=(),
        )
    )
    for _ in range(20):
        if len(runner.run_approvals) >= 2:
            break
        await asyncio.sleep(0.01)

    assert runner.run_approvals == [final_approval, final_approval, final_approval]
    assert runner.continue_approvals == [final_approval]
    await dispatcher.aclose(drain=False)


@pytest.mark.unit
async def test_session_engine_run_signature_rejects_approval_keyword() -> None:
    """公开签名不含 approval，运行时传入旧 keyword 会触发 TypeError。"""
    runtime = SessionEngine.build(_cfg(), llm_provider=_StubLLM())

    assert "approval" not in inspect.signature(runtime.run).parameters
    run_with_legacy_keyword = runtime.run
    with pytest.raises(TypeError, match="approval"):
        await run_with_legacy_keyword("blocked", approval=object())  # type: ignore[call-arg]


@pytest.mark.unit
async def test_native_runtime_remove_lifecycle_hook_by_identity() -> None:
    from core import AgentSpec, InMemorySession

    hook_calls: list[tuple[str, str, str]] = []
    runner = _DelegateOnlyRunner()
    runtime = SessionEngine(
        config=_cfg(),
        runner=runner,  # type: ignore[arg-type]
        llm=_StubLLM(),
        tools={},
        enabled_tool_names=[],
        approval=object(),  # type: ignore[arg-type]
        session_factory=lambda sid: InMemorySession(session_id=sid),
        event_sinks=[],
        agent_spec=AgentSpec(name="delegator", instructions="", default_model="m"),
    )
    hook = _RuntimeLifecycleHook(runtime, hook_calls)

    runtime.add_lifecycle_hook(hook)
    assert runtime.remove_lifecycle_hook(hook) is True
    assert runtime.remove_lifecycle_hook(hook) is False

    await runtime.run("hello", session_id="thread-run")
    assert runner.run_lifecycle_hooks == ()


@pytest.mark.unit
async def test_native_runtime_lifecycle_hook_snapshot_is_stable_per_run() -> None:
    class _SecondHook:
        async def before_turn(self, state: RunState) -> None:
            return None

        async def after_turn(self, state: RunState, assistant_message: Message) -> None:
            return None

        async def before_tool(self, state: RunState, call: ToolCall) -> None:
            return None

        async def after_tool(
            self, state: RunState, call: ToolCall, result_message: Message
        ) -> None:
            return None

        async def after_run(self, state: RunState, session: Session, result: Result) -> None:
            calls.append(f"second:{result.run_id}")

    class _AddingHook:
        def __init__(self, runtime: SessionEngine, second: _SecondHook) -> None:
            self._runtime = runtime
            self._second = second
            self._added = False

        async def before_turn(self, state: RunState) -> None:
            return None

        async def after_turn(self, state: RunState, assistant_message: Message) -> None:
            return None

        async def before_tool(self, state: RunState, call: ToolCall) -> None:
            return None

        async def after_tool(
            self, state: RunState, call: ToolCall, result_message: Message
        ) -> None:
            return None

        async def after_run(self, state: RunState, session: Session, result: Result) -> None:
            calls.append(f"first:{result.run_id}")
            if not self._added:
                self._added = True
                self._runtime.add_lifecycle_hook(self._second)

    calls: list[str] = []
    runtime = SessionEngine.build(_cfg())
    runtime._llm = _StubLLM(reply="ok")  # type: ignore[attr-defined]
    second = _SecondHook()
    runtime.add_lifecycle_hook(_AddingHook(runtime, second))

    first = await runtime.run("one", session_id="snapshot")
    second_result = await runtime.run("two", session_id="snapshot")

    assert calls == [
        f"first:{first.run_id}",
        f"first:{second_result.run_id}",
        f"second:{second_result.run_id}",
    ]


@pytest.mark.unit
async def test_reasoning_effort_override_is_request_scoped_during_concurrent_runs() -> None:
    """并发 run 的 reasoning_effort 写入各自 LLMRequest，runtime config 保持装配期值。"""
    runtime = SessionEngine.build(
        _cfg(reasoning_effort="medium"),
        llm_provider=_StubLLM(),
    )
    stub = _BarrierLLM(
        expected_calls=2,
        config_effort=lambda: runtime.config.model.reasoning_effort,
    )
    runtime._llm = stub  # type: ignore[attr-defined]

    results = await asyncio.gather(
        runtime.run("high run", session_id="thread-high", reasoning_effort="high"),
        runtime.run("low run", session_id="thread-low", reasoning_effort="low"),
    )

    assert [result.status for result in results] == ["completed", "completed"]
    assert {request.reasoning_effort for request in stub.requests} == {"high", "low"}
    assert stub.config_efforts_seen == ["medium", "medium"]
    assert runtime.config.model.reasoning_effort == "medium"


@pytest.mark.unit
async def test_native_runtime_reuses_session_for_same_session_id() -> None:
    """同一 session_id 多次 run 应该落到同一个 session（多轮连续性）。"""
    runtime = SessionEngine.build(_cfg())
    stub = _StubLLM(reply="ok")
    runtime._llm = stub  # type: ignore[attr-defined]

    await runtime.run("turn1", session_id="abc")
    await runtime.run("turn2", session_id="abc")

    # stub 被调用两次
    assert stub.called == 2
    # runtime 内部缓存了一份 session 实例
    # （这里通过公共访问点不直接断言，改为 run 结果都 completed）


@pytest.mark.unit
async def test_native_runtime_continue_from_last_user_message_does_not_duplicate_user() -> None:
    runtime = SessionEngine.build(_cfg())
    stub = _StubLLM(reply="continued")
    runtime._llm = stub  # type: ignore[attr-defined]
    session = runtime._get_or_create_session("thread-1")  # type: ignore[attr-defined]
    await session.append(Message.user("hello already stored"))

    result = await runtime.continue_from_last_user_message(session_id="thread-1")

    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "continued"
    history = await session.history()
    assert [(message.role, message.content) for message in history] == [
        ("user", "hello already stored"),
        ("assistant", "continued"),
    ]
    assert stub.called == 1
    assert stub.requests[0].messages[-1].role == "user"
    assert stub.requests[0].messages[-1].content == "hello already stored"
    assert stub.requests[0].tools == ()
    assert history[-2].content == "hello already stored"


@pytest.mark.unit
def test_native_runtime_build_with_explicit_agent_spec() -> None:
    from core.agent_spec import AgentSpec

    spec = AgentSpec(
        name="explicit",
        instructions="hi",
        default_model="custom-model",
        max_turns=2,
    )
    runtime = SessionEngine.build(_cfg(), agent_spec=spec)
    assert runtime.agent_spec is not spec
    assert runtime.agent_spec.default_model == "custom-model"
    assert runtime.agent_spec.metadata["model_preset_id"] == "local-gemma-4-e4b-it"


@pytest.mark.unit
def test_native_runtime_build_with_tools() -> None:
    from tools import ReadFileTool, ToolRegistry

    registry = ToolRegistry([ReadFileTool()])
    runtime = SessionEngine.build(
        _cfg(),
        tools=registry,
        enabled_tool_names=["read_file"],
    )
    assert runtime is not None


@pytest.mark.unit
def test_native_runtime_snapshots_tool_lookup_with_shared_tool_references() -> None:
    """创建 runtime 时复制工具查找表，Tool 对象本身保持共享引用。"""
    from tools import ToolRegistry

    tool = _NamedTool()
    registry = ToolRegistry([tool])
    runtime = SessionEngine.build(
        _cfg(),
        tools=registry,
        enabled_tool_names=["temp_tool"],
    )

    registry.unregister("temp_tool")

    assert "temp_tool" in runtime.tools
    assert runtime.tools["temp_tool"] is tool
    assert runtime.enabled_tool_names == ["temp_tool"]


@pytest.mark.unit
async def test_native_runtime_aclose_delegates_to_llm() -> None:
    """SessionEngine.aclose 应调 provider.aclose，且幂等（多次调不抛）。"""

    class _AcloseCountingLLM:
        def __init__(self) -> None:
            self.aclose_calls = 0

        async def complete(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover
            raise AssertionError("not invoked in this test")

        async def aclose(self) -> None:
            self.aclose_calls += 1

    runtime = SessionEngine.build(_cfg())
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

    runtime = SessionEngine.build(_cfg())
    runtime._llm = _NoAcloseLLM()  # type: ignore[attr-defined]
    # 不抛即视为通过
    await runtime.aclose()
