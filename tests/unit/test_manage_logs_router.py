"""Unit tests for the manage-logs router (web.dashboard.logs.router)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hosts.web.dashboard.logs.registry import LogSourceRegistry
from hosts.web.dashboard.logs.router import router
from hosts.web.dashboard.logs.service import LogReadService
from infrastructure.config.models import Config, ModelConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _test_config() -> Config:
    return Config(model=ModelConfig(name="test-model", base_url="http://127.0.0.1:1234/v1"))


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    """Build a FastAPI TestClient with the logs router wired up."""
    app = FastAPI()

    home = tmp_path / ".kongming"
    home.mkdir()

    cfg = _test_config()
    registry = LogSourceRegistry(cfg, home)
    service = LogReadService(registry)

    app.state.log_source_registry = registry
    app.state.log_read_service = service

    app.include_router(router)
    return TestClient(app)


def _create_web_server_log(tmp_path: Path, content: str) -> None:
    """Helper: create web server log under tmp_path/.kongming."""
    home = tmp_path / ".kongming"
    log_dir = home / "web"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "server.log").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListSources:
    def test_list_sources(self, client: TestClient) -> None:
        resp = client.get("/api/manage/logs/sources")
        assert resp.status_code == 200

        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 7

        types = {item["type"] for item in data}
        assert "web_server" in types
        assert "full_log" in types
        assert "trace" in types


class TestReadLogValidType:
    def test_read_log_valid_type(self, client: TestClient, tmp_path: Path) -> None:
        _create_web_server_log(tmp_path, "hello\nworld\n")
        resp = client.get("/api/manage/logs/read", params={"type": "web_server"})
        assert resp.status_code == 200

        data = resp.json()
        assert data["source"]["type"] == "web_server"
        assert data["source"]["exists"] is True
        assert len(data["lines"]) == 2


class TestReadLogInvalidType:
    def test_read_log_invalid_type_400(self, client: TestClient) -> None:
        resp = client.get("/api/manage/logs/read", params={"type": "nonexistent"})
        assert resp.status_code == 400
        assert "Unknown log source type" in resp.json()["detail"]


class TestReadLogDefaultParams:
    def test_read_log_default_params(self, client: TestClient, tmp_path: Path) -> None:
        _create_web_server_log(tmp_path, "line1\n")
        # No tail_lines / max_bytes / query -- all use defaults.
        resp = client.get("/api/manage/logs/read", params={"type": "web_server"})
        assert resp.status_code == 200

        data = resp.json()
        assert data["source"]["exists"] is True
        assert len(data["lines"]) == 1
        assert data["truncated"] is False
