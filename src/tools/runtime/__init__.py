"""Tool runtime primitives."""

from tools.runtime.approval import (
    ApprovalAction,
    AutoAllowApproval,
    AutoDenyApproval,
    InteractiveApproval,
    PromptActionFn,
    PromptFn,
    build_default_approval,
    mark_action_aware,
)
from tools.runtime.base import BaseBuiltinTool
from tools.runtime.registry import ToolRegistry

__all__ = [
    "ApprovalAction",
    "AutoAllowApproval",
    "AutoDenyApproval",
    "BaseBuiltinTool",
    "InteractiveApproval",
    "PromptActionFn",
    "PromptFn",
    "ToolRegistry",
    "build_default_approval",
    "mark_action_aware",
]
