"""unit：evolution_write tool。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.contracts import ToolContext
from evolution.state_store import EvolutionStateStore
from evolution.store import EvolutionStore
from tools.builtin.evolution_write_tool import build_evolution_write_tool


def _ctx() -> ToolContext:
    return ToolContext(run_id="review-run", session_id="review-session", turn=1, call_id="call-1")


def _payload() -> dict[str, object]:
    return {
        "review_result": {
            "run_id": "run-parent-1",
            "session_id": "cli-demo",
            "reviewed_at_ms": 123,
            "review_summary": "picked one nutrient",
            "nutrients": [
                {
                    "nutrient_id": "nutrient-1",
                    "kind": "workflow",
                    "title": "review loop",
                    "content": "After each task, extract high-value workflow knowledge.",
                    "summary": "post-run workflow review",
                    "confidence": 0.91,
                    "evidence_turns": [3, 4],
                    "source_run_id": "run-parent-1",
                    "source_session_id": "cli-demo",
                    "suggested_target": "skill",
                    "tags": ["workflow"],
                },
                {
                    "nutrient_id": "nutrient-low",
                    "kind": "memory",
                    "title": "low confidence",
                    "content": "discard me",
                    "summary": "discard",
                    "confidence": 0.2,
                    "evidence_turns": [4],
                    "source_run_id": "run-parent-1",
                    "source_session_id": "cli-demo",
                    "suggested_target": "memory",
                    "tags": ["low"],
                },
            ],
            "skip_reasons": [],
        },
        "transcript_window": {
            "session_id": "cli-demo",
            "run_id": "run-parent-1",
            "user_turn_count": 4,
            "included_turns": [3, 4],
            "messages": [
                {"turn": 3, "role": "user", "content": "extract this"},
                {"turn": 4, "role": "assistant", "content": "done"},
            ],
            "final_message": "done",
            "tool_call_count": 1,
            "summary": "2 messages",
        },
        "trigger_reason": "cadence",
    }


@pytest.mark.unit
async def test_evolution_write_tool_writes_review_queue_and_state(tmp_path: Path) -> None:
    root_dir = tmp_path / ".kongming" / "evolution"
    store = EvolutionStore(
        root_dir=root_dir,
        state_store=EvolutionStateStore(root_dir),
    )
    tool = build_evolution_write_tool(store, min_confidence=0.75, max_nutrients=2)

    result = await tool.execute(_payload(), _ctx())
    assert result.ok
    assert result.data["status"] == "written"
    assert result.data["nutrients_written"] == 1

    review_path = root_dir / "reviews" / "run-parent-1.json"
    queue_path = root_dir / "evolution-nutrients.jsonl"
    state_path = root_dir / "evolution.state.json"
    assert review_path.exists()
    assert queue_path.exists()
    assert state_path.exists()

    queue_lines = queue_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(queue_lines) == 1
    queue_item = json.loads(queue_lines[0])
    assert queue_item["nutrient_id"] == "nutrient-1"

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["sessions"]["cli-demo"]["last_reviewed_run_id"] == "run-parent-1"
    assert state["sessions"]["cli-demo"]["last_nutrient_id"] == "nutrient-1"


@pytest.mark.unit
async def test_evolution_write_tool_is_idempotent_by_run_id(tmp_path: Path) -> None:
    root_dir = tmp_path / ".kongming" / "evolution"
    store = EvolutionStore(
        root_dir=root_dir,
        state_store=EvolutionStateStore(root_dir),
    )
    tool = build_evolution_write_tool(store, min_confidence=0.75, max_nutrients=2)

    first = await tool.execute(_payload(), _ctx())
    second = await tool.execute(_payload(), _ctx())

    assert first.ok and second.ok
    assert second.data["status"] == "already_exists"
    queue_path = root_dir / "evolution-nutrients.jsonl"
    assert len(queue_path.read_text(encoding="utf-8").strip().splitlines()) == 1


@pytest.mark.unit
async def test_evolution_write_tool_accepts_flattened_payload_with_transcript_fallback(
    tmp_path: Path,
) -> None:
    root_dir = tmp_path / ".kongming" / "evolution"
    store = EvolutionStore(
        root_dir=root_dir,
        state_store=EvolutionStateStore(root_dir),
    )
    tool = build_evolution_write_tool(store, min_confidence=0.75, max_nutrients=2)

    result = await tool.execute(
        {
            "run_id": "run-flat-1",
            "session_id": "flat-session",
            "review_summary": "",
            "nutrients": [
                {
                    "kind": "workflow",
                    "title": "flat nutrient",
                    "content": "keep this",
                    "summary": "flat",
                    "confidence": 0.95,
                    "evidence_turns": [1],
                }
            ],
            "transcript_window": {
                "session_id": "flat-session",
                "run_id": "run-flat-1",
                "user_turn_count": 1,
                "included_turns": [1],
                "messages": [{"turn": 1, "role": "user", "content": "hello"}],
                "final_message": "ok",
                "tool_call_count": 0,
                "summary": "1 message",
            },
        },
        _ctx(),
    )

    assert result.ok
    assert result.data["status"] == "written"
    queue_item = json.loads((root_dir / "evolution-nutrients.jsonl").read_text(encoding="utf-8"))
    assert queue_item["source_run_id"] == "run-flat-1"
    assert queue_item["source_session_id"] == "flat-session"
    assert queue_item["nutrient_id"].startswith("run-flat-1-")


@pytest.mark.unit
async def test_evolution_write_tool_ignores_malformed_transcript_window(tmp_path: Path) -> None:
    root_dir = tmp_path / ".kongming" / "evolution"
    store = EvolutionStore(
        root_dir=root_dir,
        state_store=EvolutionStateStore(root_dir),
    )
    tool = build_evolution_write_tool(store, min_confidence=0.75, max_nutrients=2)

    result = await tool.execute(
        {
            "review_result": {
                "run_id": "run-bad-window-1",
                "session_id": "bad-window-session",
                "reviewed_at_ms": 456,
                "review_summary": "ok",
                "nutrients": [],
                "skip_reasons": [],
            },
            "transcript_window": {
                "session_id": "bad-window-session",
                "run_id": "run-bad-window-1",
                "messages": "broken",
            },
        },
        _ctx(),
    )

    assert result.ok
    review_data = json.loads(
        (root_dir / "reviews" / "run-bad-window-1.json").read_text(encoding="utf-8")
    )
    assert review_data["transcript_window"]["included_turns"] == []


@pytest.mark.unit
async def test_evolution_write_tool_recovers_parent_run_from_reviewer_session(
    tmp_path: Path,
) -> None:
    root_dir = tmp_path / ".kongming" / "evolution"
    store = EvolutionStore(
        root_dir=root_dir,
        state_store=EvolutionStateStore(root_dir),
    )
    tool = build_evolution_write_tool(store, min_confidence=0.75, max_nutrients=2)

    result = await tool.execute(
        {
            "review_result": {
                "reviewed_at_ms": 789,
                "review_summary": "fallback",
                "nutrients": [
                    {
                        "kind": "workflow",
                        "title": "fallback nutrient",
                        "content": "recover parent ids from review session id",
                        "summary": "fallback",
                        "confidence": 0.92,
                        "evidence_turns": [1],
                        "source_run_id": "run-smoke-7",
                        "source_session_id": "smoke",
                        "suggested_target": "skill",
                        "tags": ["fallback"],
                    }
                ],
                "skip_reasons": [],
            },
            "trigger_reason": "cadence",
        },
        ToolContext(
            run_id="run-evo-review-smoke-run-smoke-7-1",
            session_id="evo-review-smoke-run-smoke-7",
            turn=1,
            call_id="call-1",
        ),
    )

    assert result.ok
    assert result.data["status"] == "written"
    review_data = json.loads(
        (root_dir / "reviews" / "run-smoke-7.json").read_text(encoding="utf-8")
    )
    assert review_data["run_id"] == "run-smoke-7"
    assert review_data["session_id"] == "smoke"


@pytest.mark.unit
async def test_evolution_write_tool_skips_invalid_nutrient_items_and_writes_valid_ones(
    tmp_path: Path,
) -> None:
    root_dir = tmp_path / ".kongming" / "evolution"
    store = EvolutionStore(
        root_dir=root_dir,
        state_store=EvolutionStateStore(root_dir),
    )
    tool = build_evolution_write_tool(store, min_confidence=0.75, max_nutrients=2)

    result = await tool.execute(
        {
            "review_result": {
                "run_id": "run-invalid-nutrient-1",
                "session_id": "invalid-session",
                "reviewed_at_ms": 999,
                "review_summary": "mixed nutrients",
                "nutrients": [
                    {
                        "nutrient_id": "broken-1",
                        "kind": "memory",
                        "title": "broken",
                        "content": "",
                        "summary": "broken",
                        "confidence": 0.91,
                        "evidence_turns": [1],
                        "source_run_id": "run-invalid-nutrient-1",
                        "source_session_id": "invalid-session",
                    },
                    {
                        "nutrient_id": "valid-1",
                        "kind": "workflow",
                        "title": "valid",
                        "content": "Keep the valid nutrient.",
                        "summary": "valid",
                        "confidence": 0.93,
                        "evidence_turns": [2],
                        "source_run_id": "run-invalid-nutrient-1",
                        "source_session_id": "invalid-session",
                    },
                ],
                "skip_reasons": [],
            }
        },
        _ctx(),
    )

    assert result.ok
    assert result.data["status"] == "written"
    assert result.data["nutrients_written"] == 1
    queue_item = json.loads((root_dir / "evolution-nutrients.jsonl").read_text(encoding="utf-8"))
    assert queue_item["nutrient_id"] == "valid-1"
