"""unit：evolution lifecycle 注册入口。"""

from __future__ import annotations

from typing import Any

import pytest

from core.message import Message
from core.result import Result
from core.run_state import RunState
from core.session import InMemorySession
from evolution.lifecycle import register_evolution_lifecycle_hook


class _Runtime:
    def __init__(self) -> None:
        self.hooks: list[Any] = []

    def add_lifecycle_hook(self, hook: Any) -> None:
        self.hooks.append(hook)


class _Manager:
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self.calls: list[tuple[Any, str, str]] = []

    async def notify_runtime_run(self, runtime: Any, session: Any, result: Result) -> None:
        self.calls.append((runtime, session.session_id, result.status))


@pytest.mark.unit
async def test_register_evolution_lifecycle_hook_enabled_registers_and_forwards() -> None:
    runtime = _Runtime()
    manager = _Manager(enabled=True)
    session = InMemorySession("thread-evo")
    result = Result(
        run_id="run-1",
        session_id="thread-evo",
        status="completed",
        final_message=Message.assistant("done"),
        turn_count=1,
    )

    registered = register_evolution_lifecycle_hook(runtime=runtime, manager=manager)  # type: ignore[arg-type]
    await runtime.hooks[0].after_run(
        RunState(run_id="run-1", session_id="thread-evo"),
        session,
        result,
    )

    assert registered is True
    assert len(runtime.hooks) == 1
    assert manager.calls == [(runtime, "thread-evo", "completed")]


@pytest.mark.unit
def test_register_evolution_lifecycle_hook_disabled_skips_registration() -> None:
    runtime = _Runtime()
    manager = _Manager(enabled=False)

    registered = register_evolution_lifecycle_hook(runtime=runtime, manager=manager)  # type: ignore[arg-type]

    assert registered is False
    assert runtime.hooks == []
