from __future__ import annotations

import pytest

from commands.models import CommandDefinition
from commands.registry import CommandRegistry, build_builtin_registry


def test_builtin_registry_contains_review():
    registry = build_builtin_registry()
    command = registry.lookup("review", "cli")
    assert command is not None
    assert command.slash == "/review"
    assert registry.lookup_slash("/review", "web") == command


def test_registry_rejects_duplicate_slash():
    review = CommandDefinition(
        id="a",
        slash="/review",
        title="A",
        description="A",
        kind="action",
        host_visibility="both",
        accepts_args=True,
        executor_key="x",
    )
    with pytest.raises(ValueError, match="duplicate command slash"):
        CommandRegistry([review, review])
