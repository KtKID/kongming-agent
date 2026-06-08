"""司天 scanner 扩展：recentUserMessages / recentAssistantMessages / recentSessions 注入。

覆盖 #6（claude_project）+ #7（claude_workspace）+ 向后兼容。
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sitian.config import SiTianScannerConfig, SiTianSourceConfig
from sitian.scanners import SiTianScanSource

OBSERVED_AT = "2026-05-09T10:00:00Z"
OBSERVED_TS = datetime(2026, 5, 9, 10, 0, 0, tzinfo=UTC).timestamp()


def _set_mtime(path: Path, ts: float) -> None:
    """显式设置文件 mtime，用来制造窗口边界场景。"""
    os.utime(path, times=(ts, ts))


def _write_session_jsonl(
    path: Path,
    *,
    cwd: str,
    entries: list[dict[str, object]],
    mtime_ts: float | None = None,
) -> Path:
    """在 ``path`` 落一个 jsonl 文件。entries 中的 user/assistant 类型如果不含 cwd
    自动补 ``cwd``（global_scanner 需要从首条带 cwd 的记录提取 real_cwd）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    for entry in entries:
        if entry.get("type") in {"user", "assistant"} and "cwd" not in entry:
            entry = {**entry, "cwd": cwd}
        rows.append(json.dumps(entry))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    if mtime_ts is not None:
        _set_mtime(path, mtime_ts)
    return path


def _claude_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    project_subdir: str = "repo",
    project_dir_name: str = "encoded",
) -> tuple[Path, Path, Path]:
    """造一套 ~/.claude/projects/<encoded>/ + project workdir，并 monkeypatch CLAUDE_HOME。

    返回 (project_dir, claude_home, session_dir)。
    """
    project_dir = tmp_path / project_subdir
    project_dir.mkdir()
    (project_dir / "README.md").write_text("hello", encoding="utf-8")

    claude_home = tmp_path / ".claude"
    session_dir = claude_home / "projects" / project_dir_name
    session_dir.mkdir(parents=True)

    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    return project_dir, claude_home, session_dir


# ---------------------------------------------------------------------------
# claude_project 路径（#6）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_claude_project_thread_obs_carries_recent_messages_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir, _, session_dir = _claude_setup(tmp_path, monkeypatch)
    _write_session_jsonl(
        session_dir / "thread-1.jsonl",
        cwd=str(project_dir),
        entries=[
            {"type": "user", "message": {"role": "user", "content": "first user msg"}},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": "first asst reply"},
            },
            {"type": "user", "message": {"role": "user", "content": "second user msg"}},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": "second asst reply"},
            },
        ],
        mtime_ts=OBSERVED_TS - 60,  # 1 分钟前，在 2 天窗口内
    )

    batch = asyncio.run(
        SiTianScanSource(
            SiTianSourceConfig(
                id="claude-alpha",
                kind="claude_project",
                path=str(project_dir),
            ),
            observed_at=OBSERVED_AT,
            scanner_config=SiTianScannerConfig(),
        )
    )

    threads = [obs for obs in batch.observations if obs.entity_type == "thread"]
    assert len(threads) == 1
    payload = threads[0].payload
    assert payload["recentUserMessages"] == ["second user msg"]
    assert payload["recentAssistantMessages"] == ["second asst reply"]


@pytest.mark.unit
def test_claude_project_respects_window_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir, _, session_dir = _claude_setup(tmp_path, monkeypatch)

    fresh = _write_session_jsonl(
        session_dir / "thread-fresh.jsonl",
        cwd=str(project_dir),
        entries=[
            {"type": "user", "message": {"role": "user", "content": "fresh question"}},
        ],
        mtime_ts=OBSERVED_TS - 3600,  # 1 小时前，窗口内
    )
    old = _write_session_jsonl(
        session_dir / "thread-old.jsonl",
        cwd=str(project_dir),
        entries=[
            {"type": "user", "message": {"role": "user", "content": "old question"}},
        ],
        mtime_ts=OBSERVED_TS - 5 * 86400,  # 5 天前，2 天窗口外
    )
    assert fresh.exists() and old.exists()  # 防 unused-var

    batch = asyncio.run(
        SiTianScanSource(
            SiTianSourceConfig(
                id="claude-alpha",
                kind="claude_project",
                path=str(project_dir),
            ),
            observed_at=OBSERVED_AT,
            scanner_config=SiTianScannerConfig(recent_session_window_days=2),
        )
    )

    threads = [obs for obs in batch.observations if obs.entity_type == "thread"]
    assert len(threads) == 1
    assert threads[0].payload["threadId"] == "thread-fresh"


@pytest.mark.unit
def test_claude_project_window_zero_disables_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir, _, session_dir = _claude_setup(tmp_path, monkeypatch)

    _write_session_jsonl(
        session_dir / "thread-fresh.jsonl",
        cwd=str(project_dir),
        entries=[{"type": "user", "message": {"role": "user", "content": "a"}}],
        mtime_ts=OBSERVED_TS - 3600,
    )
    _write_session_jsonl(
        session_dir / "thread-old.jsonl",
        cwd=str(project_dir),
        entries=[{"type": "user", "message": {"role": "user", "content": "b"}}],
        mtime_ts=OBSERVED_TS - 30 * 86400,  # 30 天前
    )

    batch = asyncio.run(
        SiTianScanSource(
            SiTianSourceConfig(
                id="claude-alpha",
                kind="claude_project",
                path=str(project_dir),
            ),
            observed_at=OBSERVED_AT,
            scanner_config=SiTianScannerConfig(recent_session_window_days=0),
        )
    )

    threads = [obs for obs in batch.observations if obs.entity_type == "thread"]
    assert {t.payload["threadId"] for t in threads} == {"thread-fresh", "thread-old"}


@pytest.mark.unit
def test_claude_project_user_count_zero_returns_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir, _, session_dir = _claude_setup(tmp_path, monkeypatch)
    _write_session_jsonl(
        session_dir / "thread-1.jsonl",
        cwd=str(project_dir),
        entries=[
            {"type": "user", "message": {"role": "user", "content": "irrelevant"}},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": "keep me"},
            },
        ],
        mtime_ts=OBSERVED_TS - 60,
    )

    batch = asyncio.run(
        SiTianScanSource(
            SiTianSourceConfig(
                id="claude-alpha",
                kind="claude_project",
                path=str(project_dir),
            ),
            observed_at=OBSERVED_AT,
            scanner_config=SiTianScannerConfig(
                session_recent_user_messages=0,
                session_recent_assistant_messages=1,
            ),
        )
    )

    threads = [obs for obs in batch.observations if obs.entity_type == "thread"]
    payload = threads[0].payload
    assert payload["recentUserMessages"] == []
    assert payload["recentAssistantMessages"] == ["keep me"]


@pytest.mark.unit
def test_claude_project_max_chars_truncates_with_ellipsis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir, _, session_dir = _claude_setup(tmp_path, monkeypatch)
    long_content = "x" * 800
    _write_session_jsonl(
        session_dir / "thread-1.jsonl",
        cwd=str(project_dir),
        entries=[
            {"type": "user", "message": {"role": "user", "content": long_content}},
        ],
        mtime_ts=OBSERVED_TS - 60,
    )

    batch = asyncio.run(
        SiTianScanSource(
            SiTianSourceConfig(
                id="claude-alpha",
                kind="claude_project",
                path=str(project_dir),
            ),
            observed_at=OBSERVED_AT,
            scanner_config=SiTianScannerConfig(session_message_max_chars=50),
        )
    )

    threads = [obs for obs in batch.observations if obs.entity_type == "thread"]
    msg = threads[0].payload["recentUserMessages"][0]
    assert msg.startswith("x" * 50)
    assert msg.endswith("…")
    assert len(msg) == 51


# ---------------------------------------------------------------------------
# claude_workspace 路径（#7）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_claude_workspace_thread_obs_carries_recent_messages(
    tmp_path: Path,
) -> None:
    claude_home = tmp_path / ".claude"
    projects_dir = claude_home / "projects"
    project_dir = tmp_path / "real-proj"
    project_dir.mkdir()
    encoded = "encoded-proj"

    session_path = projects_dir / encoded / "thread-1.jsonl"
    _write_session_jsonl(
        session_path,
        cwd=str(project_dir),
        entries=[
            {"type": "user", "message": {"role": "user", "content": "workspace user msg"}},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": "workspace asst reply"},
            },
        ],
        mtime_ts=OBSERVED_TS - 60,
    )

    batch = asyncio.run(
        SiTianScanSource(
            SiTianSourceConfig(
                id="ws",
                kind="claude_workspace",
                path=str(claude_home),
                top_n=5,
            ),
            observed_at=OBSERVED_AT,
            scanner_config=SiTianScannerConfig(),
        )
    )

    threads = [obs for obs in batch.observations if obs.entity_type == "thread"]
    assert len(threads) == 1
    payload = threads[0].payload
    assert payload["recentUserMessages"] == ["workspace user msg"]
    assert payload["recentAssistantMessages"] == ["workspace asst reply"]


@pytest.mark.unit
def test_claude_workspace_project_obs_carries_recent_sessions_array(
    tmp_path: Path,
) -> None:
    claude_home = tmp_path / ".claude"
    project_dir = tmp_path / "real-proj"
    project_dir.mkdir()
    encoded = "encoded-proj"
    base = claude_home / "projects" / encoded

    # 3 个 session，全部在 2 天窗口内（mtime 越新越靠前）
    for i, ts_offset in enumerate([-60, -3600, -7200], start=1):
        _write_session_jsonl(
            base / f"thread-{i}.jsonl",
            cwd=str(project_dir),
            entries=[
                {"type": "user", "message": {"role": "user", "content": f"u{i}"}},
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": f"a{i}"},
                },
            ],
            mtime_ts=OBSERVED_TS + ts_offset,
        )

    batch = asyncio.run(
        SiTianScanSource(
            SiTianSourceConfig(
                id="ws",
                kind="claude_workspace",
                path=str(claude_home),
                top_n=5,
            ),
            observed_at=OBSERVED_AT,
            scanner_config=SiTianScannerConfig(),
        )
    )

    project_obs = [obs for obs in batch.observations if obs.entity_type == "project"]
    assert len(project_obs) == 1
    recent = project_obs[0].payload["recentSessions"]
    assert isinstance(recent, list)
    assert len(recent) == 3
    # 按 lastModified 倒序（最新在前）
    assert recent[0]["threadId"] == "thread-1"
    assert recent[0]["recentUserMessages"] == ["u1"]
    assert recent[0]["recentAssistantMessages"] == ["a1"]
    assert recent[1]["threadId"] == "thread-2"
    assert recent[2]["threadId"] == "thread-3"


@pytest.mark.unit
def test_claude_workspace_recent_sessions_filtered_by_window(
    tmp_path: Path,
) -> None:
    claude_home = tmp_path / ".claude"
    project_dir = tmp_path / "real-proj"
    project_dir.mkdir()
    encoded = "encoded-proj"
    base = claude_home / "projects" / encoded

    _write_session_jsonl(
        base / "thread-fresh.jsonl",
        cwd=str(project_dir),
        entries=[{"type": "user", "message": {"role": "user", "content": "fresh"}}],
        mtime_ts=OBSERVED_TS - 60,
    )
    _write_session_jsonl(
        base / "thread-old.jsonl",
        cwd=str(project_dir),
        entries=[{"type": "user", "message": {"role": "user", "content": "stale"}}],
        mtime_ts=OBSERVED_TS - 5 * 86400,  # 5 天前
    )

    batch = asyncio.run(
        SiTianScanSource(
            SiTianSourceConfig(
                id="ws",
                kind="claude_workspace",
                path=str(claude_home),
                top_n=5,
            ),
            observed_at=OBSERVED_AT,
            scanner_config=SiTianScannerConfig(recent_session_window_days=2),
        )
    )

    project_obs = [obs for obs in batch.observations if obs.entity_type == "project"]
    recent = project_obs[0].payload["recentSessions"]
    thread_ids = [item["threadId"] for item in recent]
    assert thread_ids == ["thread-fresh"]


@pytest.mark.unit
def test_claude_workspace_zero_counts_yield_empty_arrays(
    tmp_path: Path,
) -> None:
    claude_home = tmp_path / ".claude"
    project_dir = tmp_path / "real-proj"
    project_dir.mkdir()
    encoded = "encoded-proj"
    base = claude_home / "projects" / encoded

    _write_session_jsonl(
        base / "thread-1.jsonl",
        cwd=str(project_dir),
        entries=[
            {"type": "user", "message": {"role": "user", "content": "u"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "a"}},
        ],
        mtime_ts=OBSERVED_TS - 60,
    )

    batch = asyncio.run(
        SiTianScanSource(
            SiTianSourceConfig(
                id="ws",
                kind="claude_workspace",
                path=str(claude_home),
                top_n=5,
            ),
            observed_at=OBSERVED_AT,
            scanner_config=SiTianScannerConfig(
                session_recent_user_messages=0,
                session_recent_assistant_messages=0,
            ),
        )
    )

    threads = [obs for obs in batch.observations if obs.entity_type == "thread"]
    assert threads[0].payload["recentUserMessages"] == []
    assert threads[0].payload["recentAssistantMessages"] == []

    project_obs = [obs for obs in batch.observations if obs.entity_type == "project"]
    recent = project_obs[0].payload["recentSessions"]
    assert recent[0]["recentUserMessages"] == []
    assert recent[0]["recentAssistantMessages"] == []


# ---------------------------------------------------------------------------
# 向后兼容（不传 scanner_config）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_claude_project_without_scanner_config_uses_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不传 scanner_config，应回退默认（window_days=3, user=1, assistant=1, max=500），
    并且原有 payload 字段全部保留。"""
    project_dir, _, session_dir = _claude_setup(tmp_path, monkeypatch)
    _write_session_jsonl(
        session_dir / "thread-1.jsonl",
        cwd=str(project_dir),
        entries=[
            {"type": "user", "message": {"role": "user", "content": "hello"}},
        ],
        mtime_ts=OBSERVED_TS - 60,
    )

    batch = asyncio.run(
        SiTianScanSource(
            SiTianSourceConfig(
                id="claude-alpha",
                kind="claude_project",
                path=str(project_dir),
            ),
            observed_at=OBSERVED_AT,
        )
    )

    threads = [obs for obs in batch.observations if obs.entity_type == "thread"]
    payload = threads[0].payload
    # 原有字段仍在
    assert payload["threadId"] == "thread-1"
    assert payload["title"] == "hello"
    assert payload["cwd"] == str(project_dir)
    assert "lastModified" in payload
    assert "messageCount" in payload
    assert "file" in payload
    # 新字段也有
    assert payload["recentUserMessages"] == ["hello"]


# ---------------------------------------------------------------------------
# env override smoke + cutoff helper unit tests（R3 P1 补测）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_env_override_sitian_scanner_window_days(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yaml_path = tmp_path / "setting.yaml"
    yaml_path.write_text(
        "model:\n  name: stub\n  base_url: http://127.0.0.1:1234/v1\n  api_key: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KONGMING_SITIAN_SCANNER_RECENT_SESSION_WINDOW_DAYS", "7")
    from infrastructure.config import load_config

    cfg = load_config(yaml_path)
    assert cfg.sitian.scanner.recent_session_window_days == 7


@pytest.mark.unit
def test_cutoff_ts_normal() -> None:
    from sitian.scanners import _compute_window_cutoff_ts

    ts = _compute_window_cutoff_ts("2026-05-09T10:00:00Z", 2)
    assert ts is not None
    expected = datetime(2026, 5, 7, 10, 0, 0, tzinfo=UTC).timestamp()
    assert abs(ts - expected) < 1


@pytest.mark.unit
def test_cutoff_ts_zero_disables() -> None:
    from sitian.scanners import _compute_window_cutoff_ts

    assert _compute_window_cutoff_ts("2026-05-09T10:00:00Z", 0) is None


@pytest.mark.unit
def test_cutoff_ts_invalid_iso_warns(caplog: pytest.LogCaptureFixture) -> None:
    from sitian.scanners import _compute_window_cutoff_ts

    with caplog.at_level("WARNING", logger="sitian.scanners"):
        result = _compute_window_cutoff_ts("not-valid-iso", 2)
    assert result is None
    assert "invalid observed_at" in caplog.text
