"""unit: accepted nutrient -> MEMORY.md materializer."""

from __future__ import annotations

from pathlib import Path

import pytest

from evolution.memory_materializer import MemoryMaterializer, materialize_memory_entries
from evolution.models import EvolutionNutrient
from memory import ENTRY_DELIMITER, MemoryStore


def _nutrient(*, content: str, summary: str = "Three-step workflow") -> EvolutionNutrient:
    return EvolutionNutrient(
        nutrient_id="nutrient-memory-1",
        kind="memory",
        title="Workflow memory",
        content=content,
        summary=summary,
        confidence=0.92,
        evidence_turns=(1, 2),
        source_run_id="run-1",
        source_session_id="thread-1",
        suggested_target="memory",
        tags=("memory", "workflow"),
    )


@pytest.mark.unit
async def test_materialize_writes_up_to_three_atomic_entries(tmp_path: Path) -> None:
    store = MemoryStore(base_path=tmp_path)
    materializer = MemoryMaterializer(store, run_id="run-apply-1")

    outcome = await materializer.materialize(
        _nutrient(
            summary="Use a wiki schema layer",
            content=(
                "Use a wiki schema layer.\n"
                "Split the workflow into ingest, query, and lint.\n"
                "Store indexes under 05_索引.\n"
                "Keep a short operator checklist."
            ),
        )
    )

    assert outcome.status == "written"
    assert outcome.mode == "append"
    assert len(outcome.entries) == 3
    memory_path = tmp_path / ".kongming" / "memory" / "MEMORY.md"
    content = memory_path.read_text(encoding="utf-8")
    assert "Use a wiki schema layer" in content
    assert "Split the workflow into ingest, query, and lint" in content
    assert "Store indexes under 05_索引" in content
    assert "Keep a short operator checklist" not in content
    assert ENTRY_DELIMITER.strip() in content


@pytest.mark.unit
async def test_materialize_skips_when_normalized_content_already_exists(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".kongming" / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text(
        "use a wiki schema layer\n§\nsplit the workflow into ingest, query, and lint",
        encoding="utf-8",
    )
    store = MemoryStore(base_path=tmp_path)
    materializer = MemoryMaterializer(store, run_id="run-apply-2")

    outcome = await materializer.materialize(
        _nutrient(
            summary="Use a wiki schema layer。",
            content="Split the workflow into ingest, query, and lint。",
        )
    )

    assert outcome.status == "skipped"
    assert outcome.written_entries == ()
    assert len(outcome.skipped_entries) == 2
    content = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert content.count("wiki schema") == 1


@pytest.mark.unit
async def test_materialize_filters_partial_duplicates_and_writes_only_new_entries(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / ".kongming" / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text("Use a wiki schema layer", encoding="utf-8")
    store = MemoryStore(base_path=tmp_path)
    materializer = MemoryMaterializer(store, run_id="run-apply-3")

    outcome = await materializer.materialize(
        _nutrient(
            summary="Use a wiki schema layer",
            content="Split the workflow into ingest, query, and lint. Store indexes under 05_索引.",
        )
    )

    assert outcome.status == "written"
    assert outcome.skipped_entries == ("Use a wiki schema layer",)
    assert outcome.written_entries == (
        "Split the workflow into ingest, query, and lint",
        "Store indexes under 05_索引",
    )
    content = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert content.count("Use a wiki schema layer") == 1
    assert "Store indexes under 05_索引" in content


def test_materialize_memory_entries_builds_stable_normalized_content() -> None:
    materialized = materialize_memory_entries(
        _nutrient(
            summary="Use a wiki schema layer。",
            content="Use a wiki schema layer.\nSplit the workflow into ingest, query, and lint。",
        )
    )

    assert materialized.entries == (
        "Use a wiki schema layer",
        "Split the workflow into ingest, query, and lint",
    )
    assert materialized.normalized_content == (
        "use a wiki schema layer | split the workflow into ingest, query, and lint"
    )
