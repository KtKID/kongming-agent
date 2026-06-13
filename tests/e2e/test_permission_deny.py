"""e2e P1-1：permission deny 分支。

覆盖 plan.md "e2e 主链路基线"的第四条：

> 某次工具调用命中 ``deny``，系统不执行工具，并把拒绝结果正确反馈回运行链。

用 :class:`safety.SafetyGatedApproval` 串起 capability / permission / 底层 approval，
把一条 ``outcome="deny"`` 的 :class:`safety.PermissionRule` 装进去，验证：

- ``SafetyGatedApproval.decide`` 直接返回 ``outcome="rejected"``（不下沉到底层 approval）
- runner 看到 rejected 后**不执行** tool，而是把拒绝消息以 ``role="tool"`` 消息回填 session
- 回填的 tool_result 不包含真实文件内容，只是拒绝文本
- 模型可以基于这条拒绝消息继续推理、走到终止文本
- ``approval.decision`` 事件的 payload 带 ``outcome="rejected"``，
  metadata 里 ``stage="permission"``
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import AgentSpec, InMemorySession, Runner, ToolCall
from safety import SafetyGatedApproval
from safety.approval.decision_engine import SafetyDecisionEngine
from safety.approval.types import (
    BoundaryScope,
    SensitivePathRule,
)
from safety.boundaries.resolver import BoundaryResolver
from safety.grants.store import GrantStore
from safety.guards.consent import ConsentResolver
from safety.guards.hard_block import HardBlockGuard
from safety.guards.trust import TrustResolver
from tests.e2e.conftest import MemoryEventSink, StubLLMProvider
from tools import AutoAllowApproval, ReadFileTool


class _TrackingApproval:
    """底层 approval spy：记录是否被 SafetyGatedApproval 下沉调用。

    hard_block 分支下不应该走到这里。如果 ``called`` 变成 True，
    说明安全链的"硬否决"短路逻辑坏了。
    """

    def __init__(self) -> None:
        self.called: bool = False

    async def decide(self, request):  # type: ignore[no-untyped-def]
        self.called = True
        # 即便被错误调用，也返回 approved 以便把 bug 放大成"tool 实际执行"的断言失败。
        from core.contracts import ApprovalDecision

        return ApprovalDecision(outcome="approved", reason="should-not-be-called")


def _build_deny_chain(
    *,
    underlying=None,
    deny_path_prefix: str = "/",
) -> SafetyGatedApproval:
    """构造一条 sensitive_paths effect=block 的安全链（v0.1.4）。

    v0.1.4 起 permission deny 由 sensitive_paths(effect=block) 表达，命中后
    HardBlockGuard 短路返回 rejected，不下沉到 InteractiveApproval。

    Args:
        underlying: 底层 ApprovalProvider spy；hard_block 分支不应被调用。
        deny_path_prefix: 拒绝的 path 前缀（默认 "/"，覆盖任意绝对路径）。
    """
    underlying = underlying or AutoAllowApproval()

    # 直接构造一条 ``effect=block`` 的 SensitivePathRule，与 HardBlockGuard 配合
    extra_block_rule = SensitivePathRule(
        name="test-policy-deny-readfile",
        matcher=deny_path_prefix,
        match_mode="path_prefix",
        ops=frozenset({"read", "write"}),
        effect="block",
        boundary_scope=BoundaryScope.ANY,
        reason="test-policy-deny",
    )
    hard_block = HardBlockGuard(
        hard_deny_commands=(),
        sensitive_paths=(extra_block_rule,),
    )
    boundary = BoundaryResolver.from_project_root()

    from infrastructure.config.models import (
        ApprovalConfig,
        Config,
        ModelConfig,
    )

    cfg = Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="m",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        approval=ApprovalConfig(mode="interactive"),
    )
    grants = GrantStore.from_config(cfg)
    engine = SafetyDecisionEngine(
        hard_block=hard_block,
        boundary=boundary,
        trust=TrustResolver(boundary, grants),
        consent=ConsentResolver.from_config(cfg, interactive_approval=underlying),
    )
    return SafetyGatedApproval(engine=engine, grant_store=grants)


@pytest.mark.e2e
async def test_permission_deny_blocks_tool_execution(
    stub_llm: StubLLMProvider,
    memory_sink: MemoryEventSink,
    tmp_path: Path,
) -> None:
    """permission deny 时 tool 不执行，拒绝消息回填，run 继续到完成。"""
    target = tmp_path / "secret.txt"
    sensitive_content = "SENSITIVE-PAYLOAD-MUST-NOT-LEAK"
    target.write_text(sensitive_content)

    # 第 1 轮：模型发 read_file tool_call（会被 permission deny 拦下）
    stub_llm.script(
        tool_calls=[
            ToolCall(
                call_id="c1",
                tool_name="read_file",
                arguments={"path": str(target)},
            )
        ],
    )
    # 第 2 轮：模型看到拒绝 tool_result 后给出终止文本
    stub_llm.script(content="I couldn't read that file.")

    underlying = _TrackingApproval()
    approval = _build_deny_chain(underlying=underlying)

    tool = ReadFileTool()
    registry: dict[str, object] = {"read_file": tool}

    runner = Runner(event_sinks=[memory_sink])
    session = InMemorySession("deny-test")
    spec = AgentSpec(
        name="t",
        instructions="",
        default_model="stub",
        tool_names=("read_file",),
        max_turns=5,
    )

    result = await runner.run(
        "读 secret.txt",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools=registry,
        approval=approval,
    )

    # 拒绝不中止 run
    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "I couldn't read that file."

    # 关键断言 1：底层 approval 不应该被调用（permission deny 直接短路）
    assert underlying.called is False, (
        "permission=deny should short-circuit before reaching underlying approval"
    )

    # 关键断言 2：session 中有恰好一条 tool-role 回填消息
    history = await session.history()
    tool_msgs = [m for m in history if m.role == "tool"]
    assert len(tool_msgs) == 1
    tool_content = tool_msgs[0].content or ""

    # 关键断言 3：tool_result 不含真实文件内容（tool 真没执行）
    assert sensitive_content not in tool_content

    # 关键断言 4：tool_result 明确是拒绝消息
    # runner._build_tool_error_message 把文本 "approval rejected: <reason>"
    # 包成 {"error": "..."} 的 JSON 串，所以内容里应该含 "rejected" 或 "approval"
    lower = tool_content.lower()
    assert "reject" in lower or "approval" in lower or "permission" in lower, (
        f"tool_result content does not look like a rejection: {tool_content!r}"
    )

    # 关键断言 5：tool_result 的 metadata 带 approval_outcome
    assert tool_msgs[0].metadata.get("approval_outcome") == "rejected"
    assert tool_msgs[0].tool_call_id == "c1"
    # ok=False 体现"工具没成功"
    assert tool_msgs[0].metadata.get("ok") is False


@pytest.mark.e2e
async def test_permission_deny_emits_approval_decision_event(
    stub_llm: StubLLMProvider,
    memory_sink: MemoryEventSink,
    tmp_path: Path,
) -> None:
    """approval.decision 事件应携带 outcome=rejected，且不产生 tool.call 成功态。"""
    target = tmp_path / "blocked.txt"
    target.write_text("ignored")

    stub_llm.script(
        tool_calls=[
            ToolCall(
                call_id="d1",
                tool_name="read_file",
                arguments={"path": str(target)},
            )
        ],
    )
    stub_llm.script(content="ok, skipping")

    approval = _build_deny_chain()

    runner = Runner(event_sinks=[memory_sink])
    session = InMemorySession("deny-evt")
    spec = AgentSpec(
        name="t",
        instructions="",
        default_model="stub",
        tool_names=("read_file",),
        max_turns=5,
    )

    result = await runner.run(
        "try to read",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={"read_file": ReadFileTool()},
        approval=approval,
    )
    assert result.status == "completed"

    # approval.decision 被 emit 了，outcome=rejected
    decisions = memory_sink.of_kind("approval.decision")
    assert len(decisions) == 1
    assert decisions[0].payload.get("outcome") == "rejected"
    assert decisions[0].payload.get("call_id") == "d1"
    assert decisions[0].payload.get("tool_name") == "read_file"
    # v0.1.4 起 deny 由 HardBlockGuard 表达；reason 含 "hard_block" 标识。
    reason_text = decisions[0].payload.get("reason") or ""
    assert "hard_block" in reason_text.lower() or "test-policy-deny" in reason_text.lower()

    # tool.call.end 的 ok 应该是 False，reason 显式是 approval_rejected
    end_events = memory_sink.of_kind("tool.call.end")
    assert len(end_events) == 1
    assert end_events[0].payload.get("ok") is False
    assert end_events[0].payload.get("reason") == "approval_rejected"


@pytest.mark.e2e
async def test_permission_deny_model_can_recover_with_next_turn(
    stub_llm: StubLLMProvider,
    memory_sink: MemoryEventSink,
    tmp_path: Path,
) -> None:
    """permission deny 后，模型继续推理并走向终止文本，整条链路事件完整。"""
    target = tmp_path / "x.txt"
    target.write_text("payload")

    stub_llm.script(
        tool_calls=[
            ToolCall(
                call_id="r1",
                tool_name="read_file",
                arguments={"path": str(target)},
            )
        ],
    )
    stub_llm.script(content="understood, stopping")

    approval = _build_deny_chain()

    runner = Runner(event_sinks=[memory_sink])
    session = InMemorySession("deny-recover")
    spec = AgentSpec(
        name="t",
        instructions="",
        default_model="stub",
        tool_names=("read_file",),
        max_turns=5,
    )

    result = await runner.run(
        "please read",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={"read_file": ReadFileTool()},
        approval=approval,
    )

    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "understood, stopping"
    # 至少 2 轮：tool_call 轮 + 终止轮
    assert result.turn_count >= 2

    # 事件流完整：两轮各有 turn.start/end，approval.request/decision 各一次
    kinds = memory_sink.kinds()
    assert kinds.count("turn.start") >= 2
    assert kinds.count("turn.end") >= 2
    assert kinds.count("approval.request") == 1
    assert kinds.count("approval.decision") == 1
    # stub_llm 被调用了 2 轮
    assert len(stub_llm.calls) == 2
