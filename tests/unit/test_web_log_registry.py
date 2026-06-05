"""LogSourceRegistry 单元测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from web.dashboard.logs.registry import LogSourceRegistry


def _make_config(
    full_log_path: str = ".kongming/logs/full_log.jsonl",
    trace_output_path: str = ".kongming/trace.jsonl",
) -> MagicMock:
    cfg = MagicMock()
    cfg.web.full_log.path = full_log_path
    cfg.trace.output_path = trace_output_path
    return cfg


class TestListSources:
    def test_returns_eight_items(self, tmp_path: Path) -> None:
        cfg = _make_config()
        home = tmp_path / ".kongming"
        home.mkdir()
        reg = LogSourceRegistry(cfg, home)
        sources = reg.list_sources()
        assert len(sources) == 8

    def test_all_types_present(self, tmp_path: Path) -> None:
        cfg = _make_config()
        home = tmp_path / ".kongming"
        home.mkdir()
        reg = LogSourceRegistry(cfg, home)
        types = {s.type for s in reg.list_sources()}
        expected = {
            "web_server",
            "full_log",
            "trace",
            "heartbeat",
            "generic_channel",
            "evolution",
            "cron_audit",
            "auto_approval_audit",
        }
        assert types == expected

    def test_missing_files_have_exists_false(self, tmp_path: Path) -> None:
        home = tmp_path / ".kongming"
        home.mkdir()
        cfg = _make_config(
            full_log_path=str(home / "logs" / "missing-full-log.jsonl"),
            trace_output_path=str(home / "missing-trace.jsonl"),
        )
        reg = LogSourceRegistry(cfg, home)
        for src in reg.list_sources():
            assert src.exists is False
            assert src.size_bytes is None
            assert src.updated_at_ms is None


class TestGetSource:
    def test_known_type(self, tmp_path: Path) -> None:
        cfg = _make_config()
        home = tmp_path / ".kongming"
        home.mkdir()
        reg = LogSourceRegistry(cfg, home)
        src = reg.get_source("web_server")
        assert src.type == "web_server"
        assert src.format == "plain"

    def test_generic_channel_source(self, tmp_path: Path) -> None:
        cfg = _make_config()
        home = tmp_path / ".kongming"
        home.mkdir()
        reg = LogSourceRegistry(cfg, home)
        src = reg.get_source("generic_channel")
        assert src.type == "generic_channel"
        assert src.format == "jsonl"
        assert src.path.endswith("logs/generic-channel/generic-channel.jsonl")

    def test_unknown_type_raises(self, tmp_path: Path) -> None:
        cfg = _make_config()
        home = tmp_path / ".kongming"
        home.mkdir()
        reg = LogSourceRegistry(cfg, home)
        with pytest.raises(ValueError, match="Unknown log source type"):
            reg.get_source("nonexistent")


class TestResolveSourcePath:
    def test_returns_absolute_path(self, tmp_path: Path) -> None:
        cfg = _make_config()
        home = tmp_path / ".kongming"
        home.mkdir()
        reg = LogSourceRegistry(cfg, home)
        p = reg.resolve_source_path("web_server")
        assert p.is_absolute()

    def test_unknown_type_raises(self, tmp_path: Path) -> None:
        cfg = _make_config()
        home = tmp_path / ".kongming"
        home.mkdir()
        reg = LogSourceRegistry(cfg, home)
        with pytest.raises(ValueError, match="Unknown log source type"):
            reg.resolve_source_path("nonexistent")


class TestExistingFile:
    def test_file_exists_true(self, tmp_path: Path) -> None:
        cfg = _make_config()
        home = tmp_path / ".kongming"
        home.mkdir()
        (home / "web").mkdir()
        (home / "web" / "server.log").write_text("hello", encoding="utf-8")
        reg = LogSourceRegistry(cfg, home)
        src = reg.get_source("web_server")
        assert src.exists is True
        assert src.size_bytes is not None
        assert src.size_bytes > 0
        assert src.updated_at_ms is not None
        assert src.updated_at_ms > 0


class TestOutOfBoundPath:
    def test_resolve_rejects_escape(self, tmp_path: Path) -> None:
        cfg = _make_config(full_log_path="/etc/passwd")
        home = tmp_path / ".kongming"
        home.mkdir()
        reg = LogSourceRegistry(cfg, home)
        with pytest.raises(ValueError, match="outside allowed roots"):
            reg.resolve_source_path("full_log")

    def test_get_source_graceful_on_escape(self, tmp_path: Path) -> None:
        cfg = _make_config(full_log_path="/etc/passwd")
        home = tmp_path / ".kongming"
        home.mkdir()
        reg = LogSourceRegistry(cfg, home)
        src = reg.get_source("full_log")
        assert src.exists is False
