"""EvolutionManager lifecycle 单测：init / aclose / enabled 同步 config。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from evolution.evolution_manager import EvolutionManager
from evolution.models import (
    DecisionRecord,
    EvolutionNutrient,
    ReviewResult,
    ReviewWritePayload,
)


def _cfg(tmp_path: Path, *, enabled: bool = True) -> object:
    from infrastructure.config import load_config

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


@pytest.mark.asyncio
async def test_apply_review_decision_queues_concurrent_apply_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = EvolutionManager(config=_cfg(tmp_path), kongming_home=tmp_path)
    await manager._evolution_store.write_review(
        ReviewWritePayload(
            review_result=ReviewResult(
                run_id="run-parent-1",
                session_id="thread-demo",
                reviewed_at_ms=123,
                review_summary="two actionable nutrients",
                nutrients=(
                    EvolutionNutrient(
                        nutrient_id="nutrient-1",
                        kind="workflow",
                        title="Workflow One",
                        content="content one",
                        summary="summary one",
                        confidence=0.9,
                        evidence_turns=(1,),
                        source_run_id="run-parent-1",
                        source_session_id="thread-demo",
                        suggested_target="skill",
                    ),
                    EvolutionNutrient(
                        nutrient_id="nutrient-2",
                        kind="memory",
                        title="Memory Two",
                        content="content two",
                        summary="summary two",
                        confidence=0.9,
                        evidence_turns=(2,),
                        source_run_id="run-parent-1",
                        source_session_id="thread-demo",
                        suggested_target="memory",
                    ),
                ),
            )
        )
    )

    active = 0
    max_active = 0

    async def fake_apply_decision_item(**kwargs: Any) -> DecisionRecord:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        record = await manager._evolution_store.read_decision(str(kwargs["review_id"]))
        assert record is not None
        return record

    monkeypatch.setattr(manager, "_apply_decision_item", fake_apply_decision_item)

    await asyncio.gather(
        manager.apply_review_decision(
            thread_id="thread-demo",
            review_id="evo-review:run-parent-1",
            nutrient_id="nutrient-1",
            decision="accept_skill",
            workspace_root=tmp_path,
        ),
        manager.apply_review_decision(
            thread_id="thread-demo",
            review_id="evo-review:run-parent-1",
            nutrient_id="nutrient-2",
            decision="accept_memory",
            workspace_root=tmp_path,
        ),
    )

    assert max_active == 1
    await manager.aclose()
