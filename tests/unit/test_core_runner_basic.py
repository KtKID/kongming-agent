"""unit：core.runner happy path + max_turns 保护。

只覆盖 runner 最核心的两条单测级行为：

1. 一轮 happy path → ``Result.status == "completed"``
2. 永远发 tool_call 但没有对应 tool 时，``max_turns`` 触发
   :class:`MaxTurnsExceededError` 并被收口到 ``Result.status == "failed"``
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from core import AgentSpec, InMemorySession, Runner, ToolCall
from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    AssembledInput,
    Event,
    LLMRequest,
    LLMResponse,
    PreparedToolCall,
    ProviderUsageFamily,
    ProviderUsageSnapshot,
    ToolContext,
    ToolExecutionScope,
    ToolResult,
)
from core.errors import MaxTurnsExceededError
from core.message import Message
from infrastructure.llm_providers.usage import ProviderUsageManager
from scheduler.store import Store
from tools import ShellTool, WriteFileTool
from tools.agent_workflow_tool import (
    AgentWorkflowHandle,
    build_run_agent_workflow_tool,
)
from tools.builtin.schedule_tool import build_schedule_tool
from tools.runtime.base import BaseBuiltinTool


class _StubLLM:
    """本地 stub，避免 unit 层 import e2e/conftest。"""

    def __init__(
        self,
        responses: list[tuple[str | None, list[ToolCall] | None]] | None = None,
        usage: dict | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._usage: ProviderUsageSnapshot | None = (
            ProviderUsageManager().normalize(
                family=ProviderUsageFamily.OPENAI_CHAT_COMPLETIONS,
                raw_usage=usage,
            )
            if usage is not None
            else None
        )
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if not self._responses:
            return LLMResponse(
                message=Message(role="assistant", content=""),
                finish_reason="stop",
                usage=self._usage,
            )
        content, tool_calls = self._responses.pop(0)
        calls_tuple = tuple(tool_calls) if tool_calls else None
        msg = Message(role="assistant", content=content, tool_calls=calls_tuple)
        finish = "tool_calls" if calls_tuple else "stop"
        return LLMResponse(message=msg, finish_reason=finish, usage=self._usage)


class _BarrierLLM:
    """等待两个并发 complete 都进入后再返回，用于制造重叠 run。"""

    def __init__(self) -> None:
        self._entered = 0
        self._all_entered = asyncio.Event()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        del request
        self._entered += 1
        if self._entered == 2:
            self._all_entered.set()
        await asyncio.wait_for(self._all_entered.wait(), timeout=1)
        return LLMResponse(
            message=Message(role="assistant", content="ok"),
            finish_reason="stop",
        )


class _AllowApproval:
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(outcome="approved")


class _DenyApproval:
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        del request
        return ApprovalDecision(outcome="denied", reason="test denial")


class _CapturingApproval:
    def __init__(self) -> None:
        self.requests: list[ApprovalRequest] = []

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(outcome="approved")


class _RecordingTool:
    name = "capture"
    description = "capture context"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.contexts: list[ToolContext] = []
        self.arguments: list[dict[str, Any]] = []

    async def execute(
        self,
        prepared: PreparedToolCall,
        ctx: ToolContext,
    ) -> ToolResult:
        self.arguments.append(dict(prepared.arguments))
        self.contexts.append(ctx)
        return ToolResult(ok=True, content="ok")


class _SinglePreparationTool(BaseBuiltinTool):
    """第一次 prepare 产出 A，第二次产出 B，用于识别双 owner。"""

    name = "single_prepare"
    description = "single preparation adversarial spy"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"nested": {"type": "object"}},
        "required": ["nested"],
    }

    def __init__(self) -> None:
        self.prepare_count = 0
        self.validation_count = 0
        self.executed_arguments: list[dict[str, Any]] = []

    def _validate_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """记录审批前校验次数，执行阶段再次校验会使计数超过 1。"""
        self.validation_count += 1
        return super()._validate_args(args)

    def prepare(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> PreparedToolCall:
        """按调用次数返回语义相反的 A/B prepared 快照。"""
        self._validate_args(arguments)
        del context
        self.prepare_count += 1
        marker = "A" if self.prepare_count == 1 else "B"
        return PreparedToolCall(
            arguments={"nested": {"marker": marker}},
            execution_scope=ToolExecutionScope(cwd=f"/scope/{marker}"),
        )

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """记录执行真正消费的 prepared arguments。"""
        del ctx
        self.executed_arguments.append(args)
        return "ok", {"arguments": args}


class _MutatingApproval:
    """记录审批事实，并把自己的 nested 副本改成 C。"""

    def __init__(self) -> None:
        self.seen_before_mutation: list[dict[str, Any]] = []

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        nested = request.arguments["nested"]
        assert isinstance(nested, dict)
        self.seen_before_mutation.append(
            {
                "arguments": {"nested": {"marker": nested["marker"]}},
                "cwd": request.execution_scope.cwd,
            }
        )
        nested["marker"] = "C"
        return ApprovalDecision(outcome="approved")


class _CapturingWorkflowManager:
    """记录 workflow 真正消费的 canonical payload，随后终止执行。"""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root
        self.received: dict[str, Any] | None = None

    async def run_workflow_payload(
        self,
        *,
        mode: str,
        parent_session_id: str,
        payload: dict[str, Any],
        parent_agent: dict[str, object] | None,
    ) -> object:
        del parent_session_id, parent_agent
        self.received = {"mode": mode, "payload": deepcopy(payload)}
        raise RuntimeError("captured canonical workflow payload")


class _ScheduleThreadProvisioner:
    """为 Runner schedule smoke 提供真实 SchedulerManager 所需 thread 门户。"""

    async def create_scheduled_task_thread(
        self,
        *,
        task_id: str,
        name: str,
        preset_id: str,
        cwd: str = "",
    ) -> str:
        del task_id, name, preset_id, cwd
        return "thread-eeeeeeeeeeee"

    async def delete_thread(self, thread_id: str, *, keep_history: bool = False) -> None:
        del thread_id, keep_history


class _RecordingSink:
    """记录收到的 Event，供 run-scoped sink 断言。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


class _CaptureAssembler:
    """测试用 assembler，记录输入来源并注入 system 消息。"""

    def __init__(self) -> None:
        """初始化 assembler，输入为空，输出为可记录 sources 的实例。"""
        self.sources: list[list[object]] = []

    async def assemble(
        self,
        history: list[Message],
        instructions: list[object] = (),
    ) -> AssembledInput:
        """装配消息，输入为历史和指令来源，输出含 system 的消息列表。"""
        self.sources.append(list(instructions))
        system_text = "\n".join(str(source.content) for source in instructions)
        messages = [Message.system(system_text), *history] if system_text else list(history)
        return AssembledInput(
            messages=messages,
            metadata={"original_count": len(history), "compacted_count": len(messages)},
            system_message=messages[0] if system_text else None,
        )


@pytest.mark.unit
async def test_runner_happy_path_single_turn() -> None:
    llm = _StubLLM([("hello", None)])
    runner = Runner()
    session = InMemorySession("u")
    spec = AgentSpec(name="t", instructions="s", default_model="m", max_turns=3)

    result = await runner.run(
        "hi",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={},
        approval=_AllowApproval(),
    )

    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "hello"
    assert result.turn_count == 1
    assert result.error is None


@pytest.mark.unit
async def test_runner_passes_request_level_model_parameters_to_llm_request() -> None:
    """请求级模型参数写入 LLMRequest，输入为 run 覆盖参数，输出为 fake LLM 调用断言。"""
    llm = _StubLLM([("hello", None)])
    runner = Runner()
    session = InMemorySession("u")
    spec = AgentSpec(name="t", instructions="s", default_model="m", max_turns=3)

    result = await runner.run(
        "hi",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={},
        approval=_AllowApproval(),
        max_tokens=131072,
        temperature=0.2,
        timeout_seconds=900,
        llm_request_metadata={
            "token_parameter_name": "max_completion_tokens",
            "provider_extra": {"top_p": 0.95},
        },
    )

    assert result.status == "completed"
    request = llm.calls[0]
    assert request.max_tokens == 131072
    assert request.temperature == 0.2
    assert request.timeout_seconds == 900
    assert request.metadata["thread_id"] == "u"
    assert request.metadata["token_parameter_name"] == "max_completion_tokens"
    assert request.metadata["provider_extra"] == {"top_p": 0.95}


@pytest.mark.unit
async def test_runner_stores_conversation_references_in_user_metadata() -> None:
    """runner 写入本轮 conversation references，供 prompt assembly 后续注入。"""
    llm = _StubLLM([("hello", None)])
    runner = Runner()
    session = InMemorySession("u")
    spec = AgentSpec(name="t", instructions="s", default_model="m", max_turns=3)
    refs = [
        {
            "id": "ref-1",
            "kind": "skill",
            "ref": "skill:skill-creator",
            "label": "Skill Creator",
            "activation": "inject_context",
        }
    ]

    result = await runner.run(
        "hi",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={},
        approval=_AllowApproval(),
        references=refs,
    )
    history = await session.history()
    user_messages = [msg for msg in history if msg.role == "user"]

    assert result.status == "completed"
    assert user_messages[0].metadata["conversation_references"] == refs


@pytest.mark.unit
async def test_runner_assembler_uses_per_run_agent_spec_instructions() -> None:
    """同一个 runner 跑子 agent 时，assembler 必须使用本次 agent_spec 指令。"""
    llm = _StubLLM([("ok", None)])
    assembler = _CaptureAssembler()
    runner = Runner(
        input_assembler=assembler,
        instruction_sources=[type("Source", (), {"origin": "", "content": "parent tools"})()],
    )
    session = InMemorySession("child")
    spec = AgentSpec(name="child", instructions="child-only instructions", default_model="m")

    result = await runner.run(
        "task",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={},
        approval=_AllowApproval(),
    )

    assert result.status == "completed"
    assert assembler.sources
    assert getattr(assembler.sources[0][0], "content") == "child-only instructions"
    assert llm.calls[0].messages[0].content == "child-only instructions"


@pytest.mark.unit
async def test_runner_max_turns_exceeded_is_captured_in_result() -> None:
    """模型无限发 tool_call 且找不到 tool，runner 达到 max_turns 会失败收口。"""
    # 每一轮都返回一个会触发 "tool not registered" 的 tool call
    responses = [
        (None, [ToolCall(call_id=f"c{i}", tool_name="ghost", arguments={})]) for i in range(10)
    ]
    llm = _StubLLM(responses)

    runner = Runner()
    session = InMemorySession("u")
    spec = AgentSpec(name="t", instructions="", default_model="m", max_turns=2)

    result = await runner.run(
        "hi",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={},
        approval=_AllowApproval(),
    )

    assert result.status == "failed"
    assert isinstance(result.error, MaxTurnsExceededError)
    assert result.final_message is None


@pytest.mark.unit
async def test_runner_resolves_tool_names_from_spec() -> None:
    """spec.tool_names 里声明的 tool 必须能在 ToolLookup 里找到，否则直接失败。"""
    llm = _StubLLM([("ok", None)])
    runner = Runner()
    session = InMemorySession("u")
    spec = AgentSpec(
        name="t",
        instructions="",
        default_model="m",
        tool_names=("unknown_tool",),
        max_turns=3,
    )

    result = await runner.run(
        "hi",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={},
        approval=_AllowApproval(),
    )

    assert result.status == "failed"
    assert result.error is not None
    assert "unknown_tool" in result.error.message


@pytest.mark.unit
async def test_runner_passes_tool_context_metadata_to_approval_and_tool(tmp_path) -> None:
    """Runner 把 root thread 与 run metadata 同步传给审批、工具和模型。"""
    llm = _StubLLM(
        [
            (None, [ToolCall(call_id="call-1", tool_name="capture", arguments={})]),
            ("done", None),
        ]
    )
    tool = _RecordingTool()
    approval = _CapturingApproval()
    runner = Runner(
        tool_context_metadata={
            "cwd": str(tmp_path),
            "scope": "default",
            "thread_id": "stale-session",
        }
    )
    session = InMemorySession("u")
    spec = AgentSpec(
        name="main-agent",
        instructions="",
        default_model="parent-model",
        max_turns=3,
        reasoning_effort="high",
    )

    result = await runner.run(
        "hi",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={"capture": tool},
        approval=approval,
        enabled_tools=[tool],
        tool_context_metadata={"scope": "run"},
        llm_request_metadata={"thread_id": "child-session-must-not-win"},
        thread_id="thread-root",
        max_tokens=8192,
        temperature=0.2,
        timeout_seconds=90.0,
    )

    assert result.status == "completed"
    assert len(approval.requests) == 1
    assert approval.requests[0].session_id == "u"
    assert approval.requests[0].metadata["thread_id"] == "thread-root"
    assert approval.requests[0].metadata["cwd"] == str(tmp_path)
    assert approval.requests[0].metadata["scope"] == "run"
    assert len(tool.contexts) == 1
    assert tool.contexts[0].session_id == "u"
    assert tool.contexts[0].metadata["thread_id"] == "thread-root"
    assert tool.contexts[0].metadata["cwd"] == str(tmp_path)
    assert tool.contexts[0].metadata["scope"] == "run"
    parent_agent = tool.contexts[0].metadata["parent_agent"]
    assert parent_agent["run_id"] == result.run_id
    assert parent_agent["session_id"] == "u"
    assert parent_agent["agent"] == "main-agent"
    assert parent_agent["model"] == "parent-model"
    assert parent_agent["reasoning_effort"] == "high"
    assert parent_agent["effective_max_turns"] == 3
    assert parent_agent["max_tokens"] == 8192
    assert parent_agent["temperature"] == 0.2
    assert parent_agent["timeout_seconds"] == 90.0
    assert parent_agent["agent_spec"]["default_model"] == "parent-model"
    assert llm.calls[0].metadata["thread_id"] == "thread-root"


@pytest.mark.unit
async def test_runner_plain_tool_gets_copied_arguments_and_empty_scope() -> None:
    """普通 Tool 保持既有执行语义，并获得空 execution scope。"""
    source_arguments = {"value": ["original"]}
    llm = _StubLLM(
        [
            (
                None,
                [
                    ToolCall(
                        call_id="capture-1",
                        tool_name="capture",
                        arguments=source_arguments,
                    )
                ],
            ),
            ("done", None),
        ]
    )
    approval = _CapturingApproval()
    tool = _RecordingTool()

    result = await Runner().run(
        "hi",
        session=InMemorySession("plain-tool"),
        agent_spec=AgentSpec(name="t", instructions="", default_model="m", max_turns=3),
        llm=llm,
        tools={"capture": tool},
        enabled_tools=[tool],
        approval=approval,
    )

    source_arguments["value"].append("mutated")
    assert result.status == "completed"
    assert approval.requests[0].execution_scope.cwd is None
    assert approval.requests[0].arguments == {"value": ["original"]}
    assert tool.arguments == [{"value": ["original"]}]


@pytest.mark.unit
async def test_runner_approval_and_shell_execution_share_prepared_cwd(tmp_path) -> None:
    """审批 arguments/scope 与 ShellTool 执行 data 使用同一个 canonical cwd。"""
    child = tmp_path / "child"
    child.mkdir()
    llm = _StubLLM(
        [
            (
                None,
                [
                    ToolCall(
                        call_id="shell-1",
                        tool_name="run_shell",
                        arguments={"command": "pwd", "cwd": "child"},
                    )
                ],
            ),
            ("done", None),
        ]
    )
    approval = _CapturingApproval()
    tool = ShellTool()
    session = InMemorySession("u")

    result = await Runner().run(
        "hi",
        session=session,
        agent_spec=AgentSpec(name="t", instructions="", default_model="m", max_turns=3),
        llm=llm,
        tools={"run_shell": tool},
        enabled_tools=[tool],
        approval=approval,
        tool_context_metadata={"cwd": tmp_path.as_posix()},
    )
    history = await session.history()
    tool_message = next(message for message in history if message.role == "tool")
    expected_cwd = child.resolve().as_posix()

    assert result.status == "completed"
    assert len(approval.requests) == 1
    assert approval.requests[0].arguments["cwd"] == expected_cwd
    assert approval.requests[0].execution_scope.cwd == expected_cwd
    assert tool_message.metadata["data"]["cwd"] == expected_cwd


@pytest.mark.unit
async def test_runner_prepares_once_and_isolates_approval_from_execution() -> None:
    """A/B/C 哨兵固定 prepare=1、审批/执行等值且内存隔离。"""
    llm = _StubLLM(
        [
            (
                None,
                [
                    ToolCall(
                        call_id="prepared-1",
                        tool_name="single_prepare",
                        arguments={"nested": {"marker": "model"}},
                    )
                ],
            ),
            ("done", None),
        ]
    )
    tool = _SinglePreparationTool()
    approval = _MutatingApproval()

    result = await Runner().run(
        "hi",
        session=InMemorySession("prepared-once"),
        agent_spec=AgentSpec(name="t", instructions="", default_model="m", max_turns=3),
        llm=llm,
        tools={tool.name: tool},
        enabled_tools=[tool],
        approval=approval,
    )

    assert result.status == "completed"
    assert tool.prepare_count == 1
    assert tool.validation_count == 1
    assert approval.seen_before_mutation == [
        {"arguments": {"nested": {"marker": "A"}}, "cwd": "/scope/A"}
    ]
    assert tool.executed_arguments == [{"nested": {"marker": "A"}}]


@pytest.mark.unit
async def test_runner_write_file_approval_matches_canonical_execution(tmp_path: Path) -> None:
    """真实 WriteFile 在审批前冻结绝对路径与 append 默认值。"""
    tool = WriteFileTool()
    approval = _CapturingApproval()
    target = tmp_path / "nested" / "note.txt"
    llm = _StubLLM(
        [
            (
                None,
                [
                    ToolCall(
                        call_id="write-1",
                        tool_name=tool.name,
                        arguments={"path": "nested/note.txt", "content": "hello"},
                    )
                ],
            ),
            ("done", None),
        ]
    )

    result = await Runner().run(
        "hi",
        session=InMemorySession("write-prepared"),
        agent_spec=AgentSpec(name="t", instructions="", default_model="m", max_turns=3),
        llm=llm,
        tools={tool.name: tool},
        enabled_tools=[tool],
        approval=approval,
        tool_context_metadata={"cwd": str(tmp_path)},
    )

    assert result.status == "completed"
    assert approval.requests[0].arguments == {
        "path": str(target),
        "content": "hello",
        "append": False,
    }
    assert target.read_text(encoding="utf-8") == "hello"


@pytest.mark.unit
async def test_runner_workflow_approval_matches_normalized_execution(tmp_path: Path) -> None:
    """真实 workflow tool 在审批前补齐 task_flow 默认结构。"""
    handle = AgentWorkflowHandle()
    manager = _CapturingWorkflowManager(tmp_path)
    handle.bind(manager)
    tool = build_run_agent_workflow_tool(handle)
    approval = _CapturingApproval()
    llm = _StubLLM(
        [
            (
                None,
                [
                    ToolCall(
                        call_id="workflow-1",
                        tool_name=tool.name,
                        arguments={
                            "mode": " task_flow ",
                            "payload": {
                                "objective": " deliver ",
                                "plan": {"nodes": []},
                            },
                        },
                    )
                ],
            ),
            ("done", None),
        ]
    )

    result = await Runner().run(
        "hi",
        session=InMemorySession("workflow-prepared"),
        agent_spec=AgentSpec(name="t", instructions="", default_model="m", max_turns=3),
        llm=llm,
        tools={tool.name: tool},
        enabled_tools=[tool],
        approval=approval,
    )

    approved = approval.requests[0].arguments
    assert result.status == "completed"
    assert approved["mode"] == "task_flow"
    assert approved["payload"]["objective"] == "deliver"
    assert approved["payload"]["planning"] == {
        "interaction_mode": "llm_decide",
        "choice_policy": "ask_when_multiple_viable_paths",
    }
    assert manager.received == approved


@pytest.mark.unit
async def test_runner_schedule_approval_matches_frozen_create_defaults(tmp_path: Path) -> None:
    """真实 ScheduleTool 在审批前冻结默认值、trigger 和 next_run_at。"""
    store = Store(home_dir=tmp_path / "cron")
    tool = build_schedule_tool(
        store,
        default_timezone="Asia/Shanghai",
        default_preset_id="preset-default",
        thread_provisioner=_ScheduleThreadProvisioner(),
    )
    approval = _CapturingApproval()
    llm = _StubLLM(
        [
            (
                None,
                [
                    ToolCall(
                        call_id="schedule-1",
                        tool_name=tool.name,
                        arguments={
                            "action": "create",
                            "name": "daily",
                            "schedule": "12 10 * * *",
                            "input": "run report",
                        },
                    )
                ],
            ),
            ("done", None),
        ]
    )

    result = await Runner().run(
        "hi",
        session=InMemorySession("schedule-prepared"),
        agent_spec=AgentSpec(name="t", instructions="", default_model="m", max_turns=3),
        llm=llm,
        tools={tool.name: tool},
        enabled_tools=[tool],
        approval=approval,
    )

    approved = approval.requests[0].arguments
    created = store.list_tasks()[0]
    assert result.status == "completed"
    assert approved["agent"] == "default"
    assert approved["preset"] == "preset-default"
    assert approved["timezone"] == "Asia/Shanghai"
    assert approved["concurrency"] == "forbid"
    assert approved["trigger_type"] == created.trigger.trigger_type.value
    assert approved["trigger_expr"] == created.trigger.expr
    assert approved["next_run_at"] == created.next_run_at


@pytest.mark.unit
async def test_runner_rejection_keeps_single_preparation_and_skips_execution() -> None:
    """审批拒绝仍只 prepare 一次，并保持执行调用数为零。"""
    llm = _StubLLM(
        [
            (
                None,
                [
                    ToolCall(
                        call_id="prepared-denied",
                        tool_name="single_prepare",
                        arguments={"nested": {"marker": "model"}},
                    )
                ],
            ),
            ("done", None),
        ]
    )
    tool = _SinglePreparationTool()

    result = await Runner().run(
        "hi",
        session=InMemorySession("prepared-denied"),
        agent_spec=AgentSpec(name="t", instructions="", default_model="m", max_turns=3),
        llm=llm,
        tools={tool.name: tool},
        enabled_tools=[tool],
        approval=_DenyApproval(),
    )

    assert result.status == "completed"
    assert tool.prepare_count == 1
    assert tool.validation_count == 1
    assert tool.executed_arguments == []


@pytest.mark.unit
async def test_runner_preparation_failure_skips_approval_and_execution() -> None:
    """cwd preparation 失败直接产生 tool_result，审批端不会看到无效请求。"""
    llm = _StubLLM(
        [
            (
                None,
                [
                    ToolCall(
                        call_id="shell-1",
                        tool_name="run_shell",
                        arguments={"command": "pwd"},
                    )
                ],
            ),
            ("done", None),
        ]
    )
    approval = _CapturingApproval()
    session = InMemorySession("u")
    tool = ShellTool()

    result = await Runner().run(
        "hi",
        session=session,
        agent_spec=AgentSpec(name="t", instructions="", default_model="m", max_turns=3),
        llm=llm,
        tools={"run_shell": tool},
        enabled_tools=[tool],
        approval=approval,
    )
    history = await session.history()
    tool_message = next(message for message in history if message.role == "tool")

    assert result.status == "completed"
    assert approval.requests == []
    assert tool_message.metadata["reason"] == "tool_preparation_failed"
    assert tool_message.metadata["preparation_error"]["code"] == "cwd_unavailable"


@pytest.mark.unit
async def test_runner_usage_in_result_metadata() -> None:
    """runner 把 usage 累计写入 Result.metadata['usage']。"""
    llm = _StubLLM(
        [("hello", None)],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    runner = Runner()
    session = InMemorySession("u")
    spec = AgentSpec(name="t", instructions="s", default_model="m", max_turns=3)

    result = await runner.run(
        "hi",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={},
        approval=_AllowApproval(),
    )

    assert result.status == "completed"
    usage = result.metadata.get("usage")
    assert isinstance(usage, dict)
    metrics = usage["metrics"]
    assert metrics["input_total_tokens"]["value"] == 10
    assert metrics["output_total_tokens"]["value"] == 5
    assert metrics["total_tokens"]["value"] == 15


@pytest.mark.unit
async def test_run_scoped_event_sinks_do_not_cross_concurrent_runs() -> None:
    """同一个 Runner 并发 run 时，临时 sink 只接收本次 run 的事件。"""
    llm = _BarrierLLM()
    base_sink = _RecordingSink()
    run_a_sink = _RecordingSink()
    run_b_sink = _RecordingSink()
    runner = Runner(event_sinks=[base_sink])
    spec = AgentSpec(name="t", instructions="s", default_model="m", max_turns=3)

    result_a, result_b = await asyncio.gather(
        runner.run(
            "a",
            session=InMemorySession("a"),
            agent_spec=spec,
            llm=llm,
            tools={},
            approval=_AllowApproval(),
            run_id="run-a",
            event_sinks=[run_a_sink],
        ),
        runner.run(
            "b",
            session=InMemorySession("b"),
            agent_spec=spec,
            llm=llm,
            tools={},
            approval=_AllowApproval(),
            run_id="run-b",
            event_sinks=[run_b_sink],
        ),
    )

    assert result_a.status == "completed"
    assert result_b.status == "completed"
    assert {event.run_id for event in run_a_sink.events} == {"run-a"}
    assert {event.run_id for event in run_b_sink.events} == {"run-b"}
    assert {event.run_id for event in base_sink.events} == {"run-a", "run-b"}


@pytest.mark.unit
async def test_runner_appends_user_message_to_session() -> None:
    llm = _StubLLM([("ok", None)])
    runner = Runner()
    session = InMemorySession("u")
    spec = AgentSpec(name="t", instructions="SYS", default_model="m", max_turns=2)

    await runner.run(
        "my-input",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={},
        approval=_AllowApproval(),
    )

    history = await session.history()
    user_msgs = [m for m in history if m.role == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0].content == "my-input"


@pytest.mark.unit
async def test_runner_keeps_user_message_content_when_tools_are_available() -> None:
    """LLM request 保持用户原文，工具只通过 schema 下发。"""
    llm = _StubLLM([("ok", None)])
    tool = _RecordingTool()
    runner = Runner()
    session = InMemorySession("u")
    spec = AgentSpec(name="t", instructions="", default_model="m", max_turns=2)

    result = await runner.run(
        "hello",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={"capture": tool},
        approval=_AllowApproval(),
        enabled_tools=[tool],
    )

    assert result.status == "completed"
    assert llm.calls[0].messages[-1].content == "hello"
    assert [tool.name for tool in llm.calls[0].tools] == ["capture"]
    history = await session.history()
    user_msgs = [m for m in history if m.role == "user"]
    assert [m.content for m in user_msgs] == ["hello"]


@pytest.mark.unit
async def test_runner_reports_unavailable_tool_to_llm_with_legacy_metadata() -> None:
    """模型调用关闭或已删除工具时，工具结果向 LLM 说明不可用并保留旧诊断字段。"""
    llm = _StubLLM(
        [
            (None, [ToolCall(call_id="call-ghost", tool_name="ghost", arguments={})]),
            ("done", None),
        ]
    )
    sink = _RecordingSink()
    runner = Runner(event_sinks=[sink])
    session = InMemorySession("u")
    spec = AgentSpec(name="t", instructions="", default_model="m", max_turns=3)

    result = await runner.run(
        "hi",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={},
        approval=_AllowApproval(),
    )

    assert result.status == "completed"
    history = await session.history()
    tool_messages = [m for m in history if m.role == "tool"]
    assert len(tool_messages) == 1
    tool_message = tool_messages[0]
    assert tool_message.content is not None
    assert "工具不可用" in tool_message.content
    assert "tool 'ghost' not registered" in tool_message.content
    assert tool_message.metadata["error_message"] == "tool 'ghost' not registered"
    assert tool_message.metadata["unavailable"] is True
    assert tool_message.metadata["reason"] == "tool_unavailable"
    end_events = [event for event in sink.events if event.kind == "tool.call.end"]
    assert end_events
    assert end_events[0].payload["reason"] == "tool_unavailable"
