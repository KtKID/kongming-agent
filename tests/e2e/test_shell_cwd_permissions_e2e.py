"""Shell prepared cwd 与 thread permissions 跨目录隔离端到端测试。

测试使用 fake LLM、真实 Runner、SafetyGatedApproval、ApprovalManager、
PermissionsManager 与 ShellTool。目录 A 记住同一命令后，目录 B 必须再次进入
pending，且人工批准前不会创建 subprocess 产物。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from core import AgentSpec, InMemorySession, Result, Runner, ToolCall
from core.contracts import (
    ApprovalProvider,
    LLMRequest,
    LLMResponse,
)
from core.message import Message
from infrastructure.config.models import Config, ModelSelectionConfig
from safety.approval.chain import build_safety_chain
from safety.approval.events import PendingApprovalView
from safety.approval.manager import ApprovalManager, make_manager_prompt_fn
from safety.approval.permissions_manager import PermissionsManager
from safety.auto_approval.disposition import ApprovalDispositionMode
from tools import ShellTool
from tools.runtime.approval import InteractiveApproval


class _ShellCallLLM:
    """每次 run 先发 Shell tool call，再返回终态文本。"""

    def __init__(self, command: str) -> None:
        self._command = command
        self._turn = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """按 turn 返回固定 tool call 或完成消息。"""
        del request
        self._turn += 1
        if self._turn == 1:
            return LLMResponse(
                message=Message(
                    role="assistant",
                    content=None,
                    tool_calls=(
                        ToolCall(
                            call_id="shell-call",
                            tool_name="run_shell",
                            arguments={"command": self._command},
                        ),
                    ),
                ),
                finish_reason="tool_calls",
            )
        return LLMResponse(
            message=Message(role="assistant", content="done"),
            finish_reason="stop",
        )


class _PendingSink:
    """把 ApprovalManager pending 放入队列供测试精确控制批准时点。"""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[PendingApprovalView] = asyncio.Queue()

    async def emit_approval_required(self, *, pending: PendingApprovalView) -> None:
        """记录一条待审批请求。"""
        await self.queue.put(pending)

    async def emit_approval_removed(self, *, request_id: str, reason: str) -> None:
        """确认统一移除出口收到完整身份。"""
        assert request_id
        assert reason == "user_decided"


class _UserModeResolver:
    """固定使用用户审批模式，使 permissions 命中与 pending 可观测。"""

    def mode_for(self, cwd: str) -> ApprovalDispositionMode:
        """返回 USER 模式。"""
        assert cwd
        return ApprovalDispositionMode.USER


def _remember_decision(pending: PendingApprovalView) -> dict[str, object]:
    """从服务端冻结候选构造严格一致的 remember resolve 输入。"""
    candidate = pending.remember_rule
    assert candidate is not None
    return {
        "allow": True,
        "remember": True,
        "rememberRule": {
            "expression": candidate.expression,
            "displayText": candidate.display_text,
            "scopeCwd": candidate.scope_cwd,
        },
    }


async def _run_shell(
    *,
    cwd: Path,
    thread_id: str,
    approval: ApprovalProvider,
    command: str,
) -> tuple[asyncio.Task[Result], InMemorySession]:
    """启动一条真实 Runner → ShellTool 链，并返回未完成任务。"""
    tool = ShellTool()
    session = InMemorySession(f"session-{cwd.name}")
    task = asyncio.create_task(
        Runner().run(
            "execute proof command",
            session=session,
            agent_spec=AgentSpec(
                name="shell-e2e",
                instructions="",
                default_model="fake",
                max_turns=3,
            ),
            llm=_ShellCallLLM(command),
            tools={"run_shell": tool},
            enabled_tools=[tool],
            approval=approval,
            tool_context_metadata={"cwd": cwd.as_posix()},
            thread_id=thread_id,
        )
    )
    return task, session


async def test_shell_allow_is_exact_cwd_and_subprocess_waits_for_reapproval(
    tmp_path: Path,
) -> None:
    """A 目录记住后 B 目录重新审批，批准前 B 没有执行产物。"""
    cwd_a = tmp_path / "a"
    cwd_b = tmp_path / "b"
    cwd_a.mkdir()
    cwd_b.mkdir()
    command = "touch .shell-cwd-scope-proof"
    proof_name = ".shell-cwd-scope-proof"
    thread_id = "thread-shell-scope"
    permissions = PermissionsManager(tmp_path / ".kongming")
    manager = ApprovalManager(permissions_manager=permissions)
    sink = _PendingSink()
    manager.register_event_sink(sink)
    chain = build_safety_chain(
        Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it")),
        interactive_approval=InteractiveApproval(
            make_manager_prompt_fn(manager, thread_id),
        ),
        permissions_manager=permissions,
        disposition_resolver=_UserModeResolver(),
    )

    task_a, _session_a = await _run_shell(
        cwd=cwd_a,
        thread_id=thread_id,
        approval=chain,
        command=command,
    )
    pending_a = await asyncio.wait_for(sink.queue.get(), timeout=1)
    assert pending_a.remember_rule is not None
    assert pending_a.remember_rule.scope_cwd == cwd_a.resolve().as_posix()
    assert pending_a.tool_input["cwd"] == cwd_a.resolve().as_posix()
    assert not (cwd_a / proof_name).exists()
    assert await manager.resolve(
        thread_id,
        pending_a.request_id,
        _remember_decision(pending_a),
    )
    result_a = await asyncio.wait_for(task_a, timeout=3)
    assert result_a.status == "completed"
    assert (cwd_a / proof_name).is_file()

    snapshot = await permissions.snapshot(thread_id)
    assert len(snapshot.allow) == 1
    assert snapshot.allow[0].scope_cwd == cwd_a.resolve().as_posix()

    task_b, _session_b = await _run_shell(
        cwd=cwd_b,
        thread_id=thread_id,
        approval=chain,
        command=command,
    )
    pending_b = await asyncio.wait_for(sink.queue.get(), timeout=1)
    assert pending_b.remember_rule is not None
    assert pending_b.remember_rule.scope_cwd == cwd_b.resolve().as_posix()
    assert pending_b.tool_input["cwd"] == cwd_b.resolve().as_posix()
    assert not (cwd_b / proof_name).exists()
    assert await manager.resolve(
        thread_id,
        pending_b.request_id,
        {"allow": True, "remember": False},
    )
    result_b = await asyncio.wait_for(task_b, timeout=3)
    assert result_b.status == "completed"
    assert (cwd_b / proof_name).is_file()
