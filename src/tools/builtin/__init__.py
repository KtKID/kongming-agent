"""Builtin tool implementations."""

from __future__ import annotations

from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "EvolutionWriteTool": ("tools.builtin.evolution_write_tool", "EvolutionWriteTool"),
    "ListDirTool": ("tools.builtin.file_tool", "ListDirTool"),
    "MemoryTool": ("tools.builtin.memory_tool", "MemoryTool"),
    "ReadFileTool": ("tools.builtin.file_tool", "ReadFileTool"),
    "ScheduleTool": ("tools.builtin.schedule_tool", "ScheduleTool"),
    "SKILL_TOOL_SCHEMA": ("tools.builtin.skill_tool", "SKILL_TOOL_SCHEMA"),
    "ShellTool": ("tools.builtin.shell_tool", "ShellTool"),
    "SkillSecurityError": ("tools.builtin.skill_tool", "SkillSecurityError"),
    "SkillTool": ("tools.builtin.skill_tool", "SkillTool"),
    "WebFetchTool": ("tools.builtin.web_fetch_tool", "WebFetchTool"),
    "WriteFileTool": ("tools.builtin.file_tool", "WriteFileTool"),
    "assert_no_command_substitution": (
        "tools.builtin.skill_tool",
        "assert_no_command_substitution",
    ),
    "build_evolution_write_tool": (
        "tools.builtin.evolution_write_tool",
        "build_evolution_write_tool",
    ),
    "build_file_tools": ("tools.builtin.file_tool", "build_file_tools"),
    "build_memory_tool": ("tools.builtin.memory_tool", "build_memory_tool"),
    "build_schedule_tool": ("tools.builtin.schedule_tool", "build_schedule_tool"),
    "build_shell_tool": ("tools.builtin.shell_tool", "build_shell_tool"),
    "build_web_fetch_tool": ("tools.builtin.web_fetch_tool", "build_web_fetch_tool"),
    "substitute_vars": ("tools.builtin.skill_tool", "substitute_vars"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Lazy re-export builtin tools without importing every implementation."""
    if name not in _EXPORTS:
        raise AttributeError(name)

    import importlib

    module_name, attr_name = _EXPORTS[name]
    module = importlib.import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
