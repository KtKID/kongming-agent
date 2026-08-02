"""Builtin command definitions."""

from __future__ import annotations

from commands.models import CommandDefinition

BUILTIN_COMMANDS: tuple[CommandDefinition, ...] = (
    CommandDefinition(
        id="builtin.evolve",
        slash="/evolve",
        title="进化复盘",
        description="立即调用独立 child reviewer 复盘当前线程并生成候选养料",
        kind="action",
        host_visibility="web",
        accepts_args=False,
        executor_key="evolution_review",
    ),
    CommandDefinition(
        id="builtin.hello",
        slash="/hello",
        title="Hello",
        description="测试内建命令,回复'收到内建测试命令' ",
        kind="prompt",
        host_visibility="both",
        accepts_args=False,
        executor_key="prompt",
    ),
)
