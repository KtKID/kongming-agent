"""EvolutionManager lifecycle 单测：init / aclose / enabled 同步 config。"""

from __future__ import annotations

from pathlib import Path

import pytest

from evolution.evolution_manager import EvolutionManager


def _cfg(tmp_path: Path, *, enabled: bool = True) -> object:
    from config_loader import load_config

    cfg = load_config(None)
    return cfg.model_copy(
        update={
            "evolution": cfg.evolution.model_copy(
                update={
                    "learning": cfg.evolution.learning.model_copy(
                        update={"enabled": enabled, "root_path": str(tmp_path / "evo")}
                    )
                }
            )
        }
    )


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_enabled_true(self, tmp_path: Path) -> None:
        manager = EvolutionManager(config=_cfg(tmp_path, enabled=True), kongming_home=tmp_path)
        assert manager.enabled is True
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_enabled_false(self, tmp_path: Path) -> None:
        manager = EvolutionManager(config=_cfg(tmp_path, enabled=False), kongming_home=tmp_path)
        assert manager.enabled is False
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_aclose_idempotent(self, tmp_path: Path) -> None:
        manager = EvolutionManager(config=_cfg(tmp_path), kongming_home=tmp_path)
        await manager.aclose()
        await manager.aclose()  # second call should not crash

    @pytest.mark.asyncio
    async def test_mini_registry_has_evolution_write(self, tmp_path: Path) -> None:
        manager = EvolutionManager(config=_cfg(tmp_path, enabled=True), kongming_home=tmp_path)
        names = list(manager._mini_registry.names())
        assert "evolution_write" in names
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_disabled_no_tool_registered(self, tmp_path: Path) -> None:
        manager = EvolutionManager(config=_cfg(tmp_path, enabled=False), kongming_home=tmp_path)
        names = list(manager._mini_registry.names())
        assert "evolution_write" not in names
        await manager.aclose()
