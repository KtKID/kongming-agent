"""Builtin /review action command."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from commands.models import (
    CommandDefinition,
    CommandExecutionContext,
    CommandInvocation,
    CommandResult,
    CommandResultStatus,
)
from core.agent_spec import AgentSpec
from core.result import Result
from core.session import InMemorySession
from executors.agent_runtime.native_runtime import NativeRuntime

_MAX_STATUS_CHARS = 4_000
_MAX_DIFF_CHARS = 24_000
_DEFAULT_REVIEW_TIMEOUT_SECONDS = 60.0


class ReviewActionError(RuntimeError):
    """Review command execution error."""


@dataclass(frozen=True)
class ReviewContext:
    cwd: Path
    repo_root: Path
    status_text: str
    staged_diff: str
    unstaged_diff: str
    focus_text: str

    @property
    def has_changes(self) -> bool:
        return bool(
            self.status_text.strip() or self.staged_diff.strip() or self.unstaged_diff.strip()
        )


def build_review_command_definition() -> CommandDefinition:
    return CommandDefinition(
        id="builtin.review",
        slash="/review",
        title="Run Review",
        description="Launch a child review agent for the current workspace changes.",
        kind="action",
        host_visibility="both",
        accepts_args=True,
        executor_key="review_action",
    )


async def run_review_action(
    *,
    runtime: NativeRuntime,
    invocation: CommandInvocation,
    context: CommandExecutionContext,
) -> CommandResult:
    started_at_ms = int(time.time() * 1000)
    try:
        review_context = collect_review_context(context.cwd, focus_text=invocation.args_text)
    except ReviewActionError as exc:
        return CommandResult(
            status="failed",
            command_name=invocation.command.slash,
            output_text=f"/review failed: {exc}",
            invocation_id=invocation.invocation_id,
        )

    if not review_context.has_changes:
        artifact_path = write_review_record(
            invocation=invocation,
            review_context=review_context,
            status="completed",
            review_text="No pending git changes to review.",
            started_at_ms=started_at_ms,
            ended_at_ms=int(time.time() * 1000),
        )
        return CommandResult(
            status="completed",
            command_name=invocation.command.slash,
            output_text=f"/review: no pending git changes to review.\nartifact: {artifact_path}",
            invocation_id=invocation.invocation_id,
            metadata={"artifact_path": str(artifact_path)},
        )

    try:
        review_text = await run_review_subagent(
            parent_runtime=runtime,
            invocation=invocation,
            review_context=review_context,
            reasoning_effort=context.reasoning_effort,
        )
        status: CommandResultStatus = "completed"
    except Exception as exc:
        review_text = f"/review failed while running reviewer: {type(exc).__name__}: {exc}"
        status = "failed"

    ended_at_ms = int(time.time() * 1000)
    artifact_path = write_review_record(
        invocation=invocation,
        review_context=review_context,
        status=status,
        review_text=review_text,
        started_at_ms=started_at_ms,
        ended_at_ms=ended_at_ms,
    )
    prefix = "/review completed" if status == "completed" else "/review failed"
    return CommandResult(
        status=status,
        command_name=invocation.command.slash,
        output_text=f"{prefix}.\n{review_text}\nartifact: {artifact_path}",
        invocation_id=invocation.invocation_id,
        metadata={"artifact_path": str(artifact_path)},
    )


def collect_review_context(cwd: Path, *, focus_text: str) -> ReviewContext:
    repo_root = Path(_run_git(cwd, "rev-parse", "--show-toplevel").strip()).resolve()
    status_text = _trim_text(_run_git(cwd, "status", "--short"), _MAX_STATUS_CHARS)
    staged_diff = _trim_text(_run_git(cwd, "diff", "--cached"), _MAX_DIFF_CHARS)
    unstaged_diff = _trim_text(_run_git(cwd, "diff", "--"), _MAX_DIFF_CHARS)
    return ReviewContext(
        cwd=cwd.resolve(),
        repo_root=repo_root,
        status_text=status_text,
        staged_diff=staged_diff,
        unstaged_diff=unstaged_diff,
        focus_text=focus_text.strip(),
    )


async def run_review_subagent(
    *,
    parent_runtime: NativeRuntime,
    invocation: CommandInvocation,
    review_context: ReviewContext,
    reasoning_effort: str | None,
) -> str:
    review_cfg = parent_runtime.config.model_copy(deep=True)
    review_cfg.evolution.learning.enabled = False
    child_session_id = f"command-review-{invocation.session_id}-{invocation.invocation_id}"
    review_session = InMemorySession(session_id=child_session_id)
    review_spec = AgentSpec(
        name="command-reviewer",
        instructions=_build_review_instructions(),
        default_model=review_cfg.model.name,
        tool_names=(),
        max_turns=2,
        metadata={"command_role": "reviewer"},
        reasoning_effort=reasoning_effort or parent_runtime.agent_spec.reasoning_effort,
    )

    def session_factory(session_id: str):  # type: ignore[no-untyped-def]
        if session_id == child_session_id:
            return review_session
        return InMemorySession(session_id=session_id)

    runtime = NativeRuntime.build(
        review_cfg,
        approval=parent_runtime.approval,
        tools=parent_runtime.tools,
        enabled_tool_names=[],
        session_factory=session_factory,
        agent_spec=review_spec,
    )
    try:
        result = await runtime.run(
            _build_review_prompt(review_context=review_context),
            session_id=child_session_id,
            reasoning_effort=reasoning_effort,
        )
    finally:
        await runtime.aclose()

    final_text = _result_text(result).strip()
    if final_text:
        return final_text
    raise ReviewActionError("reviewer returned empty output")


def write_review_record(
    *,
    invocation: CommandInvocation,
    review_context: ReviewContext,
    status: str,
    review_text: str,
    started_at_ms: int,
    ended_at_ms: int,
) -> Path:
    root_dir = review_context.repo_root if review_context.repo_root.exists() else invocation.cwd
    out_dir = root_dir / ".kongming" / "commands" / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = out_dir / f"{invocation.invocation_id}.json"
    payload = {
        "invocation_id": invocation.invocation_id,
        "session_id": invocation.session_id,
        "command_name": invocation.command.slash,
        "cwd": str(invocation.cwd),
        "repo_root": str(review_context.repo_root),
        "status": status,
        "started_at_ms": started_at_ms,
        "ended_at_ms": ended_at_ms,
        "args_text": invocation.args_text,
        "focus_text": review_context.focus_text,
        "git": {
            "status_text": review_context.status_text,
            "staged_diff": review_context.staged_diff,
            "unstaged_diff": review_context.unstaged_diff,
        },
        "review_text": review_text,
    }
    artifact_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    return artifact_path


def _build_review_instructions() -> str:
    return (
        "You are a code review subagent. Review the provided git changes. "
        "Return findings first. Use short, concrete bullets. "
        "Prefer bugs, regressions, safety issues, missing tests, and risky assumptions. "
        "If changes look safe, say 'No blocking findings.' and mention residual risks."
    )


def _build_review_prompt(*, review_context: ReviewContext) -> str:
    focus = review_context.focus_text or "none"
    return (
        f"Repository root: {review_context.repo_root}\n"
        f"Working directory: {review_context.cwd}\n"
        f"Review focus: {focus}\n\n"
        "Git status:\n"
        f"{review_context.status_text or '(clean status output)'}\n\n"
        "Staged diff:\n"
        f"{review_context.staged_diff or '(no staged diff)'}\n\n"
        "Unstaged diff:\n"
        f"{review_context.unstaged_diff or '(no unstaged diff)'}\n\n"
        "Write the review with this structure:\n"
        "Findings:\n"
        "- [severity] file:line - issue\n"
        "Open questions:\n"
        "- question or assumption\n"
        "Change summary:\n"
        "- one short paragraph\n"
    )


def _run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise ReviewActionError(message)
    return result.stdout.strip()


def _result_text(result: Result) -> str:
    if result.final_message is None:
        return ""
    return result.final_message.content or ""


def _trim_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    trimmed = text[:limit].rstrip()
    return f"{trimmed}\n\n[truncated]"
