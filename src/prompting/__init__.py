"""Prompt assembly, instruction loading, compaction, and skill discovery."""

from __future__ import annotations

from prompting.assembly.input_assembler import AssembledInput, InputAssembler
from prompting.assembly.runtime_context import build_runtime_context_text
from prompting.compaction.history_compactor import CompactorConfig, HistoryCompactor
from prompting.context_sources.sitian_context import (
    MAX_ITEMS_PER_CHANNEL,
    build_sitian_context_text,
)
from prompting.instructions.instruction_loader import (
    InstructionLoader,
    InstructionSource,
    assemble_instructions,
)
from prompting.instructions.prompts_loader import TEMPLATE_FILENAMES, materialize_and_load_prompts
from prompting.skills.skill_loader import SkillSpec, format_skill_listing, load_skill_specs

__all__ = [
    "AssembledInput",
    "CompactorConfig",
    "HistoryCompactor",
    "InputAssembler",
    "InstructionLoader",
    "InstructionSource",
    "MAX_ITEMS_PER_CHANNEL",
    "SkillSpec",
    "TEMPLATE_FILENAMES",
    "assemble_instructions",
    "build_runtime_context_text",
    "build_sitian_context_text",
    "format_skill_listing",
    "load_skill_specs",
    "materialize_and_load_prompts",
]
