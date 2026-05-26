"""unit：ws thread boot evolution replay。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from config_loader.models import Config
from evolution.models import EvolutionNutrient, ReviewResult, ReviewWritePayload
from evolution.state_store import EvolutionStateStore
from evolution.store import EvolutionStore
from web.ws import _send_evolution_replay_frames


class _FakeWS:
    def __init__(self, cfg: Config) -> None:
        self.sent: list[dict[str, Any]] = []
        self.app = SimpleNamespace(state=SimpleNamespace(config=cfg))

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


class _FakeCell:
    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id


def _cfg(root_dir: Path) -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {"enabled": True, "dev_mode": True},
            "evolution": {
                "learning": {
                    "enabled": True,
                    "root_path": str(root_dir),
                }
            },
        }
    )


@pytest.mark.unit
async def test_send_evolution_replay_frames_replays_completed_notice(tmp_path: Path) -> None:
    root_dir = tmp_path / ".kongming" / "evolution"
    store = EvolutionStore(root_dir=root_dir, state_store=EvolutionStateStore(root_dir))
    await store.write_review(
        ReviewWritePayload(
            review_result=ReviewResult(
                run_id="run-parent-9",
                session_id="thread-xyz",
                reviewed_at_ms=333,
                review_summary="captured one nutrient",
                nutrients=(
                    EvolutionNutrient(
                        nutrient_id="nutrient-9",
                        kind="workflow",
                        title="Replay Me",
                        content="content",
                        summary="summary",
                        confidence=0.95,
                        evidence_turns=(4,),
                        source_run_id="run-parent-9",
                        source_session_id="thread-xyz",
                        suggested_target="skill",
                        tags=("workflow",),
                    ),
                ),
                skip_reasons=(),
            ),
            trigger_reason="cadence",
        )
    )
    ws = _FakeWS(_cfg(root_dir))

    await _send_evolution_replay_frames(ws, _FakeCell("thread-xyz"))

    assert len(ws.sent) == 1
    sent = ws.sent[0]
    assert sent["frame_type"] == "system.notice"
    assert sent["notice_key"] == "self_evolution.review"
    assert sent["status"] == "completed"
    assert sent["message"] == "发现 1 条进化养料"
    assert sent["details"]["review_id"] == "evo-review:run-parent-9"
    assert sent["details"]["pending_count"] == 1
