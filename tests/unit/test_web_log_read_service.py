"""Unit tests for LogReadService (web.dashboard.logs.service)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from infrastructure.config.models import Config, ModelConfig
from web.dashboard.logs.registry import LogSourceRegistry
from web.dashboard.logs.service import LogReadService
from web.protocol.log_dto import LogReadResponseDTO

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _test_config() -> Config:
    return Config(model=ModelConfig(name="test-model", base_url="http://127.0.0.1:1234/v1"))


@pytest.fixture()
def kongming_home(tmp_path: Path) -> Path:
    """Create a temporary kongming_home directory."""
    home = tmp_path / ".kongming"
    home.mkdir()
    return home


@pytest.fixture()
def registry(kongming_home: Path) -> LogSourceRegistry:
    """Build a LogSourceRegistry with default config and temp home."""
    cfg = _test_config()
    return LogSourceRegistry(cfg, kongming_home)


@pytest.fixture()
def service(registry: LogSourceRegistry) -> LogReadService:
    """Build a LogReadService backed by the temp registry."""
    return LogReadService(registry)


def _create_web_server_log(kongming_home: Path, content: str) -> Path:
    """Helper: create the web server log file and return its path."""
    log_dir = kongming_home / "web"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "server.log"
    log_file.write_text(content, encoding="utf-8")
    return log_file


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReadTailMissingFile:
    def test_read_tail_missing_file_returns_empty(self, service: LogReadService) -> None:
        resp = service.read_tail("web_server")
        assert isinstance(resp, LogReadResponseDTO)
        assert resp.lines == []
        assert resp.source.exists is False
        assert resp.truncated is False
        assert resp.read_bytes == 0


class TestReadTailEmptyFile:
    def test_read_tail_empty_file(self, service: LogReadService, kongming_home: Path) -> None:
        _create_web_server_log(kongming_home, "")
        resp = service.read_tail("web_server")
        assert resp.lines == []
        assert resp.source.exists is True
        assert resp.truncated is False


class TestReadTailSmallFile:
    def test_read_tail_small_file(self, service: LogReadService, kongming_home: Path) -> None:
        _create_web_server_log(kongming_home, "hello\nworld\n")
        resp = service.read_tail("web_server")
        assert len(resp.lines) == 2
        assert resp.lines[0].raw == "hello"
        assert resp.lines[1].raw == "world"


class TestReadTailJsonlFile:
    def test_read_tail_jsonl_file(self, service: LogReadService, kongming_home: Path) -> None:
        # Create a JSONL file as the cron_audit source.
        audit_dir = kongming_home / "cron"
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit_file = audit_dir / "audits.jsonl"

        lines = [
            json.dumps({"ts": 1, "msg": "first"}),
            json.dumps({"ts": 2, "msg": "second"}),
        ]
        audit_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        resp = service.read_tail("cron_audit")
        assert len(resp.lines) == 2
        assert resp.lines[0].parsed is not None
        assert resp.lines[0].parsed["msg"] == "first"
        assert resp.lines[1].parsed["ts"] == 2
        assert resp.lines[0].parse_error is None


class TestReadTailTruncation:
    def test_read_tail_truncation(self, service: LogReadService, kongming_home: Path) -> None:
        # Create a file larger than max_bytes.
        line = "A" * 200 + "\n"
        big_content = line * 100  # ~20 KiB
        _create_web_server_log(kongming_home, big_content)

        resp = service.read_tail("web_server", max_bytes=512)
        assert resp.truncated is True
        assert resp.read_bytes == 512
        assert resp.total_bytes > 512


class TestReadTailQueryFilter:
    def test_read_tail_query_filter(self, service: LogReadService, kongming_home: Path) -> None:
        _create_web_server_log(kongming_home, "error: bad\ninfo: ok\nerror: worse\n")

        resp = service.read_tail("web_server", query="error")
        assert len(resp.lines) == 2
        assert "error" in resp.lines[0].raw.lower()
        assert "error" in resp.lines[1].raw.lower()


class TestReadTailUnknownType:
    def test_read_tail_unknown_type_raises(self, service: LogReadService) -> None:
        with pytest.raises(ValueError, match="Unknown log source type"):
            service.read_tail("nonexistent")


class TestReadTailTailLinesLimit:
    def test_read_tail_tail_lines_limit(self, service: LogReadService, kongming_home: Path) -> None:
        # Create a file with many lines.
        lines = [f"line {i}" for i in range(100)]
        _create_web_server_log(kongming_home, "\n".join(lines))

        # Request more than _MAX_TAIL_LINES (5000) -- service should clamp.
        resp = service.read_tail("web_server", tail_lines=9999)
        # All 100 lines are within the clamp, so we get all of them.
        assert len(resp.lines) == 100
