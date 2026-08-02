"""unit: evolution runtime event payload definitions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from evolution.events import (
    EvolutionReviewCompletedPayload,
    EvolutionReviewDrainTimeoutPayload,
    EvolutionReviewFailedPayload,
    EvolutionReviewStartedPayload,
)


@pytest.mark.unit
def test_started_payload_includes_runtime_cadence_metadata() -> None:
    payload = EvolutionReviewStartedPayload(
        review_id="evo-review:run-1",
        session_id="thread-aabbccddeeff",
        timeout_seconds=5.0,
        user_turn_count=3,
        included_turns=(1, 2, 3),
    ).to_payload()

    assert payload == {
        "review_id": "evo-review:run-1",
        "session_id": "thread-aabbccddeeff",
        "timeout_seconds": 5.0,
        "user_turn_count": 3,
        "included_turns": [1, 2, 3],
    }


@pytest.mark.unit
def test_completed_payload_extracts_write_data_fields() -> None:
    outcome = SimpleNamespace(
        write_status="written",
        duration_ms=123,
        timed_out=True,
        timeout_seconds=10.0,
        write_data={
            "nutrients_written": 2,
            "written_nutrient_ids": ["n1", "n2"],
            "review_summary": "发现两个可沉淀经验",
            "nutrient_summaries": ["路径需归一化", "提交前复跑测试"],
        },
    )

    payload = EvolutionReviewCompletedPayload.from_child_outcome(
        review_id="evo-review:run-2",
        review_run_id="review-run-2",
        session_id="thread-aabbccddeeff",
        outcome=outcome,
    ).to_payload()

    assert payload == {
        "review_id": "evo-review:run-2",
        "review_run_id": "review-run-2",
        "session_id": "thread-aabbccddeeff",
        "write_status": "written",
        "duration_ms": 123,
        "timeout_hit": True,
        "timeout_seconds": 10.0,
        "nutrients_written": 2,
        "written_nutrient_ids": ["n1", "n2"],
        "review_summary": "发现两个可沉淀经验",
        "nutrient_summaries": ["路径需归一化", "提交前复跑测试"],
        "timed_out_after_write": True,
    }


@pytest.mark.unit
def test_failed_payload_keeps_false_timeout_hit_and_omits_missing_fields() -> None:
    payload = EvolutionReviewFailedPayload(
        review_id="evo-review:run-3",
        session_id="thread-aabbccddeeff",
        error_kind="cancelled",
        timeout_hit=False,
    ).to_payload()

    assert payload == {
        "review_id": "evo-review:run-3",
        "session_id": "thread-aabbccddeeff",
        "error_kind": "cancelled",
        "timeout_hit": False,
    }


@pytest.mark.unit
def test_drain_timeout_payload_uses_list_for_wire_shape() -> None:
    payload = EvolutionReviewDrainTimeoutPayload.from_review_ids(
        pending_review_ids=("evo-review:run-1", "evo-review:run-2"),
        timeout_seconds=3.5,
    ).to_payload()

    assert payload == {
        "pending_review_ids": ["evo-review:run-1", "evo-review:run-2"],
        "timeout_seconds": 3.5,
    }
