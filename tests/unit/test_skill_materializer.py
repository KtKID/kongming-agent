"""unit：self evolution skill materializer。"""

from __future__ import annotations

from pathlib import Path

import pytest

from evolution.models import EvolutionNutrient
from evolution.skill_materializer import materialize_skill
from prompting.skill_loader import load_skill_specs


def _nutrient(
    *,
    nutrient_id: str = "wf-2025-002",
    title: str = "wiki schema overlay",
    content: str = (
        "Add a wiki schema layer above the existing vault.\n"
        "Split the workflow into ingest, query, and lint.\n"
        "Keep the schema documentation close to the content tree."
    ),
    summary: str = "Use when the task needs the wiki schema overlay workflow.",
) -> EvolutionNutrient:
    return EvolutionNutrient(
        nutrient_id=nutrient_id,
        kind="workflow",
        title=title,
        content=content,
        summary=summary,
        confidence=0.93,
        evidence_turns=(3, 4),
        source_run_id="run-parent-1",
        source_session_id="thread-1",
        suggested_target="skill",
        tags=("workflow", "wiki"),
    )


@pytest.mark.unit
async def test_materialize_skill_creates_workspace_skill_loader_can_read(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"

    result = await materialize_skill(workspace, _nutrient())

    assert result.mode == "create"
    assert result.status == "written"
    assert result.written is True
    assert result.skill_name == "wiki-schema-overlay"

    skill_path = Path(result.path)
    assert skill_path == workspace / ".kongming" / "skills" / "wiki-schema-overlay" / "SKILL.md"
    assert skill_path.exists()
    assert not any(skill_path.parent.glob("*.tmp"))

    specs = await load_skill_specs(tmp_path / "home", workspace)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "wiki-schema-overlay"
    assert spec.description == "Use when the task needs the wiki schema overlay workflow."
    assert spec.when_to_use == "Use when the task needs the wiki schema overlay workflow."
    assert spec.paths == [".kongming/skills/wiki-schema-overlay"]

    body = skill_path.read_text(encoding="utf-8")
    assert "# wiki-schema-overlay" in body
    assert "## Managed evolution nutrients" in body
    assert "### Nutrient `wf-2025-002`" in body
    assert "1. Add a wiki schema layer above the existing vault" in body


@pytest.mark.unit
async def test_materialize_skill_updates_existing_skill_and_preserves_manual_body(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    skill_dir = workspace / ".kongming" / "skills" / "wiki-schema-overlay"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        "---\n"
        "name: wiki-schema-overlay\n"
        "description: Existing skill description\n"
        "when-to-use: Existing trigger text\n"
        "allowed-tools:\n"
        "  - read\n"
        "paths:\n"
        "  - docs/wiki\n"
        "---\n\n"
        "# wiki-schema-overlay\n\n"
        "Manual operator guidance stays here.\n",
        encoding="utf-8",
    )

    result = await materialize_skill(workspace, _nutrient())

    assert result.mode == "update"
    assert result.status == "written"
    updated = skill_path.read_text(encoding="utf-8")
    assert "Manual operator guidance stays here." in updated
    assert "### Nutrient `wf-2025-002`" in updated

    specs = await load_skill_specs(tmp_path / "home", workspace)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.description == "Existing skill description"
    assert spec.when_to_use == "Existing trigger text"
    assert spec.allowed_tools == ["read"]
    assert spec.paths == [".kongming/skills/wiki-schema-overlay", "docs/wiki"]


@pytest.mark.unit
async def test_materialize_skill_is_idempotent_for_same_nutrient(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    nutrient = _nutrient()

    first = await materialize_skill(workspace, nutrient)
    skill_path = Path(first.path)
    first_text = skill_path.read_text(encoding="utf-8")

    second = await materialize_skill(workspace, nutrient)
    second_text = skill_path.read_text(encoding="utf-8")

    assert first.status == "written"
    assert second.mode == "update"
    assert second.status == "skipped"
    assert second.written is False
    assert first_text == second_text
    assert second_text.count("### Nutrient `wf-2025-002`") == 1


@pytest.mark.unit
async def test_materialize_skill_repairs_missing_frontmatter_into_loader_readable_file(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    skill_dir = workspace / ".kongming" / "skills" / "wiki-schema-overlay"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("# legacy body\n\nKeep this note.\n", encoding="utf-8")

    result = await materialize_skill(workspace, _nutrient())

    assert result.mode == "update"
    assert result.status == "written"
    updated = skill_path.read_text(encoding="utf-8")
    assert updated.startswith("---\nname: wiki-schema-overlay\n")
    assert "Keep this note." in updated

    specs = await load_skill_specs(tmp_path / "home", workspace)
    assert len(specs) == 1
    assert specs[0].name == "wiki-schema-overlay"
