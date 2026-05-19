"""create_app + lifespan 单测（v0.1.5）。

覆盖：

1. startup 调 thread_manager.start()
2. shutdown 调 thread_manager.aclose_all() 含 5s 超时
3. aclose_all 超时不阻断 shutdown
4. WebAuthNotConfiguredError：缺 password 时抛
5. app.state 正确挂载
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from config_loader.models import Config
from evolution.apply_executor import build_apply_job
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
from web.app import create_app


def _make_cfg(*, dev_mode: bool = True) -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {
                "enabled": True,
                "dev_mode": dev_mode,
                "idle_timeout_seconds": 1800,
                "idle_check_interval_seconds": 60,
                "pending_approval_timeout_seconds": 60,
            },
        }
    )


def _run_async(awaitable):  # type: ignore[no-untyped-def]
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(awaitable)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


class FakeThreadManager:
    """最小 ThreadManagerProtocol 实现（用于装配测试）。"""

    def __init__(self) -> None:
        self.start_called = 0
        self.aclose_called = 0
        self.aclose_delay = 0.0
        self._started = False
        self._closed = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        self.start_called += 1
        self._started = True

    async def aclose_all(self) -> None:
        self.aclose_called += 1
        if self.aclose_delay > 0:
            await asyncio.sleep(self.aclose_delay)
        self._closed = True

    async def create_thread(self, name: str, preset_id: str) -> Any:
        raise NotImplementedError

    async def rename_thread(self, thread_id: str, new_name: str) -> Any:
        raise NotImplementedError

    async def delete_thread(self, thread_id: str, *, keep_history: bool = False) -> None:
        return None

    async def boot_or_attach(self, thread_id: str) -> Any:
        raise NotImplementedError

    async def evict_cell(
        self,
        thread_id: str,
        reason: str,
        message: str | None = None,
        *,
        notify_ws: bool = True,
    ) -> None:
        return None

    def list_threads(self) -> list[Any]:
        return []

    def list_cells(self) -> list[Any]:
        return []

    def get_cell(self, thread_id: str) -> Any:
        return None

    def find_thread_by_claude_thread_id(self, claude_thread_id: str) -> Any:
        return None  # 默认无命中；测试可 monkeypatch

    async def bind_claude_thread(
        self,
        thread_id: str,
        claude_thread_id: str,
        cwd: str,
    ) -> Any:
        raise NotImplementedError  # 默认；测试 mock 时按需 override

    def resolve_approval(self, thread_id: str, call_id: str, approved: bool) -> None:
        return None


def _seed_password(home: Path, password: str = "test-password") -> None:
    """提前在 home/web/password.hash 落 hash，避免装配时抛 WebAuthNotConfiguredError。"""
    from web.auth_secrets import hash_password

    web_dir = home / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    h = hash_password(password)
    (web_dir / "password.hash").write_text(h, encoding="utf-8")


def test_lifespan_startup_calls_start(tmp_path: Path) -> None:
    """启动 app 时 thread_manager.start() 被调。"""
    cfg = _make_cfg()
    tm = FakeThreadManager()
    _seed_password(tmp_path)

    app = create_app(cfg, tm, home_dir=tmp_path)

    with TestClient(app) as _client:
        # 进入 with 触发 startup
        assert tm.start_called == 1
        assert tm.started is True


def test_lifespan_shutdown_calls_aclose_all(tmp_path: Path) -> None:
    cfg = _make_cfg()
    tm = FakeThreadManager()
    _seed_password(tmp_path)

    app = create_app(cfg, tm, home_dir=tmp_path)

    with TestClient(app):
        pass
    # 退出 with → shutdown
    assert tm.aclose_called == 1


def test_lifespan_shutdown_timeout_warns_but_does_not_raise(tmp_path: Path) -> None:
    """aclose_all 超时不让 app 退出失败。"""
    cfg = _make_cfg()
    tm = FakeThreadManager()
    tm.aclose_delay = 1.0  # > 0.2s 超时
    _seed_password(tmp_path)

    app = create_app(cfg, tm, home_dir=tmp_path, lifespan_shutdown_timeout=0.2)

    # 不应抛
    with TestClient(app):
        pass
    assert tm.aclose_called == 1


def test_app_state_attached(tmp_path: Path) -> None:
    cfg = _make_cfg()
    tm = FakeThreadManager()
    _seed_password(tmp_path)

    app = create_app(cfg, tm, home_dir=tmp_path)

    assert app.state.config is cfg
    assert app.state.thread_manager is tm
    assert app.state.serializer is not None
    assert app.state.password_hash is not None
    assert app.state.rate_limiter is not None
    assert app.state.kongming_home == tmp_path


def test_password_not_configured_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 password.hash + env → WebAuthNotConfiguredError。"""
    # 隔离环境变量，还原"真的没配密码"场景
    monkeypatch.delenv("KONGMING_WEB_PASSWORD", raising=False)
    cfg = _make_cfg()
    tm = FakeThreadManager()
    # 不调 _seed_password

    from web.errors import WebAuthNotConfiguredError

    with pytest.raises(WebAuthNotConfiguredError):
        create_app(cfg, tm, home_dir=tmp_path)


def test_initial_password_from_config_bootstraps_hash(tmp_path: Path) -> None:
    cfg = Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {
                "enabled": True,
                "dev_mode": True,
                "initial_password": "bootstrap-password",
                "idle_timeout_seconds": 1800,
                "idle_check_interval_seconds": 60,
                "pending_approval_timeout_seconds": 60,
            },
        }
    )
    tm = FakeThreadManager()

    app = create_app(cfg, tm, home_dir=tmp_path)

    assert app.state.password_hash is not None
    assert (tmp_path / "web" / "password.hash").is_file()


def test_lifespan_startup_recovers_pending_evolution_apply_jobs(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    evolution_root = tmp_path / ".kongming" / "evolution"
    store = EvolutionStore(root_dir=evolution_root, state_store=EvolutionStateStore(evolution_root))
    review = ReviewResult(
        run_id="run-parent-1",
        session_id="thread-demo",
        reviewed_at_ms=123,
        review_summary="captured one nutrient",
        nutrients=(
            EvolutionNutrient(
                nutrient_id="nutrient-1",
                kind="memory",
                title="Memory One",
                content="workspace keeps a stable note layout",
                summary="workspace keeps a stable note layout",
                confidence=0.9,
                evidence_turns=(1,),
                source_run_id="run-parent-1",
                source_session_id="thread-demo",
                suggested_target="memory",
                tags=("memory",),
            ),
        ),
        skip_reasons=(),
    )
    decision = DecisionRecord(
        review_id="evo-review:run-parent-1",
        session_id="thread-demo",
        run_id="run-parent-1",
        summary=DecisionSummary(
            total=1,
            accepted_memory=1,
            accepted_skill=0,
            ignored=0,
            pending=0,
        ),
        items=(
            DecisionItem(
                nutrient_id="nutrient-1",
                decision="accept_memory",
                target="memory",
                decided_at_ms=1000,
            ),
        ),
    )
    _run_async(
        store.write_review(
            ReviewWritePayload(
                review_result=review,
                trigger_reason="cadence",
            )
        )
    )
    _run_async(store.write_decision(decision))
    _run_async(
        store.write_apply_job(
            build_apply_job(
                review_id=decision.review_id,
                session_id=decision.session_id,
                run_id=decision.run_id,
                nutrient_id="nutrient-1",
                decision=decision.items[0],
                workspace_root=workspace,
                created_at_ms=1001,
            )
        )
    )

    cfg = Config.model_validate(
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
                    "root_path": str(evolution_root),
                }
            },
        }
    )
    tm = FakeThreadManager()
    _seed_password(tmp_path)

    app = create_app(cfg, tm, home_dir=tmp_path)

    with TestClient(app):
        pass

    memory_path = workspace / ".kongming" / "memory" / "MEMORY.md"
    assert memory_path.exists()
    content = memory_path.read_text(encoding="utf-8")
    assert "workspace keeps a stable note layout" in content
    recovered_decision = _run_async(store.read_decision(decision.review_id))
    assert recovered_decision is not None
    assert recovered_decision.items[0].applied_status == "written"
    recovered_job = _run_async(store.read_apply_job("apply:evo-review:run-parent-1:nutrient-1"))
    assert recovered_job is not None
    assert recovered_job.status == "finished"
