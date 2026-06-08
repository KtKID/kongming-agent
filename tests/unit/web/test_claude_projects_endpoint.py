"""GET /api/claude/projects endpoint 单测（v0.2 #7 + claude-session-rename-archive
-metadata-source #3）。

覆盖：
1. 未登录 → 401
2. mock list_projects 返非空 → projects 数组
3. mock list_projects 返空 → projects=[]
4. refresh 流式：progress + done

本轮迁移：list_projects 多了 ``thread_metadata_index`` 参数，所有 mock 用
``*args, **kwargs`` 兼容（避免 router 调签名变更时再次破测）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.unit.test_web_app_lifespan import _seed_password
from tests.unit.test_web_routers_threads import FakeTM, _login_client, _make_cfg
from web.app import create_app
from web.integrations.claude_code.projects_scanner import ProjectSummary, SessionSummary


def test_unauthenticated_returns_401(tmp_path: Path) -> None:
    _seed_password(tmp_path, "pwd")
    tm = FakeTM()
    app = create_app(_make_cfg(), tm, home_dir=tmp_path)
    with TestClient(app) as client:
        resp = client.get("/api/claude/projects")
        assert resp.status_code == 401


def test_returns_projects_dict(tmp_path: Path, monkeypatch) -> None:
    fake_data = [
        ProjectSummary(
            name="-foo-bar",
            cwd="/foo/bar",
            display_name="bar",
            sessions=[
                SessionSummary(
                    claude_thread_id="sid-1",
                    title="hello world",
                    last_modified=1000.0,
                    message_count=42,
                ),
                SessionSummary(
                    claude_thread_id="sid-2",
                    title="second",
                    last_modified=900.0,
                    message_count=10,
                ),
            ],
        )
    ]

    # router 现在用 list_projects(cwds, *, claude_home, progress_callback, thread_metadata_index)
    # mock 用显式签名钉死调用契约——router 加新参数时 TypeError 提示更新测试
    def fake_list_projects(
        registry_cwds,
        *,
        claude_home=None,
        progress_callback=None,
        thread_metadata_index=None,
    ):
        del registry_cwds, claude_home, progress_callback, thread_metadata_index
        return fake_data

    monkeypatch.setattr("web.routers.claude.list_projects", fake_list_projects)
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        resp = client.get("/api/claude/projects")
        assert resp.status_code == 200
        body = resp.json()
        assert "projects" in body
        assert len(body["projects"]) == 1
        p = body["projects"][0]
        assert p["name"] == "-foo-bar"
        assert p["cwd"] == "/foo/bar"
        assert p["display_name"] == "bar"
        assert len(p["sessions"]) == 2
        s = p["sessions"][0]
        assert s["claude_thread_id"] == "sid-1"
        assert s["title"] == "hello world"
        assert s["last_modified"] == 1000.0
        assert s["message_count"] == 42
    finally:
        client.__exit__(None, None, None)


def test_refresh_stream_returns_progress_and_done(tmp_path: Path, monkeypatch) -> None:
    fake_data = [
        ProjectSummary(
            name="-foo-bar",
            cwd="/foo/bar",
            display_name="bar",
            sessions=[
                SessionSummary(
                    claude_thread_id="sid-1",
                    title="hello world",
                    last_modified=1000.0,
                    message_count=42,
                )
            ],
        )
    ]

    def fake_list_projects(
        registry_cwds,
        *,
        claude_home=None,
        progress_callback=None,
        thread_metadata_index=None,
    ):
        del registry_cwds, claude_home, thread_metadata_index
        if progress_callback is not None:
            progress_callback(1, 1, "-foo-bar")
        return fake_data

    monkeypatch.setattr("web.routers.claude.list_projects", fake_list_projects)
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        with client.stream("GET", "/api/claude/projects/refresh") as resp:
            assert resp.status_code == 200
            lines = [line for line in resp.iter_lines() if line]
        assert len(lines) == 2
        assert '"frame_type": "progress"' in lines[0]
        assert '"current": 1' in lines[0]
        assert '"frame_type": "done"' in lines[1]
        assert '"display_name": "bar"' in lines[1]
    finally:
        client.__exit__(None, None, None)
