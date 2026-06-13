"""diagrams 路由单元测试。

覆盖场景：
1. 双源列表（docs/ + dev-pipeline/tasks/）
2. 正常读取 HTML 内容
3. 空目录（无任何 HTML）
4. 白名单外路径拒绝（路径遍历 / 不在扫描结果中）
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from hosts.web.app import create_app
from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from infrastructure.config.models import Config
from tests.unit.test_web_app_lifespan import _seed_password

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}

_MINIMAL_HTML = """\
<!DOCTYPE html>
<html><head><title>{title}</title></head>
<body><p>test</p></body></html>
"""


class _FakeTM:
    """只满足 create_app 不报错的最小 ThreadManager 桩。"""

    _started = False
    _closed = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        self._started = True

    async def aclose_all(self) -> None:
        self._closed = True

    def list_threads(self) -> list:
        return []

    def list_cells(self) -> list:
        return []

    def get_cell(self, thread_id: str) -> None:
        return None


def _make_cfg() -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {"enabled": True, "dev_mode": True},
        }
    )


def _login_client(tmp_path: Path) -> TestClient:
    _seed_password(tmp_path, "pwd")
    app = create_app(_make_cfg(), _FakeTM(), home_dir=tmp_path)  # type: ignore[arg-type]
    app.state.workspace_root = tmp_path
    client = TestClient(app)
    client.__enter__()
    resp = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert resp.status_code == 200
    return client


# ---- helpers ----


def _seed_docs_diagram(workspace: Path, subdir: str, title: str) -> Path:
    """在 docs/<subdir>/ 下创建 architecture.html。"""
    d = workspace / "docs" / subdir
    d.mkdir(parents=True, exist_ok=True)
    html = d / "architecture.html"
    html.write_text(_MINIMAL_HTML.format(title=title), encoding="utf-8")
    return html


def _seed_task_diagram(workspace: Path, task_name: str, title: str) -> Path:
    """在 dev-pipeline/tasks/<task_name>/ 下创建 diagram.html。"""
    d = workspace / "dev-pipeline" / "tasks" / task_name
    d.mkdir(parents=True, exist_ok=True)
    html = d / "diagram.html"
    html.write_text(_MINIMAL_HTML.format(title=title + " · 模块依赖图"), encoding="utf-8")
    return html


# ---- tests ----


def test_list_diagrams_dual_source(tmp_path: Path) -> None:
    """双来源扫描应返回 docs + tasks 两组条目。"""
    _seed_docs_diagram(tmp_path, "v1-mini", "v1-mini 架构总览")
    _seed_task_diagram(tmp_path, "cron-v04", "cron-v0.4")
    client = _login_client(tmp_path)
    try:
        resp = client.get("/api/diagrams")
        assert resp.status_code == 200
        body = resp.json()
        diagrams = body["diagrams"]
        assert len(diagrams) == 2

        docs_items = [d for d in diagrams if d["source"] == "docs"]
        tasks_items = [d for d in diagrams if d["source"] == "tasks"]
        assert len(docs_items) == 1
        assert len(tasks_items) == 1

        assert docs_items[0]["title"] == "v1-mini 架构总览"
        assert docs_items[0]["path"] == "docs/v1-mini/architecture.html"

        # title 后缀 " · 模块依赖图" 应被去掉
        assert tasks_items[0]["title"] == "cron-v0.4"
    finally:
        client.__exit__(None, None, None)


def test_list_diagrams_empty(tmp_path: Path) -> None:
    """无 HTML 文件时返回空列表。"""
    client = _login_client(tmp_path)
    try:
        resp = client.get("/api/diagrams")
        assert resp.status_code == 200
        assert resp.json()["diagrams"] == []
    finally:
        client.__exit__(None, None, None)


def test_get_content_success(tmp_path: Path) -> None:
    """正常读取 HTML 内容。"""
    _seed_docs_diagram(tmp_path, "stream-v01", "Stream Progress")
    client = _login_client(tmp_path)
    try:
        resp = client.get(
            "/api/diagrams/content",
            params={"path": "docs/stream-v01/architecture.html"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "<title>Stream Progress</title>" in resp.text
    finally:
        client.__exit__(None, None, None)


def test_get_content_path_traversal_rejected(tmp_path: Path) -> None:
    """路径遍历（../）应返回 404。"""
    _seed_docs_diagram(tmp_path, "legit", "Legit")
    client = _login_client(tmp_path)
    try:
        resp = client.get(
            "/api/diagrams/content",
            params={"path": "../../../etc/passwd"},
        )
        assert resp.status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_get_content_not_in_whitelist_rejected(tmp_path: Path) -> None:
    """非白名单路径（不在扫描结果中）应返回 404。"""
    # 创建一个 HTML 但文件名不匹配（非 architecture*/diagram）
    rogue = tmp_path / "docs" / "paper-reader" / "meta.html"
    rogue.parent.mkdir(parents=True, exist_ok=True)
    rogue.write_text("<html>rogue</html>", encoding="utf-8")

    client = _login_client(tmp_path)
    try:
        resp = client.get(
            "/api/diagrams/content",
            params={"path": "docs/paper-reader/meta.html"},
        )
        assert resp.status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_list_excludes_non_architecture_html(tmp_path: Path) -> None:
    """docs/ 下非 architecture*.html 文件应被排除。"""
    # architecture.html — 应被收集
    _seed_docs_diagram(tmp_path, "valid", "Valid")
    # random.html — 应被排除
    other = tmp_path / "docs" / "valid" / "random.html"
    other.write_text("<html>random</html>", encoding="utf-8")

    client = _login_client(tmp_path)
    try:
        resp = client.get("/api/diagrams")
        diagrams = resp.json()["diagrams"]
        assert len(diagrams) == 1
        assert diagrams[0]["title"] == "Valid"
    finally:
        client.__exit__(None, None, None)
