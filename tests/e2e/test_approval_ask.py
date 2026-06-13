"""e2e P0-3：approval ask 分支。

覆盖 plan.md "e2e 主链路基线"的第三条：

> 某次工具调用命中 ``ask``，CLI 完成人工确认，批准后继续执行。

用 :class:`tools.runtime.approval.InteractiveApproval` 注入一个异步 prompt 回调模拟
"CLI 用户输入 y/n"，验证：

- user 说 yes → tool 正常执行、最终 completed
- user 说 no → tool 未执行，runner 把 rejected 结果喂回模型继续
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import AgentSpec, InMemorySession, Runner, ToolCall
from core.contracts import ApprovalRequest
from tests.e2e.conftest import MemoryEventSink, StubLLMProvider
from tools import InteractiveApproval, ReadFileTool


@pytest.mark.e2e
async def test_approval_ask_user_confirms_then_tool_runs(
    stub_llm: StubLLMProvider,
    memory_sink: MemoryEventSink,
    tmp_path: Path,
) -> None:
    """ask 分支 + user 确认 → 工具执行 → 最终完成。"""
    target = tmp_path / "a.txt"
    target.write_text("data")

    received: list[ApprovalRequest] = []

    async def prompt_fn(req: ApprovalRequest) -> bool:
        received.append(req)
        return True  # user says yes

    approval = InteractiveApproval(prompt_fn)

    stub_llm.script(
        tool_calls=[
            ToolCall(
                call_id="a1",
                tool_name="read_file",
                arguments={"path": str(target)},
            )
        ],
    )
    stub_llm.script(content="done")

    runner = Runner(event_sinks=[memory_sink])
    session = InMemorySession("test-ask-yes")
    spec = AgentSpec(
        name="t",
        instructions="",
        default_model="stub",
        tool_names=("read_file",),
        max_turns=5,
    )

    result = await runner.run(
        "ok please read",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={"read_file": ReadFileTool()},
        approval=approval,
    )

    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "done"

    # prompt 被调用了一次
    assert len(received) == 1
    assert received[0].tool_name == "read_file"

    # 事件流里有 approval.decision 且 outcome 为 approved
    decisions = memory_sink.of_kind("approval.decision")
    assert len(decisions) == 1
    assert decisions[0].payload.get("outcome") == "approved"

    # tool 确实执行了：session 历史里有 tool 角色的回填消息
    history = await session.history()
    tool_msgs = [m for m in history if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "data" in (tool_msgs[0].content or "")


@pytest.mark.e2e
async def test_approval_ask_user_rejects_then_tool_is_skipped(
    stub_llm: StubLLMProvider,
    memory_sink: MemoryEventSink,
    tmp_path: Path,
) -> None:
    """ask 分支 + user 拒绝 → tool 不执行，rejected 结果喂回模型。

    按 runner 语义，approval 拒绝不终止 run，而是把"被拒绝"这条事实以
    ``role="tool"`` 消息追加到 session，然后进入下一个 turn。
    """
    target = tmp_path / "b.txt"
    target.write_text("secret")

    received: list[ApprovalRequest] = []

    async def prompt_fn(req: ApprovalRequest) -> bool:
        received.append(req)
        return False  # user says no

    approval = InteractiveApproval(prompt_fn)

    # 第 1 轮：模型请求 tool call（会被拒）
    stub_llm.script(
        tool_calls=[
            ToolCall(
                call_id="r1",
                tool_name="read_file",
                arguments={"path": str(target)},
            )
        ],
    )
    # 第 2 轮：模型看到 rejected tool result 后给终止文本
    stub_llm.script(content="ok, 我不读了")

    runner = Runner(event_sinks=[memory_sink])
    session = InMemorySession("test-ask-no")
    spec = AgentSpec(
        name="t",
        instructions="",
        default_model="stub",
        tool_names=("read_file",),
        max_turns=5,
    )

    result = await runner.run(
        "read the secret",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={"read_file": ReadFileTool()},
        approval=approval,
    )

    # 拒绝不中止 run
    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "ok, 我不读了"

    # prompt 调用了一次；后续没有再走审批
    assert len(received) == 1

    # 事件流里 approval.decision 应为 rejected
    decisions = memory_sink.of_kind("approval.decision")
    assert len(decisions) == 1
    assert decisions[0].payload.get("outcome") == "rejected"

    # session 里写入的是 rejected 结果（文本含 "approval rejected" / "rejected"），
    # 而不是真实文件内容。
    history = await session.history()
    tool_msgs = [m for m in history if m.role == "tool"]
    assert len(tool_msgs) == 1
    content = tool_msgs[0].content or ""
    assert "secret" not in content  # 文件真实内容绝不能出现
    assert "reject" in content.lower() or "approval" in content.lower()
