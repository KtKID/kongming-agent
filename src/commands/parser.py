"""Parse raw host input into plain text or command invocations."""

from __future__ import annotations

import re

from commands.models import ParsedInput

_COMMAND_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def parse_input(raw_input: str) -> ParsedInput:
    stripped = raw_input.strip()
    if not stripped:
        return ParsedInput(kind="text", raw_input=raw_input)
    if not stripped.startswith("/"):
        return ParsedInput(kind="text", raw_input=raw_input)

    command_token, _, tail = stripped.partition(" ")
    command_name = command_token[1:]
    if not command_name:
        return ParsedInput(kind="text", raw_input=raw_input)
    if "/" in command_name:
        return ParsedInput(kind="text", raw_input=raw_input)
    if not _COMMAND_NAME_RE.fullmatch(command_name):
        return ParsedInput(kind="text", raw_input=raw_input)

    return ParsedInput(
        kind="command",
        raw_input=raw_input,
        slash=command_token,
        command_name=command_name,
        tail_text=tail,
        args_text=tail.strip(),
    )
