from __future__ import annotations

import json
from pathlib import Path

import pytest

from commands.models import CommandDefinition, CommandExecutionContext, CommandInvocation
from commands.review_action import ReviewActionError, ReviewContext, run_review_action


class _DummyRuntime:
    config = type(
        "Cfg",
        (),
        {
            "model_copy": lambda self, deep=True: self,
            "evolution": type(
                "Evo",
                (),
                {"learning": type("Learning", (), {"enabled": False})()},
            )(),
        },
    )()
    approval = object()
    tools = object()
    agent_spec = type("AgentSpec", (), {"reasoning_effort": None})()


def _command() -> CommandDefinition:
    return CommandDefinition(
        id="builtin.review",
        slash="/review",
        title="Run Review",
        description="d",
        kind="action",
        host_visibility="both",
        accepts_args=True,
        executor_key="review_action",
    )


def _invocation(tmp_path: Path) -> CommandInvocation:
    return CommandInvocation(
        invocation_id="cmd-123",
        session_id="sid-1",
        command=_command(),
        raw_input="/review auth",
        args_text="auth",
        cwd=tmp_path,
    )


def _context(tmp_path: Path) -> CommandExecutionContext:
    return CommandExecutionContext(
        session_id="sid-1",
        cwd=tmp_path,
        host_kind="cli",
        reasoning_effort=None,
    )


@pytest.mark.asyncio
async def test_review_action_returns_failure_for_nongit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_collect(cwd: Path, *, focus_text: str):
        raise ReviewActionError("not a git repository")

    monkeypatch.setattr("commands.review_action.collect_review_context", fake_collect)

    result = await run_review_action(
        runtime=_DummyRuntime(),  # type: ignore[arg-type]
        invocation=_invocation(tmp_path),
        context=_context(tmp_path),
    )

    assert result.status == "failed"
    assert "not a git repository" in result.output_text


@pytest.mark.asyncio
async def test_review_action_writes_artifact_for_clean_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def fake_collect(cwd: Path, *, focus_text: str):
        return ReviewContext(
            cwd=repo_root,
            repo_root=repo_root,
            status_text="",
            staged_diff="",
            unstaged_diff="",
            focus_text=focus_text,
        )

    monkeypatch.setattr("commands.review_action.collect_review_context", fake_collect)

    result = await run_review_action(
        runtime=_DummyRuntime(),  # type: ignore[arg-type]
        invocation=_invocation(repo_root),
        context=_context(repo_root),
    )

    artifact = repo_root / ".kongming" / "commands" / "runs" / "cmd-123.json"
    assert result.status == "completed"
    assert artifact.exists()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert "no pending git changes" in result.output_text.lower()


@pytest.mark.asyncio
async def test_review_action_uses_subagent_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def fake_collect(cwd: Path, *, focus_text: str):
        return ReviewContext(
            cwd=repo_root,
            repo_root=repo_root,
            status_text="M foo.py",
            staged_diff="",
            unstaged_diff="diff --git a/foo.py b/foo.py",
            focus_text=focus_text,
        )

    async def fake_review_subagent(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["review_context"].focus_text == "auth"
        return "Findings:\n- [medium] foo.py:1 - missing guard"

    monkeypatch.setattr("commands.review_action.collect_review_context", fake_collect)
    monkeypatch.setattr("commands.review_action.run_review_subagent", fake_review_subagent)

    result = await run_review_action(
        runtime=_DummyRuntime(),  # type: ignore[arg-type]
        invocation=_invocation(repo_root),
        context=_context(repo_root),
    )

    assert result.status == "completed"
    assert "missing guard" in result.output_text
