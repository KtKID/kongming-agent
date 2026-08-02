"""Evolution lifecycle hook adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.contracts import Session
from core.lifecycle import LifecycleHookBase
from core.result import Result
from core.run_state import RunState
from evolution.evolution_manager import EvolutionManager


@dataclass(frozen=True)
class _EvolutionLifecycleHook(LifecycleHookBase):
    """把 EvolutionManager 接入 Runner lifecycle。"""

    runtime: Any
    manager: EvolutionManager

    async def after_run(self, state: RunState, session: Session, result: Result) -> None:
        await self.manager.notify_runtime_run(self.runtime, session, result)


def register_evolution_lifecycle_hook(
    *,
    runtime: Any,
    manager: EvolutionManager,
) -> bool:
    """按 evolution 开关把 lifecycle hook 注册到 runtime。"""
    if not manager.enabled:
        return False
    runtime.add_lifecycle_hook(_EvolutionLifecycleHook(runtime, manager))
    return True


__all__ = ["register_evolution_lifecycle_hook"]
