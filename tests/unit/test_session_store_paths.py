"""unit：session 持久化路径按 kongming_home 解析。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from infrastructure.config.models import Config, ModelSelectionConfig, SessionConfig
from sessions.file_session import FileSession
from sessions.session_bootstrap import SessionBootstrap
from sessions.session_store import SQLiteSession, build_session


def _base_config(session: SessionConfig) -> Config:
    """构造最小合法 Config，用本地模型地址避开远端 key 校验。"""
    return Config(
        model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"),
        session=session,
    )


def _bootstrap(cwd: Path) -> SessionBootstrap:
    """构造 file session 所需的稳定元数据。"""
    return SessionBootstrap(
        agent_name="test-agent",
        model_name="local-test-model",
        instruction_sources=[],
        instruction_text_hash="hash",
        created_at=time.time(),
        cwd=str(cwd),
    )


@pytest.mark.unit
def test_file_session_kongming_relative_path_uses_kongming_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("KONGMING_HOME", str(home))
    monkeypatch.chdir(workspace)
    cfg = _base_config(SessionConfig(backend="file", file_store_path=".kongming/sessions"))

    session = build_session(cfg, "sid-file", bootstrap=_bootstrap(workspace))

    assert isinstance(session, FileSession)
    assert session._store_path == (home / "sessions").resolve()


@pytest.mark.unit
def test_sqlite_session_kongming_relative_path_uses_kongming_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("KONGMING_HOME", str(home))
    monkeypatch.chdir(workspace)
    cfg = _base_config(SessionConfig(backend="sqlite", store_path=".kongming/sessions.db"))

    session = build_session(cfg, "sid-sqlite")

    assert isinstance(session, SQLiteSession)
    assert Path(session._db_path) == (home / "sessions.db").resolve()
