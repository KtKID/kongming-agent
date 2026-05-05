"""Builtin command registry."""

from __future__ import annotations

from collections.abc import Iterable

from commands.models import CommandDefinition, HostVisibility


class CommandRegistry:
    def __init__(self, commands: Iterable[CommandDefinition]) -> None:
        ordered = list(commands)
        seen_slashes: set[str] = set()
        for command in ordered:
            key = command.slash.lower()
            if key in seen_slashes:
                raise ValueError(f"duplicate command slash: {command.slash}")
            seen_slashes.add(key)
        self._commands = tuple(ordered)

    def list_commands(self, host_kind: HostVisibility | str) -> tuple[CommandDefinition, ...]:
        return tuple(
            command
            for command in self._commands
            if _visible_to_host(command.host_visibility, host_kind)
        )

    def lookup(
        self, command_name: str, host_kind: HostVisibility | str
    ) -> CommandDefinition | None:
        wanted = command_name.strip().lstrip("/").lower()
        for command in self.list_commands(host_kind):
            if command.slash.lstrip("/").lower() == wanted:
                return command
        return None

    def lookup_slash(self, slash: str, host_kind: HostVisibility | str) -> CommandDefinition | None:
        return self.lookup(slash, host_kind)


def build_builtin_registry() -> CommandRegistry:
    from commands.builtins import BUILTIN_COMMANDS

    return CommandRegistry(BUILTIN_COMMANDS)


def _visible_to_host(visibility: HostVisibility, host_kind: HostVisibility | str) -> bool:
    if visibility == "both":
        return True
    return visibility == host_kind
