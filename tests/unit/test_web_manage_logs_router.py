"""日志查看 REST 端点单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infrastructure.config.models import Config, ModelConfig
from web.dashboard.logs.registry import LogSourceRegistry
from web.dashboard.logs.router import router as logs_router
from web.dashboard.logs.service import LogReadService


def _test_config() -> Config:
    return Config(model=ModelConfig(name="test-model", base_url="http://127.0.0.1:1234/v1"))


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

    def test_returns_seven_sources(self, logs_client: TestClient) -> None:
        resp = logs_client.get("/api/manage/logs/sources")
        body = resp.json()
        assert len(body) == 7

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
