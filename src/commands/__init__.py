"""Shared command module for CLI and Web hosts."""

from commands.models import (
    CommandDefinition,
    CommandExecutionContext,
    CommandResult,
)
from commands.service import CommandService, build_default_command_service

__all__ = [
    "CommandDefinition",
    "CommandExecutionContext",
    "CommandResult",
    "CommandService",
    "build_default_command_service",
]
