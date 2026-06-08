"""Pytest for ``src/web/routers/claude.py`` projects 端点（v0.1 #12）。

覆盖三个 endpoint：

- ``GET /api/claude/projects``
- ``POST /api/claude/projects``
- ``DELETE /api/claude/projects?cwd=...``

worktree 隔离硬约束：用 ``tmp_path`` 当 ``kongming_home`` + ``claude_home``，
绝对不碰真实 ``~/.kongming`` / ``~/.claude``。``app.state.claude_home`` 由
``create_app`` 默认设为 ``Path.home()/.claude``，每条用例显式覆盖到 tmp_path 子目录
（与 :mod:`web.routers.claude` 通过 ``request.app.state.claude_home`` 读取的契约对齐）。

另外用 ``monkeypatch`` 把 ``web.app._bootstrap_projects_registry`` 替换为 no-op，
避免 lifespan 自动把当前 web server 进程的 ``_REPO_ROOT`` 写入 registry，污染
测试断言（registry 应在每条用例开始时为空）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hosts.web.app import create_app
from hosts.web.integrations.claude_code.jsonl_history import encode_cwd
from hosts.web.integrations.claude_code.projects_registry import (
    add_project,
    claude_projects_path,
    load_registry,
)
from tests.unit.test_web_app_lifespan import _seed_password
from tests.unit.test_web_routers_threads import (
    CSRF_HEADERS,
    FakeTM,
    _make_cfg,
)

# ---------------------------------------------------------------------------
# 测试装配 helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_bootstrap_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """禁用 lifespan 期间的 projects registry bootstrap。

    ``create_app`` 启动时会把 ``_REPO_ROOT``（真实的 kongming-agent 仓库根目录）
    自动登记到 claude / codex registry。本测试要做"registry 为空"等断言，
    必须把这步置空，否则每条用例都会被预登记一条 ``/Volumes/.../kongming-agent``。
    """
    monkeypatch.setattr(
        "hosts.web.app._bootstrap_projects_registry",
        lambda home, repo_root: None,
    )


def _login_client_with_isolated_claude_home(tmp_path: Path, tm: FakeTM) -> TestClient:
    """构造已登录 TestClient，并把 ``app.state.claude_home`` 重定向到 tmp_path。

    与 :func:`tests.unit.test_web_routers_threads._login_client` 的差异：
    本 helper 显式覆盖 ``app.state.claude_home`` 到 ``tmp_path / "claude_home"``，
    避免任何 endpoint 真去读用户家目录的 ``~/.claude/projects/``。
    """
    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    app = create_app(cfg, tm, home_dir=tmp_path)
    # 把 claude_home 重定向到 tmp_path 子目录，硬隔离
    isolated_claude_home = tmp_path / "claude_home"
    isolated_claude_home.mkdir(parents=True, exist_ok=True)
    app.state.claude_home = isolated_claude_home
    client = TestClient(app)
    client.__enter__()  # 进入 lifespan
    resp = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert resp.status_code == 200, f"login failed: {resp.text}"
    return client


def _claude_home(tmp_path: Path) -> Path:
    return tmp_path / "claude_home"


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


# ---------------------------------------------------------------------------
# GET /api/claude/projects
# ---------------------------------------------------------------------------


class TestGetProjects:
    def test_empty_registry_returns_empty_list(self, tmp_path: Path) -> None:
        tm = FakeTM()
        client = _login_client_with_isolated_claude_home(tmp_path, tm)
        try:
            resp = client.get("/api/claude/projects")
            assert resp.status_code == 200
            assert resp.json() == {"projects": []}
        finally:
            client.__exit__(None, None, None)

    def test_registry_has_cwd_with_no_jsonl_dir_returns_empty_sessions(
        self, tmp_path: Path
    ) -> None:
        """关键 D5 行为：registry 里的 cwd，但 ``~/.claude/projects/<encoded>/`` 不存在
        → 仍返回 project 节点，``sessions`` 为空列表。
        """
        # 先 add 一条 cwd 到 registry，但不在 claude_home 下创建对应目录
        add_project(tmp_path, "/proj/empty", alias="empty-alias")

        tm = FakeTM()
        client = _login_client_with_isolated_claude_home(tmp_path, tm)
        try:
            resp = client.get("/api/claude/projects")
            assert resp.status_code == 200
            body = resp.json()
            assert "projects" in body
            assert len(body["projects"]) == 1
            p = body["projects"][0]
            assert p["cwd"] == "/proj/empty"
            assert p["display_name"] == "empty"
            # encode_cwd("/proj/empty") = "-proj-empty"
            assert p["name"] == encode_cwd("/proj/empty")
            assert p["sessions"] == []
        finally:
            client.__exit__(None, None, None)

    def test_registry_has_cwd_with_jsonl_returns_session_summaries(self, tmp_path: Path) -> None:
        """registry 登记 cwd + ``<claude_home>/projects/<encoded>/`` 含 jsonl
        → sessions 填充。
        """
        cwd = "/proj/has-data"
        add_project(tmp_path, cwd)
        # 在 isolated claude_home 下铺真实 jsonl
        encoded = encode_cwd(cwd)
        proj_dir = _claude_home(tmp_path) / "projects" / encoded
        _write_jsonl(
            proj_dir / "sid-aaaaa.jsonl",
            [
                {
                    "type": "user",
                    "sessionId": "sid-aaaaa",
                    "message": {"role": "user", "content": "hello"},
                }
            ],
        )

        tm = FakeTM()
        client = _login_client_with_isolated_claude_home(tmp_path, tm)
        try:
            resp = client.get("/api/claude/projects")
            assert resp.status_code == 200
            body = resp.json()
            assert len(body["projects"]) == 1
            p = body["projects"][0]
            assert p["cwd"] == cwd
            sessions = p["sessions"]
            assert len(sessions) == 1
            s = sessions[0]
            assert s["claude_thread_id"] == "sid-aaaaa"
            assert s["title"] == "hello"
            assert s["message_count"] == 1
            assert "last_modified" in s
            assert s["is_pinned"] is False
        finally:
            client.__exit__(None, None, None)

    def test_unauthenticated_returns_401(self, tmp_path: Path) -> None:
        """缺 cookie 时 AuthMiddleware 拦截。"""
        _seed_password(tmp_path, "pwd")
        cfg = _make_cfg()
        tm = FakeTM()
        app = create_app(cfg, tm, home_dir=tmp_path)
        app.state.claude_home = tmp_path / "claude_home"
        with TestClient(app) as client:
            resp = client.get("/api/claude/projects")
            assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/claude/projects
# ---------------------------------------------------------------------------


class TestPostProject:
    def test_legal_cwd_returns_201_and_persists(self, tmp_path: Path) -> None:
        """传一个真实存在的目录，期望 201 + entry，registry 文件落盘。"""
        # 用 tmp_path 子目录当合法 cwd（is_dir() True）
        legal_cwd_path = tmp_path / "real_dir"
        legal_cwd_path.mkdir()
        legal_cwd = str(legal_cwd_path)

        tm = FakeTM()
        client = _login_client_with_isolated_claude_home(tmp_path, tm)
        try:
            resp = client.post(
                "/api/claude/projects",
                json={"cwd": legal_cwd, "alias": "myproj"},
                headers=CSRF_HEADERS,
            )
            assert resp.status_code == 201, resp.text
            body = resp.json()
            assert body["cwd"] == legal_cwd
            assert body["alias"] == "myproj"
            assert isinstance(body["added_at"], (int, float))
            # registry 落盘
            entries = load_registry(tmp_path)
            assert len(entries) == 1
            assert entries[0].cwd == legal_cwd
            assert entries[0].alias == "myproj"
            # 文件实存
            assert claude_projects_path(tmp_path).is_file()
        finally:
            client.__exit__(None, None, None)

    def test_nonexistent_path_returns_400(self, tmp_path: Path) -> None:
        """绝对路径但目录不存在 → 400 + detail 含 ``not a directory``。"""
        bogus = "/this/path/should/never/exist/xyz123"
        tm = FakeTM()
        client = _login_client_with_isolated_claude_home(tmp_path, tm)
        try:
            resp = client.post(
                "/api/claude/projects",
                json={"cwd": bogus, "alias": ""},
                headers=CSRF_HEADERS,
            )
            assert resp.status_code == 400
            detail = resp.json().get("detail", "")
            assert "not a directory" in detail
            # 不写入 registry
            assert load_registry(tmp_path) == []
        finally:
            client.__exit__(None, None, None)

    def test_relative_path_returns_422(self, tmp_path: Path) -> None:
        """相对路径（不以 ``/`` 开头）→ pydantic 校验失败 422。"""
        tm = FakeTM()
        client = _login_client_with_isolated_claude_home(tmp_path, tm)
        try:
            resp = client.post(
                "/api/claude/projects",
                json={"cwd": "relative/path", "alias": ""},
                headers=CSRF_HEADERS,
            )
            assert resp.status_code == 422
            assert load_registry(tmp_path) == []
        finally:
            client.__exit__(None, None, None)

    def test_empty_cwd_returns_422(self, tmp_path: Path) -> None:
        """空字符串 cwd → DTO ``min_length=1`` 校验失败 422。"""
        tm = FakeTM()
        client = _login_client_with_isolated_claude_home(tmp_path, tm)
        try:
            resp = client.post(
                "/api/claude/projects",
                json={"cwd": "", "alias": ""},
                headers=CSRF_HEADERS,
            )
            assert resp.status_code == 422
            assert load_registry(tmp_path) == []
        finally:
            client.__exit__(None, None, None)

    def test_idempotent_same_cwd_added_twice(self, tmp_path: Path) -> None:
        """同 cwd 二次 POST → 仍 201（幂等），registry 不重复。"""
        legal_cwd_path = tmp_path / "dir_idempotent"
        legal_cwd_path.mkdir()
        legal_cwd = str(legal_cwd_path)

        tm = FakeTM()
        client = _login_client_with_isolated_claude_home(tmp_path, tm)
        try:
            r1 = client.post(
                "/api/claude/projects",
                json={"cwd": legal_cwd, "alias": "first"},
                headers=CSRF_HEADERS,
            )
            assert r1.status_code == 201
            first_added_at = r1.json()["added_at"]

            r2 = client.post(
                "/api/claude/projects",
                json={"cwd": legal_cwd, "alias": "second"},
                headers=CSRF_HEADERS,
            )
            assert r2.status_code == 201
            body2 = r2.json()
            assert body2["cwd"] == legal_cwd
            # alias 被刷新
            assert body2["alias"] == "second"
            # added_at 保留首次
            assert body2["added_at"] == first_added_at

            entries = load_registry(tmp_path)
            assert len(entries) == 1
            assert entries[0].alias == "second"
        finally:
            client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# DELETE /api/claude/projects?cwd=...
# ---------------------------------------------------------------------------


class TestDeleteProject:
    def test_delete_unknown_cwd_returns_404(self, tmp_path: Path) -> None:
        tm = FakeTM()
        client = _login_client_with_isolated_claude_home(tmp_path, tm)
        try:
            resp = client.delete(
                "/api/claude/projects",
                params={"cwd": "/never/registered"},
                headers=CSRF_HEADERS,
            )
            assert resp.status_code == 404
            detail = resp.json().get("detail", "")
            assert "not in registry" in detail
        finally:
            client.__exit__(None, None, None)

    def test_delete_existing_cwd_returns_204_and_removes(self, tmp_path: Path) -> None:
        """删一条存在的 cwd → 204；后续 GET 不再包含该 project。"""
        legal_cwd_path = tmp_path / "dir_to_delete"
        legal_cwd_path.mkdir()
        legal_cwd = str(legal_cwd_path)
        # 先添加
        add_project(tmp_path, legal_cwd, alias="x")

        tm = FakeTM()
        client = _login_client_with_isolated_claude_home(tmp_path, tm)
        try:
            # 验证 GET 能看到
            r_before = client.get("/api/claude/projects")
            assert r_before.status_code == 200
            cwds_before = [p["cwd"] for p in r_before.json()["projects"]]
            assert legal_cwd in cwds_before

            # 删
            r_del = client.delete(
                "/api/claude/projects",
                params={"cwd": legal_cwd},
                headers=CSRF_HEADERS,
            )
            assert r_del.status_code == 204
            assert r_del.content == b""

            # registry 文件已清空
            assert load_registry(tmp_path) == []

            # GET 再查不到
            r_after = client.get("/api/claude/projects")
            assert r_after.status_code == 200
            cwds_after = [p["cwd"] for p in r_after.json()["projects"]]
            assert legal_cwd not in cwds_after
        finally:
            client.__exit__(None, None, None)
