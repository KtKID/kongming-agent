from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from evolution.models import (
    DecisionItem,
    DecisionRecord,
    DecisionSummary,
    EvolutionNutrient,
    ReviewResult,
    ReviewWritePayload,
)
from evolution.state_store import EvolutionStateStore
from evolution.store import EvolutionStore
from hosts.web.app import create_app
from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from hosts.web.threads.metadata import ThreadMetadata
from infrastructure.config.models import Config
from tests.unit.test_web_app_lifespan import _seed_password

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


class FakeTM:
    def __init__(self, threads: list[ThreadMetadata]) -> None:
        self._threads = {thread.id: thread for thread in threads}
        self._started = False
        self._closed = False

    async def start(self) -> None:
        self._started = True

    async def aclose_all(self) -> None:
        self._closed = True

    def list_threads(self) -> list[ThreadMetadata]:
        return list(self._threads.values())

    async def create_thread(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def rename_thread(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def delete_thread(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    async def boot_or_attach(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def evict_cell(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    def list_cells(self) -> list[object]:
        return []

    def get_cell(self, thread_id: str) -> object | None:
        del thread_id
        return None

    def find_thread_by_claude_thread_id(self, claude_thread_id: str) -> ThreadMetadata | None:
        del claude_thread_id
        return None

    async def bind_claude_thread(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    # task#3.3：``add_thread_usage`` 已删除；mock 改提供 ``usage_manager``。
    usage_manager = None  # type: ignore[assignment]

    def resolve_approval(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None


def _cfg(root_dir: Path) -> Config:
    return Config.model_validate(
        {
            "model": {"preset_id": "local-gemma-4-e4b-it"},
            "web": {"enabled": True, "dev_mode": True},
            "evolution": {
                "learning": {
                    "enabled": True,
                    "root_path": str(root_dir),
                }
            },
        }
    )


@asynccontextmanager
async def _login_client(tmp_path: Path, tm: FakeTM, evolution_root: Path):
    _seed_password(tmp_path, "pwd")
    app = create_app(_cfg(evolution_root), tm, home_dir=tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/api/auth/login", json={"password": "pwd"}, headers=CSRF_HEADERS
            )
            assert resp.status_code == 200
            yield client


async def _seed_review(root_dir: Path, *, session_id: str, run_id: str) -> None:
    store = EvolutionStore(root_dir=root_dir, state_store=EvolutionStateStore(root_dir))
    await store.write_review(
        ReviewWritePayload(
            review_result=ReviewResult(
                run_id=run_id,
                session_id=session_id,
                reviewed_at_ms=123,
                review_summary="captured one nutrient",
                nutrients=(
                    EvolutionNutrient(
                        nutrient_id="nutrient-1",
                        kind="workflow",
                        title="Workflow One",
                        content="content one",
                        summary="summary one",
                        confidence=0.9,
                        evidence_turns=(1,),
                        source_run_id=run_id,
                        source_session_id=session_id,
                        suggested_target="skill",
                        tags=("workflow",),
                    ),
                ),
                skip_reasons=(),
            ),
            trigger_reason="cadence",
        )
    )


@pytest.mark.asyncio
async def test_get_evolution_reviews_returns_review_with_pending_summary(tmp_path: Path) -> None:
    evolution_root = tmp_path / ".kongming" / "evolution"
    await _seed_review(evolution_root, session_id="thread-000000000001", run_id="run-parent-1")
    tm = FakeTM(
        [
            ThreadMetadata(
                id="thread-000000000001",
                name="Demo",
                preset_id="preset-a",
                created_at=1.0,
                updated_at=2.0,
                message_count=0,
            )
        ]
    )
    async with _login_client(tmp_path, tm, evolution_root) as client:
        resp = await client.get(
            "/api/threads/thread-000000000001/evolution/reviews", headers=CSRF_HEADERS
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["review_id"] == "evo-review:run-parent-1"
        assert body[0]["decision_summary"]["pending"] == 1
        assert body[0]["nutrients"][0]["nutrient_id"] == "nutrient-1"


@pytest.mark.asyncio
async def test_post_evolution_decision_updates_review_summary(tmp_path: Path) -> None:
    evolution_root = tmp_path / ".kongming" / "evolution"
    workspace = tmp_path / "workspace"
    await _seed_review(evolution_root, session_id="thread-000000000001", run_id="run-parent-1")
    tm = FakeTM(
        [
            ThreadMetadata(
                id="thread-000000000001",
                name="Demo",
                preset_id="preset-a",
                cwd=str(workspace),
                created_at=1.0,
                updated_at=2.0,
                message_count=0,
            )
        ]
    )
    async with _login_client(tmp_path, tm, evolution_root) as client:
        resp = await client.post(
            "/api/threads/thread-000000000001/evolution/reviews/evo-review:run-parent-1/decisions",
            json={"nutrient_id": "nutrient-1", "decision": "accept_skill"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["review"]["decision_summary"]["accepted_skill"] == 1
        assert body["review"]["decision_summary"]["pending"] == 0
        assert body["review"]["decisions"][0]["target"] == "skill"
        assert body["review"]["decisions"][0]["applied_status"] == "written"
        assert body["review"]["decisions"][0]["applied_mode"] == "create"
        skill_path = workspace / ".kongming" / "skills" / "workflow-one" / "SKILL.md"
        assert Path(body["review"]["decisions"][0]["applied_path"]) == skill_path
        assert skill_path.exists()
        assert "### Nutrient `nutrient-1`" in skill_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_post_evolution_decision_accept_memory_materializes_workspace_memory(
    tmp_path: Path,
) -> None:
    evolution_root = tmp_path / ".kongming" / "evolution"
    workspace = tmp_path / "workspace"
    await _seed_review(evolution_root, session_id="thread-000000000001", run_id="run-parent-1")
    tm = FakeTM(
        [
            ThreadMetadata(
                id="thread-000000000001",
                name="Demo",
                preset_id="preset-a",
                cwd=str(workspace),
                created_at=1.0,
                updated_at=2.0,
                message_count=0,
            )
        ]
    )
    async with _login_client(tmp_path, tm, evolution_root) as client:
        resp = await client.post(
            "/api/threads/thread-000000000001/evolution/reviews/evo-review:run-parent-1/decisions",
            json={"nutrient_id": "nutrient-1", "decision": "accept_memory"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        item = body["review"]["decisions"][0]
        assert item["target"] == "memory"
        assert item["applied_status"] == "written"
        assert item["applied_mode"] == "append"
        memory_path = tmp_path / "memory" / "MEMORY.md"
        assert item["applied_path"] == str(memory_path)
        assert memory_path.exists()
        content = memory_path.read_text(encoding="utf-8")
        assert "summary one" in content or "content one" in content


@pytest.mark.asyncio
async def test_post_evolution_decision_ignore_keeps_workspace_unchanged(
    tmp_path: Path,
) -> None:
    evolution_root = tmp_path / ".kongming" / "evolution"
    workspace = tmp_path / "workspace"
    await _seed_review(
        evolution_root,
        session_id="thread-000000000001",
        run_id="run-parent-1",
    )
    tm = FakeTM(
        [
            ThreadMetadata(
                id="thread-000000000001",
                name="Demo",
                preset_id="preset-a",
                cwd=str(workspace),
                created_at=1.0,
                updated_at=2.0,
                message_count=0,
            )
        ]
    )
    async with _login_client(tmp_path, tm, evolution_root) as client:
        resp = await client.post(
            "/api/threads/thread-000000000001/evolution/reviews/evo-review:run-parent-1/decisions",
            json={"nutrient_id": "nutrient-1", "decision": "ignore"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        item = body["review"]["decisions"][0]
        assert item["target"] is None
        assert item["applied_status"] == "skipped"
        assert item["applied_mode"] == "ignore"
        assert item["applied_path"] is None
        assert body["review"]["decision_summary"]["ignored"] == 1
        assert list(workspace.rglob("*")) == []


@pytest.mark.asyncio
async def test_post_evolution_reapply_materializes_pending_memory_and_skill(tmp_path: Path) -> None:
    evolution_root = tmp_path / ".kongming" / "evolution"
    workspace = tmp_path / "workspace"
    store = EvolutionStore(root_dir=evolution_root, state_store=EvolutionStateStore(evolution_root))
    await store.write_review(
        ReviewWritePayload(
            review_result=ReviewResult(
                run_id="run-parent-1",
                session_id="thread-000000000001",
                reviewed_at_ms=123,
                review_summary="captured two nutrients",
                nutrients=(
                    EvolutionNutrient(
                        nutrient_id="nutrient-1",
                        kind="workflow",
                        title="Workflow One",
                        content="content one",
                        summary="summary one",
                        confidence=0.8,
                        evidence_turns=(1,),
                        source_run_id="run-parent-1",
                        source_session_id="thread-000000000001",
                        suggested_target="skill",
                    ),
                    EvolutionNutrient(
                        nutrient_id="nutrient-2",
                        kind="memory",
                        title="Memory Two",
                        content="remember second",
                        summary="summary two",
                        confidence=0.7,
                        evidence_turns=(2,),
                        source_run_id="run-parent-1",
                        source_session_id="thread-000000000001",
                        suggested_target="memory",
                    ),
                ),
                skip_reasons=(),
            )
        )
    )
    tm = FakeTM(
        [
            ThreadMetadata(
                id="thread-000000000001",
                name="Demo",
                preset_id="preset-a",
                cwd=str(workspace),
                created_at=1.0,
                updated_at=2.0,
                message_count=0,
            )
        ]
    )
    await store.write_decision(
        DecisionRecord(
            review_id="evo-review:run-parent-1",
            session_id="thread-000000000001",
            run_id="run-parent-1",
            summary=DecisionSummary(
                total=2,
                accepted_memory=0,
                accepted_skill=0,
                ignored=0,
                pending=2,
            ),
            items=(
                DecisionItem(
                    nutrient_id="nutrient-1",
                    decision="accept_skill",
                    target="skill",
                    decided_at_ms=999,
                ),
                DecisionItem(
                    nutrient_id="nutrient-2",
                    decision="accept_memory",
                    target="memory",
                    decided_at_ms=1000,
                ),
            ),
        )
    )
    async with _login_client(tmp_path, tm, evolution_root) as client:
        resp = await client.post(
            "/api/threads/thread-000000000001/evolution/reviews/evo-review:run-parent-1/reapply",
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        items = {item["nutrient_id"]: item for item in body["review"]["decisions"]}
        skill_item = items["nutrient-1"]
        assert skill_item["applied_status"] == "written"
        assert skill_item["applied_mode"] == "create"
        skill_path = workspace / ".kongming" / "skills" / "workflow-one" / "SKILL.md"
        assert skill_item["applied_path"] == str(skill_path)
        assert skill_path.exists()
        memory_item = items["nutrient-2"]
        assert memory_item["applied_status"] == "written"
        assert memory_item["applied_mode"] == "append"
        memory_path = tmp_path / "memory" / "MEMORY.md"
        assert memory_item["applied_path"] == str(memory_path)
        assert memory_path.exists()


@pytest.mark.asyncio
async def test_post_evolution_decision_rejects_wrong_thread_review(tmp_path: Path) -> None:
    evolution_root = tmp_path / ".kongming" / "evolution"
    await _seed_review(evolution_root, session_id="thread-000000000001", run_id="run-parent-1")
    tm = FakeTM(
        [
            ThreadMetadata(
                id="thread-000000000002",
                name="Other",
                preset_id="preset-a",
                created_at=1.0,
                updated_at=2.0,
                message_count=0,
            )
        ]
    )
    async with _login_client(tmp_path, tm, evolution_root) as client:
        resp = await client.post(
            "/api/threads/thread-000000000002/evolution/reviews/evo-review:run-parent-1/decisions",
            json={"nutrient_id": "nutrient-1", "decision": "accept_skill"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 404
