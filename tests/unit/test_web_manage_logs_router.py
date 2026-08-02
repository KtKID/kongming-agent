"""日志查看 REST 端点单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hosts.web.dashboard.logs.registry import LogSourceRegistry
from hosts.web.dashboard.logs.router import router as logs_router
from hosts.web.dashboard.logs.service import LogReadService
from infrastructure.config.models import Config, ModelSelectionConfig


def _test_config() -> Config:
    return Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"))


@pytest.fixture()
def logs_client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    home = tmp_path / ".kongming"
    home.mkdir()
    cfg = _test_config()
    app.state.log_source_registry = LogSourceRegistry(cfg, home)
    app.state.log_read_service = LogReadService(app.state.log_source_registry)
    app.include_router(logs_router)
    return TestClient(app)


class TestListSources:
    def test_returns_200(self, logs_client: TestClient) -> None:
        resp = logs_client.get("/api/manage/logs/sources")
        assert resp.status_code == 200

    def test_returns_eight_sources(self, logs_client: TestClient) -> None:
        resp = logs_client.get("/api/manage/logs/sources")
        body = resp.json()
        assert len(body) == 8

    def test_returns_session_source_with_thread_context(self, logs_client: TestClient) -> None:
        resp = logs_client.get(
            "/api/manage/logs/sources",
            params={"thread_id": "thread-abcdef123456"},
        )
        body = resp.json()
        types = {item["type"] for item in body}
        assert resp.status_code == 200
        assert len(body) == 9
        assert "session_conversation" in types

    def test_invalid_thread_id_returns_400(self, logs_client: TestClient) -> None:
        resp = logs_client.get(
            "/api/manage/logs/sources",
            params={"thread_id": "../escape"},
        )
        assert resp.status_code == 400

    def test_source_has_required_fields(self, logs_client: TestClient) -> None:
        resp = logs_client.get("/api/manage/logs/sources")
        body = resp.json()
        src = body[0]
        for field in ("type", "label", "format", "description", "path", "exists"):
            assert field in src, f"missing field: {field}"


class TestReadLog:
    def test_unknown_type_returns_400(self, logs_client: TestClient) -> None:
        resp = logs_client.get("/api/manage/logs/read", params={"type": "nonexistent"})
        assert resp.status_code == 400

    def test_missing_file_returns_200_exists_false(self, logs_client: TestClient) -> None:
        resp = logs_client.get("/api/manage/logs/read", params={"type": "web_server"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"]["exists"] is False
        assert body["lines"] == []

    def test_existing_file_returns_lines(self, tmp_path: Path) -> None:
        app = FastAPI()
        home = tmp_path / ".kongming"
        home.mkdir()
        (home / "web").mkdir()
        (home / "web" / "server.log").write_text("line1\nline2\n", encoding="utf-8")
        cfg = _test_config()
        app.state.log_source_registry = LogSourceRegistry(cfg, home)
        app.state.log_read_service = LogReadService(app.state.log_source_registry)
        app.include_router(logs_router)
        client = TestClient(app)

        resp = client.get("/api/manage/logs/read", params={"type": "web_server"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"]["exists"] is True
        assert len(body["lines"]) == 2
        assert body["lines"][0]["raw"] == "line1"
        assert body["lines"][1]["raw"] == "line2"

    def test_session_conversation_file_returns_lines(self, tmp_path: Path) -> None:
        app = FastAPI()
        home = tmp_path / ".kongming"
        home.mkdir()
        thread_id = "thread-abcdef123456"
        session_dir = home / "sessions" / thread_id
        session_dir.mkdir(parents=True)
        session_file = session_dir / f"{thread_id}.jsonl"
        session_file.write_text(
            '{"record_type":"message","message":{"role":"user","content":"hello"}}\n',
            encoding="utf-8",
        )
        cfg = _test_config()
        app.state.log_source_registry = LogSourceRegistry(cfg, home)
        app.state.log_read_service = LogReadService(app.state.log_source_registry)
        app.include_router(logs_router)
        client = TestClient(app)

        resp = client.get(
            "/api/manage/logs/read",
            params={"type": "session_conversation", "thread_id": thread_id},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["source"]["type"] == "session_conversation"
        assert body["source"]["exists"] is True
        assert body["lines"][0]["parsed"]["message"]["content"] == "hello"

    def test_session_conversation_without_thread_id_returns_400(
        self, logs_client: TestClient
    ) -> None:
        resp = logs_client.get(
            "/api/manage/logs/read",
            params={"type": "session_conversation"},
        )
        assert resp.status_code == 400
