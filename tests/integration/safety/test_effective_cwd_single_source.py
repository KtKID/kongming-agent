"""真实 Runner → Safety → Shell 链的 effective cwd 单一真源红线测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from core import AgentSpec, InMemorySession, Runner, ToolCall
from core.contracts import ApprovalDecision, ApprovalRequest, Event, LLMRequest, LLMResponse
from core.message import Message
from infrastructure.config.models import Config, ModelSelectionConfig
from safety.approval.chain import build_safety_chain
from safety.approval.permissions_manager import PermissionsManager
from safety.auto_approval.disposition import ApprovalDispositionMode
from safety.guards.danger import DangerGuard
from tools import ShellTool


class _ShellOverrideLLM:
    """首轮请求在 B 目录执行 Shell，次轮返回终态文本。"""

    def __init__(self, *, command: str, effective_cwd: Path) -> None:
        self._command = command
        self._effective_cwd = effective_cwd
        self._turn = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """通过 fake LLM 固定外部输入，内部 Runner 与 Safety 保持真实。"""
        del request
        self._turn += 1
        if self._turn == 1:
            return LLMResponse(
                message=Message(
                    role="assistant",
                    tool_calls=(
                        ToolCall(
                            call_id="shell-effective-cwd",
                            tool_name="run_shell",
                            arguments={
                                "command": self._command,
                                "cwd": self._effective_cwd.as_posix(),
                            },
                        ),
                    ),
                ),
                finish_reason="tool_calls",
            )
        return LLMResponse(
            message=Message(role="assistant", content="done"),
            finish_reason="stop",
        )


@dataclass
class _RecordingApproval:
    """记录真实 Safety 送来的人审请求并返回拒绝。"""

    requests: list[ApprovalRequest] = field(default_factory=list)

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """拒绝调用，使审批前 subprocess 副作用保持可验证。"""
        self.requests.append(request)
        return ApprovalDecision(outcome="rejected", metadata={"decision_source": "user"})


@dataclass
class _RecordingModeResolver:
    """让 runtime A 与 effective B 导向相反结果并记录查询坐标。"""

    modes: dict[str, ApprovalDispositionMode]
    seen_cwds: list[str] = field(default_factory=list)

    def mode_for(self, cwd: str) -> ApprovalDispositionMode:
        """记录决策引擎实际使用的 cwd。"""
        self.seen_cwds.append(cwd)
        return self.modes[cwd]


@dataclass
class _RecordingEventSink:
    """记录真实 Safety 事件中的 prepared execution scope。"""

    events: list[Event] = field(default_factory=list)

    async def emit(self, event: Event) -> None:
        """保存单条审计事件。"""
        self.events.append(event)


@pytest.mark.e2e
async def test_runner_safety_and_shell_share_prepared_effective_cwd(
    tmp_path: Path,
) -> None:
    """runtime=A、Shell=B 时 mode、人审与执行边界必须统一使用 B。"""
    runtime_cwd = tmp_path / "runtime-a"
    effective_cwd = tmp_path / "shell-b"
    runtime_cwd.mkdir()
    effective_cwd.mkdir()
    runtime_value = runtime_cwd.resolve().as_posix()
    effective_value = effective_cwd.resolve().as_posix()
    proof_name = ".effective-cwd-redline-proof"
    resolver = _RecordingModeResolver(
        modes={
            runtime_value: ApprovalDispositionMode.FULL_TRUST,
            effective_value: ApprovalDispositionMode.USER,
        }
    )
    interactive = _RecordingApproval()
    event_sink = _RecordingEventSink()
    home = tmp_path / ".kongming"
    approval = build_safety_chain(
        Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it")),
        interactive_approval=interactive,
        permissions_manager=PermissionsManager(home),
        danger_guard=DangerGuard(kongming_home=home),
        disposition_resolver=resolver,
        event_sinks=[event_sink],
    )
    tool = ShellTool()

    result = await Runner().run(
        "execute effective cwd proof",
        session=InMemorySession("session-effective-cwd-redline"),
        agent_spec=AgentSpec(
            name="effective-cwd-redline",
            instructions="",
            default_model="fake",
            max_turns=3,
        ),
        llm=_ShellOverrideLLM(
            command=f"touch {proof_name}",
            effective_cwd=effective_cwd,
        ),
        tools={"run_shell": tool},
        enabled_tools=[tool],
        approval=approval,
        tool_context_metadata={"cwd": runtime_value},
        thread_id="thread-effective-cwd-redline",
    )
    await asyncio.sleep(0)

    assert result.status == "completed"
    assert not (runtime_cwd / proof_name).exists()
    assert not (effective_cwd / proof_name).exists()
    assert resolver.seen_cwds == [effective_value]
    assert len(interactive.requests) == 1
    assert interactive.requests[0].execution_scope.cwd == effective_value
    approval_required = next(
        event for event in event_sink.events if event.kind == "tool.approval_required"
    )
    assert approval_required.payload["execution_scope_cwd"] == effective_value
    assert all(event.kind != "approval.full_trust.auto_allow" for event in event_sink.events)
