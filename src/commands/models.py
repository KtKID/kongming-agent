"""Core data models for shared command handling."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

CommandKind = Literal["prompt", "action", "interactive"]
HostVisibility = Literal["cli", "web", "both"]
ParsedInputKind = Literal["text", "command"]
CommandResultStatus = Literal["completed", "failed", "delegated_to_runtime", "waiting_input"]


@dataclass(frozen=True)
class CommandDefinition:
    id: str
    slash: str
    title: str
    description: str
    kind: CommandKind
    host_visibility: HostVisibility
    accepts_args: bool
    executor_key: str


@dataclass(frozen=True)
class ParsedInput:
    kind: ParsedInputKind
    raw_input: str
    slash: str | None = None
    command_name: str | None = None
    tail_text: str = ""
    args_text: str = ""


@dataclass(frozen=True)
class CommandInvocation:
    invocation_id: str
    session_id: str
    command: CommandDefinition
    raw_input: str
    args_text: str
    cwd: Path


@dataclass(frozen=True)
class CommandExecutionContext:
    session_id: str
    cwd: Path
    host_kind: Literal["cli", "web"]
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class CommandResult:
    status: CommandResultStatus
    command_name: str
    output_text: str
    invocation_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
