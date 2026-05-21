"""Pytest for ``src/web/routers/codex.py`` projects 端点（v0.1 #13）。

覆盖三个 endpoint：

- ``GET /api/codex/projects``
- ``POST /api/codex/projects``
- ``DELETE /api/codex/projects?cwd=...``

worktree 隔离硬约束：用 ``tmp_path`` 当 ``kongming_home`` + ``codex_home``，
绝对不碰真实 ``~/.kongming`` / ``~/.codex``。``app.state.codex_home`` 由
``create_app`` **不**默认注入（与 claude_home 不同），本测试显式覆盖到 tmp_path
子目录（与 :mod:`web.routers.codex` 通过
``getattr(request.app.state, "codex_home", None)`` 读取的契约对齐）。

另外用 ``monkeypatch`` 把 ``web.app._bootstrap_projects_registry`` 替换为 no-op，
避免 lifespan 自动把当前 web server 进程的 ``_REPO_ROOT`` 写入 registry，污染
测试断言（registry 应在每条用例开始时为空）。

与 ``test_claude_router_projects.py`` 的差异：

1. registry 模块换 ``web.codex.projects_registry`` + 路径字段 ``codex_projects.json``
2. POST/DELETE 路径前缀 ``/api/codex/projects``
3. **codex jsonl 结构不同**：codex 不按 cwd 编码目录名，而是
   ``sessions/<Y>/<M>/<D>/rollout-<ISO>-<UUID>.jsonl``，cwd 真值在 jsonl 第一行
   ``session_meta.payload.cwd`` 字段里。所以 GET 用例造数据时按这套结构铺，
   不存在 "encode_cwd" 一说。
4. **session 字段差异**：codex session summary 多 ``cli_version`` /
   ``rollout_path`` / ``provider`` 等字段，本测试的 GET 用例不深入比对，
   只断言 sessions 长度 + session_id + cwd 即可。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.unit.test_web_app_lifespan import _seed_password
from tests.unit.test_web_routers_threads import (
    CSRF_HEADERS,
    FakeTM,
    _make_cfg,
)
from web.app import create_app
from web.codex.projects_registry import (
    add_project,
    codex_projects_path,
    load_registry,
)

# ---------------------------------------------------------------------------
# 测试装配 helpers
# ---------------------------------------------------------------------------

_UUID_A = str(uuid.UUID("019c429a-b3e2-7f00-a1d2-e4f5a6b7c8d9"))


@pytest.fixture(autouse=True)
def _disable_bootstrap_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """禁用 lifespan 期间的 projects registry bootstrap。

    ``create_app`` 启动时会把 ``_REPO_ROOT``（真实的 kongming-agent 仓库根目录）
    自动登记到 claude / codex registry。本测试要做"registry 为空"等断言，
    必须把这步置空，否则每条用例都会被预登记一条 ``/Volumes/.../kongming-agent``。
    """
    monkeypatch.setattr(
        "web.app._bootstrap_projects_registry",
        lambda home, repo_root: None,
    )


def _login_client_with_isolated_codex_home(tmp_path: Path, tm: FakeTM) -> TestClient:
    """构造已登录 TestClient，并把 ``app.state.codex_home`` 重定向到 tmp_path。

    与 :func:`tests.unit.test_web_routers_threads._login_client` 的差异：
    本 helper 显式 *新增* ``app.state.codex_home`` 到 ``tmp_path / "codex_home"``
    （``create_app`` 默认不设此 attr，路由层用 ``getattr`` fallback 到
    ``~/.codex``，必须在测试里覆盖避免读用户家目录）。
    """
    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    app = create_app(cfg, tm, home_dir=tmp_path)
    # 显式注入 codex_home 到 tmp_path 子目录，硬隔离
    isolated_codex_home = tmp_path / "codex_home"
    isolated_codex_home.mkdir(parents=True, exist_ok=True)
    app.state.codex_home = isolated_codex_home
    client = TestClient(app)
    client.__enter__()  # 进入 lifespan
    resp = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert resp.status_code == 200, f"login failed: {resp.text}"
    return client


def _codex_home(tmp_path: Path) -> Path:
    return tmp_path / "codex_home"


def _rollout_filename(session_id: str) -> str:
    return f"rollout-2026-05-10T14-00-00-{session_id}.jsonl"


def _write_rollout(
    rollout_path: Path,
    session_id: str,
    cwd: str,
    extra_lines: int = 2,
) -> None:
    """写一个最小合法 codex rollout 文件。

    第一行 ``session_meta``（payload 含 id / cwd / cli_version），
    后续 ``extra_lines`` 条占位 event_msg。
    """
    rollout_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "timestamp": "2026-05-10T14:00:00.000Z",
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "cwd": cwd,
                    "originator": "codex_exec",
                    "cli_version": "0.128.0",
                },
            }
        )
    ]
    for i in range(extra_lines):
        lines.append(
            json.dumps(
                {
                    "timestamp": f"2026-05-10T14:00:0{i + 1}.000Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "text": f"msg {i}"},
                }
            )
        )
    rollout_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# GET /api/codex/projects
# ---------------------------------------------------------------------------


class TestGetProjects:
    def test_registry_empty_but_codex_home_has_jsonl_returns_empty(self, tmp_path: Path) -> None:
        """registry 空 → 即使 sessions/ 目录里有 jsonl 也不返回任何 project。

        与 claude 行为一致：scanner 用 ``set(registry_cwds)`` 过滤所有 jsonl，
        未登记的 cwd 一律丢弃（D1 决议）。
        """
        # 在 codex_home 下铺一个真实 rollout，cwd 指向 /proj/unregistered
        unregistered_cwd = tmp_path / "unregistered"
        unregistered_cwd.mkdir()
        rollout = (
            _codex_home(tmp_path)
            / "sessions"
            / "2026"
            / "05"
            / "10"
            / _rollout_filename(_UUID_A)
        )
        _write_rollout(rollout, _UUID_A, str(unregistered_cwd))

        tm = FakeTM()
        client = _login_client_with_isolated_codex_home(tmp_path, tm)
        try:
            resp = client.get("/api/codex/projects")
            assert resp.status_code == 200
            assert resp.json() == []
        finally:
            client.__exit__(None, None, None)

    def test_registry_has_cwd_with_no_jsonl_returns_empty_sessions(self, tmp_path: Path) -> None:
        """关键 D5 行为：registry 里的 cwd，但 sessions/ 下找不到匹配 cwd 的 rollout
        → 仍返回 project 节点，``sessions`` 为空列表。
        """
        # 先 add 一条 cwd 到 registry，但不在 codex_home 下创建对应 rollout
        empty_cwd = str(tmp_path / "proj-empty")
        add_project(tmp_path, empty_cwd, alias="empty-alias")

        tm = FakeTM()
        client = _login_client_with_isolated_codex_home(tmp_path, tm)
        try:
            resp = client.get("/api/codex/projects")
            assert resp.status_code == 200
            body = resp.json()
            # 端点直接返回 list（不像 claude 包了一层 {"projects": [...]}）
            assert isinstance(body, list)
            assert len(body) == 1
            p = body[0]
            assert p["cwd"] == str(Path(empty_cwd).resolve())
            assert p["display_name"] == Path(empty_cwd).name
            assert p["sessions"] == []
        finally:
            client.__exit__(None, None, None)

    def test_registry_has_cwd_with_jsonl_returns_session_summaries(self, tmp_path: Path) -> None:
        """registry 登记 cwd + ``codex_home/sessions/<Y>/<M>/<D>/rollout-*.jsonl``
        含匹配 cwd → sessions 填充。

        codex 的 cwd 真值不在目录名（不像 claude），而在 jsonl 第一行
        ``session_meta.payload.cwd``——本用例用 ``tmp_path`` 子目录当 cwd，
        因为 scanner 走 ``os.path.realpath()`` 归一，必须是真实存在的路径
        才能稳定 match（macOS 下 ``/tmp`` 会被归一到 ``/private/tmp``）。
        """
        # 用 tmp_path 子目录当 cwd（realpath 归一后稳定）
        cwd_path = tmp_path / "real_proj_with_data"
        cwd_path.mkdir()
        cwd = str(cwd_path)
        add_project(tmp_path, cwd)

        # 在 isolated codex_home 下铺真实 rollout，session_meta.cwd 指向上面的 cwd
        rollout = (
            _codex_home(tmp_path)
            / "sessions"
            / "2026"
            / "05"
            / "10"
            / _rollout_filename(_UUID_A)
        )
        _write_rollout(rollout, _UUID_A, cwd, extra_lines=2)

        tm = FakeTM()
        client = _login_client_with_isolated_codex_home(tmp_path, tm)
        try:
            resp = client.get("/api/codex/projects")
            assert resp.status_code == 200
            body = resp.json()
            assert isinstance(body, list)
            assert len(body) == 1
            p = body[0]
            # scanner 走 realpath 归一；macOS 下 cwd 可能从 /var/... 归一到 /private/var/...
            # 这里只断言 sessions 至少有一条 + session_id 对得上即可
            sessions = p["sessions"]
            assert len(sessions) >= 1
            s = sessions[0]
            assert s["session_id"] == _UUID_A
            assert s["is_pinned"] is False
            # codex 特有字段存在（不深入比对值）
            assert "cli_version" in s
            assert "rollout_path" in s
            assert s["provider"] == "codex"
        finally:
            client.__exit__(None, None, None)

    def test_unauthenticated_returns_401(self, tmp_path: Path) -> None:
        """缺 cookie 时 AuthMiddleware 拦截。"""
        _seed_password(tmp_path, "pwd")
        cfg = _make_cfg()
        tm = FakeTM()
        app = create_app(cfg, tm, home_dir=tmp_path)
        app.state.codex_home = tmp_path / "codex_home"
        with TestClient(app) as client:
            resp = client.get("/api/codex/projects")
            assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/codex/projects
# ---------------------------------------------------------------------------


class TestPostProject:
    def test_legal_cwd_returns_201_and_persists(self, tmp_path: Path) -> None:
        """传一个真实存在的目录，期望 201 + entry，registry 文件落盘。"""
        legal_cwd_path = tmp_path / "real_dir"
        legal_cwd_path.mkdir()
        legal_cwd = str(legal_cwd_path)

        tm = FakeTM()
        client = _login_client_with_isolated_codex_home(tmp_path, tm)
        try:
            resp = client.post(
                "/api/codex/projects",
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
            assert codex_projects_path(tmp_path).is_file()
        finally:
            client.__exit__(None, None, None)

    def test_nonexistent_path_returns_400(self, tmp_path: Path) -> None:
        """绝对路径但目录不存在 → 400 + detail 含 ``not a directory``。"""
        bogus = "/this/path/should/never/exist/xyz123"
        tm = FakeTM()
        client = _login_client_with_isolated_codex_home(tmp_path, tm)
        try:
            resp = client.post(
                "/api/codex/projects",
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
        client = _login_client_with_isolated_codex_home(tmp_path, tm)
        try:
            resp = client.post(
                "/api/codex/projects",
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
        client = _login_client_with_isolated_codex_home(tmp_path, tm)
        try:
            resp = client.post(
                "/api/codex/projects",
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
        client = _login_client_with_isolated_codex_home(tmp_path, tm)
        try:
            r1 = client.post(
                "/api/codex/projects",
                json={"cwd": legal_cwd, "alias": "first"},
                headers=CSRF_HEADERS,
            )
            assert r1.status_code == 201
            first_added_at = r1.json()["added_at"]

            r2 = client.post(
                "/api/codex/projects",
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
# DELETE /api/codex/projects?cwd=...
# ---------------------------------------------------------------------------


class TestDeleteProject:
    def test_delete_unknown_cwd_returns_404(self, tmp_path: Path) -> None:
        tm = FakeTM()
        client = _login_client_with_isolated_codex_home(tmp_path, tm)
        try:
            resp = client.delete(
                "/api/codex/projects",
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
        client = _login_client_with_isolated_codex_home(tmp_path, tm)
        try:
            # 验证 GET 能看到
            r_before = client.get("/api/codex/projects")
            assert r_before.status_code == 200
            cwds_before = [p["cwd"] for p in r_before.json()]
            assert legal_cwd in cwds_before

            # 删
            r_del = client.delete(
                "/api/codex/projects",
                params={"cwd": legal_cwd},
                headers=CSRF_HEADERS,
            )
            assert r_del.status_code == 204
            assert r_del.content == b""

            # registry 文件已清空
            assert load_registry(tmp_path) == []

            # GET 再查不到
            r_after = client.get("/api/codex/projects")
            assert r_after.status_code == 200
            cwds_after = [p["cwd"] for p in r_after.json()]
            assert legal_cwd not in cwds_after
        finally:
            client.__exit__(None, None, None)
