"""unit：tools.runtime.approval 三种 ApprovalProvider 实现 + 工厂。"""

from __future__ import annotations

import pytest

from core.contracts import ApprovalRequest
from tools import (
    AutoAllowApproval,
    AutoDenyApproval,
    InteractiveApproval,
    build_default_approval,
)


def _req(tool_name: str = "t") -> ApprovalRequest:
    return ApprovalRequest(
        run_id="r",
        session_id="s",
        turn=1,
        call_id="c",
        tool_name=tool_name,
    )


@pytest.mark.unit
async def test_auto_allow_approval_returns_approved() -> None:
    provider = AutoAllowApproval()
    decision = await provider.decide(_req())
    assert decision.outcome == "approved"
    assert decision.approved is True


@pytest.mark.unit
async def test_auto_deny_approval_returns_rejected() -> None:
    provider = AutoDenyApproval()
    decision = await provider.decide(_req())
    assert decision.outcome == "rejected"
    assert decision.approved is False


@pytest.mark.unit
async def test_interactive_approval_calls_prompt_and_returns_approved_on_true() -> None:
    calls: list[ApprovalRequest] = []

    async def prompt_fn(req: ApprovalRequest) -> bool:
        calls.append(req)
        return True

    provider = InteractiveApproval(prompt_fn)
    decision = await provider.decide(_req("read_file"))
    assert decision.outcome == "approved"
    assert decision.approved is True
    assert len(calls) == 1
    assert calls[0].tool_name == "read_file"


@pytest.mark.unit
async def test_interactive_approval_returns_rejected_on_false() -> None:
    async def prompt_fn(req: ApprovalRequest) -> bool:
        return False

    provider = InteractiveApproval(prompt_fn)
    decision = await provider.decide(_req())
    assert decision.outcome == "rejected"
    assert decision.approved is False


@pytest.mark.unit
def test_interactive_approval_requires_prompt_fn() -> None:
    with pytest.raises(ValueError):
        InteractiveApproval(None)  # type: ignore[arg-type]


@pytest.mark.unit
async def test_build_default_approval_dispatches_by_mode() -> None:
    async def prompt_fn(req: ApprovalRequest) -> bool:
        return True

    interactive = build_default_approval("interactive", prompt_fn=prompt_fn)
    assert isinstance(interactive, InteractiveApproval)

    allow = build_default_approval("auto_allow")
    assert isinstance(allow, AutoAllowApproval)

    deny = build_default_approval("auto_deny")
    assert isinstance(deny, AutoDenyApproval)


@pytest.mark.unit
def test_build_default_approval_interactive_requires_prompt_fn() -> None:
    with pytest.raises(ValueError):
        build_default_approval("interactive")


@pytest.mark.unit
def test_build_default_approval_unknown_mode() -> None:
    with pytest.raises(ValueError):
        build_default_approval("weird_mode")


@pytest.mark.unit
async def test_approval_decision_approved_property() -> None:
    """ApprovalDecision.approved 只对 outcome=='approved' 为 True。"""
    allow_provider = AutoAllowApproval()
    d = await allow_provider.decide(_req())
    assert d.approved is True

    deny_provider = AutoDenyApproval()
    d2 = await deny_provider.decide(_req())
    assert d2.approved is False
