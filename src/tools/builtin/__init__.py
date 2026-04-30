"""Builtin tool 子包：把零散的 builtin tool 汇总到 ``tools.builtin`` 命名空间。

v0.1.6 起新增 first-class :class:`SkillTool`，它只在装配层有 ``skill_specs``
时才注册（见 ``tools/registry.py::build_default_registry`` 的 ``skill_specs``
入参，由 ``skill-assemble-v0.1.6`` 模块接通）。

历史 builtin tool（``ReadFileTool`` / ``WriteFileTool`` / ``ListDirTool`` /
``ShellTool`` / ``MemoryTool``）仍在 ``src/tools/`` 顶层；本子包不重新 export
它们以避免双重入口造成的歧义。
"""

from __future__ import annotations

from tools.builtin.skill_tool import (
    SKILL_TOOL_SCHEMA,
    SkillSecurityError,
    SkillTool,
    assert_no_command_substitution,
    substitute_vars,
)

__all__ = [
    "SKILL_TOOL_SCHEMA",
    "SkillSecurityError",
    "SkillTool",
    "assert_no_command_substitution",
    "substitute_vars",
]
