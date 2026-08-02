"""LogSourceRegistry 单元测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hosts.web.dashboard.logs.registry import LogSourceRegistry


def _make_config(
    full_log_path: str = ".kongming/logs/full_log.jsonl",
    trace_output_path: str = ".kongming/trace.jsonl",
    session_file_store_path: str = ".kongming/sessions",
) -> MagicMock:
    cfg = MagicMock()
    cfg.web.full_log.path = full_log_path
    cfg.trace.output_path = trace_output_path
    cfg.session.file_store_path = session_file_store_path
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

    def test_thread_context_adds_session_conversation_source(self, tmp_path: Path) -> None:
        cfg = _make_config()
        home = tmp_path / ".kongming"
        home.mkdir()
        reg = LogSourceRegistry(cfg, home)

        sources = reg.list_sources(thread_id="thread-abcdef123456")
        types = {s.type for s in sources}

        assert len(sources) == 9
        assert "session_conversation" in types

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
        assert Path(src.path).parts[-3:] == (
            "logs",
            "generic-channel",
            "generic-channel.jsonl",
        )

    def test_session_conversation_source(self, tmp_path: Path) -> None:
        cfg = _make_config()
        home = tmp_path / ".kongming"
        home.mkdir()
        reg = LogSourceRegistry(cfg, home)

        src = reg.get_source("session_conversation", thread_id="thread-abcdef123456")

        assert src.type == "session_conversation"
        assert src.label == "Session Conversation"
        assert src.format == "jsonl"
        assert (
            Path(src.path)
            == (home / "sessions" / "thread-abcdef123456" / "thread-abcdef123456.jsonl").resolve()
        )
        assert src.exists is False

    def test_session_conversation_requires_thread_id(self, tmp_path: Path) -> None:
        cfg = _make_config()
        home = tmp_path / ".kongming"
        home.mkdir()
        reg = LogSourceRegistry(cfg, home)

        with pytest.raises(ValueError, match="thread_id is required"):
            reg.get_source("session_conversation")

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

    def test_kongming_relative_config_paths_use_home(self, tmp_path: Path) -> None:
        cfg = _make_config(
            full_log_path=".kongming/logs/full_log.jsonl",
            trace_output_path=".kongming/trace.jsonl",
        )
        home = tmp_path / "home"
        home.mkdir()
        reg = LogSourceRegistry(cfg, home)

        assert reg.resolve_source_path("full_log") == (home / "logs" / "full_log.jsonl").resolve()
        assert reg.resolve_source_path("trace") == (home / "trace.jsonl").resolve()

    def test_unknown_type_raises(self, tmp_path: Path) -> None:
        cfg = _make_config()
        home = tmp_path / ".kongming"
        home.mkdir()
        reg = LogSourceRegistry(cfg, home)
        with pytest.raises(ValueError, match="Unknown log source type"):
            reg.resolve_source_path("nonexistent")

    def test_session_conversation_path_uses_thread_context(self, tmp_path: Path) -> None:
        cfg = _make_config()
        home = tmp_path / ".kongming"
        home.mkdir()
        reg = LogSourceRegistry(cfg, home)

        p = reg.resolve_source_path("session_conversation", thread_id="thread-abcdef123456")

        assert (
            p == (home / "sessions" / "thread-abcdef123456" / "thread-abcdef123456.jsonl").resolve()
        )

    def test_invalid_thread_id_raises(self, tmp_path: Path) -> None:
        cfg = _make_config()
        home = tmp_path / ".kongming"
        home.mkdir()
        reg = LogSourceRegistry(cfg, home)

        with pytest.raises(ValueError, match="Invalid thread_id"):
            reg.list_sources(thread_id="../escape")


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
