"""Unit tests for scheduler.safety_wrapper.ScheduleApprovalProvider."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, field
from pathlib import Path
from typing import Any

import pytest

from core.contracts import ApprovalDecision, ApprovalRequest
from infrastructure.config.models import SchedulerApprovalConfig
from scheduler.safety_wrapper import ScheduleApprovalProvider


@dataclass
class FakeInner:
    decision: ApprovalDecision | None = None
    raise_exc: BaseException | None = None
    captured: list[ApprovalRequest] = field(default_factory=list)

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        self.captured.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.decision is not None
        return self.decision


def _make_request(
    tool_name: str = "shell",
    arguments: dict[str, Any] | None = None,
) -> ApprovalRequest:
    return ApprovalRequest(
        run_id="run-1",
        session_id="sess-1",
        turn=1,
        call_id="call-1",
        tool_name=tool_name,
        arguments=arguments or {"command": "rm -rf /tmp/x"},
        reason="test",
        metadata={},
    )


def _consent_decision(outcome: str = "approved") -> ApprovalDecision:
    return ApprovalDecision(
        outcome=outcome,
        reason="user said yes",
        metadata={
            "decision_class": "explicit_consent",
            "decision_source": "standard",
            "matched_rule": "approval:write",
        },
    )


@pytest.mark.asyncio
async def test_fail_closed_when_consent_required_for_non_whitelisted_tool() -> None:
    inner = FakeInner(decision=_consent_decision())
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-42")

    decision = await wrapped.decide(_make_request(tool_name="shell"))

    assert decision.outcome == "rejected"
    assert decision.reason == "cron fail-closed: requires consent (shell)"
    assert decision.metadata["cron_fail_closed"] is True
    assert decision.metadata["original_decision_class"] == "explicit_consent"
    assert decision.metadata["original_outcome"] == "approved"
    assert decision.metadata["cron_task_id"] == "cron-task-42"
    assert decision.metadata["decision_class"] == "explicit_consent"
    assert decision.metadata["matched_rule"] == "approval:write"


@pytest.mark.asyncio
async def test_auto_allows_new_file_create_inside_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    inner = FakeInner(decision=_consent_decision())
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-create")

    decision = await wrapped.decide(
        _make_request(
            tool_name="write_file",
            arguments={"path": "scheduled/output.txt", "content": "hello"},
        )
    )

    assert decision.outcome == "approved"
    assert decision.reason == "cron auto-allow: create file (write_file)"
    assert decision.metadata["decision_class"] == "silent_allow"
    assert decision.metadata["decision_source"] == "cron_whitelist"
    assert decision.metadata["cron_auto_allow"] == "write_file_create"
    assert decision.metadata["original_decision_class"] == "explicit_consent"
    assert decision.metadata["original_outcome"] == "approved"
    assert decision.metadata["cron_task_id"] == "cron-task-create"


@pytest.mark.asyncio
async def test_rejects_write_file_append_even_inside_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    inner = FakeInner(decision=_consent_decision())
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-append")

    decision = await wrapped.decide(
        _make_request(
            tool_name="write_file",
            arguments={"path": "append.txt", "content": "hello", "append": True},
        )
    )

    assert decision.outcome == "rejected"
    assert decision.metadata["cron_fail_closed"] is True
    assert decision.reason == "cron fail-closed: requires consent (write_file)"


@pytest.mark.asyncio
async def test_rejects_new_file_create_when_whitelist_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    inner = FakeInner(decision=_consent_decision())
    wrapped = ScheduleApprovalProvider(
        inner=inner,
        task_id="cron-task-disabled",
        policy=SchedulerApprovalConfig(allow_write_file_create_in_cwd=False),
    )

    decision = await wrapped.decide(
        _make_request(
            tool_name="write_file",
            arguments={"path": "scheduled/output.txt", "content": "hello"},
        )
    )

    assert decision.outcome == "rejected"
    assert decision.metadata["cron_fail_closed"] is True
    assert decision.reason == "cron fail-closed: requires consent (write_file)"


@pytest.mark.asyncio
async def test_rejects_write_file_over_existing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / "existing.txt"
    existing.write_text("old", encoding="utf-8")

    inner = FakeInner(decision=_consent_decision())
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-existing")

    decision = await wrapped.decide(
        _make_request(
            tool_name="write_file",
            arguments={"path": str(existing), "content": "new"},
        )
    )

    assert decision.outcome == "rejected"
    assert decision.metadata["cron_fail_closed"] is True
    assert decision.reason == "cron fail-closed: requires consent (write_file)"


@pytest.mark.asyncio
async def test_rejects_write_file_outside_cwd(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    outside_root = tmp_path_factory.mktemp("outside-cron")
    outside_target = outside_root / "escape.txt"

    inner = FakeInner(decision=_consent_decision())
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-outside")

    decision = await wrapped.decide(
        _make_request(
            tool_name="write_file",
            arguments={"path": str(outside_target), "content": "hello"},
        )
    )

    assert decision.outcome == "rejected"
    assert decision.metadata["cron_fail_closed"] is True
    assert decision.reason == "cron fail-closed: requires consent (write_file)"


@pytest.mark.asyncio
async def test_silent_allow_passthrough() -> None:
    inner_decision = ApprovalDecision(
        outcome="approved",
        reason="trusted path",
        metadata={
            "decision_class": "silent_allow",
            "decision_source": "intrinsic",
        },
    )
    inner = FakeInner(decision=inner_decision)
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-1")

    decision = await wrapped.decide(_make_request())

    assert decision is inner_decision
    assert decision.metadata == {
        "decision_class": "silent_allow",
        "decision_source": "intrinsic",
    }


@pytest.mark.asyncio
async def test_hard_block_passthrough() -> None:
    inner_decision = ApprovalDecision(
        outcome="rejected",
        reason="blocked by policy",
        metadata={
            "decision_class": "hard_block",
            "matched_rule": "deny:rm -rf /",
        },
    )
    inner = FakeInner(decision=inner_decision)
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-7")

    decision = await wrapped.decide(_make_request())

    assert decision is inner_decision
    assert decision.metadata == {
        "decision_class": "hard_block",
        "matched_rule": "deny:rm -rf /",
    }


@pytest.mark.asyncio
async def test_missing_decision_class_passthrough() -> None:
    inner_decision = ApprovalDecision(
        outcome="approved",
        reason="legacy approval path",
        metadata={},
    )
    inner = FakeInner(decision=inner_decision)
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-x")

    decision = await wrapped.decide(_make_request())

    assert decision is inner_decision
    assert decision.metadata == {}


@pytest.mark.asyncio
async def test_metadata_without_decision_class_key_passthrough() -> None:
    inner_decision = ApprovalDecision(
        outcome="approved",
        reason="legacy",
        metadata={"some_other_field": "x", "matched_rule": "foo"},
    )
    inner = FakeInner(decision=inner_decision)
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-y")

    decision = await wrapped.decide(_make_request())

    assert decision is inner_decision
    assert "cron_fail_closed" not in decision.metadata


@pytest.mark.asyncio
async def test_inner_exception_propagates() -> None:
    sentinel = RuntimeError("inner blew up")
    inner = FakeInner(raise_exc=sentinel)
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-err")

    with pytest.raises(RuntimeError) as excinfo:
        await wrapped.decide(_make_request())

    assert excinfo.value is sentinel
    assert len(inner.captured) == 1


@pytest.mark.asyncio
async def test_cancelled_outcome_is_preserved_in_fail_closed_metadata() -> None:
    inner = FakeInner(decision=_consent_decision(outcome="cancelled"))
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-cancel")

    decision = await wrapped.decide(_make_request(tool_name="shell"))

    assert decision.outcome == "rejected"
    assert decision.metadata["cron_fail_closed"] is True
    assert decision.metadata["original_outcome"] == "cancelled"
    assert decision.metadata["original_decision_class"] == "explicit_consent"


def test_frozen_dataclass_rejects_field_assignment() -> None:
    inner = FakeInner(decision=ApprovalDecision(outcome="approved", metadata={}))
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-z")

    with pytest.raises(FrozenInstanceError):
        wrapped.task_id = "new-id"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_request_is_passed_through_untouched() -> None:
    inner = FakeInner(
        decision=ApprovalDecision(
            outcome="approved",
            metadata={"decision_class": "silent_allow"},
        )
    )
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-passthrough")
    request = _make_request(tool_name="read_file")

    await wrapped.decide(request)

    assert len(inner.captured) == 1
    assert inner.captured[0] is request


@pytest.mark.asyncio
async def test_fail_closed_does_not_mutate_inner_metadata() -> None:
    inner_meta: dict[str, Any] = {"decision_class": "explicit_consent"}
    inner_decision = ApprovalDecision(outcome="approved", metadata=inner_meta)
    inner = FakeInner(decision=inner_decision)
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-iso")

    decision = await wrapped.decide(_make_request())

    assert inner_meta == {"decision_class": "explicit_consent"}
    assert decision.metadata is not inner_meta
    assert decision.metadata["cron_fail_closed"] is True
