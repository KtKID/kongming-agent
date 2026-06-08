"""routers/whiteboard.py 单测（thread-scoped 双 scope 版）。

覆盖 docs/whiteboard-scope/05-validation-and-evolution.md 中的 R1~R7：

- R1：GET 不存在的 thread → 404
- R2：GET 有 cwd 的 thread → 200，含 global_title + project_title
- R3：GET cwd 为空的 thread → 200，project_title=None，只含 global cards
- R4：POST scope=project + cwd="" → 422
- R5：POST scope=global + cwd="" → 201 写到 global
- R6：PATCH layout scope=project + cwd="" → 422
- R7：DELETE 不存在的 card → 404

辅以 happy-path：
- POST scope=project（cwd 非空）→ 201
- PATCH layout scope=project（cwd 非空）→ 200
- DELETE 已存在的 card → 204
- card_id 非法 → 422

设计要点：
- 用真实 :class:`WhiteboardManager`（``create_app`` 自动装配，根为 ``home / "whiteboard"``），
  接近集成测试；whiteboard_root 完全落在 tmp_path 下，无跨用例污染。
- ``FakeTM`` 实现 ``ThreadManagerProtocol`` 最小子集；``list_threads()`` 返回
  内置字典，构造时显式塞入 thread + cwd（router 的 ``_require_thread_meta``
  通过遍历 ``list_threads()`` 找 thread）。
- thread_id 用 ``thread-<12 hex>`` 格式（router 入口先 ``_validate_thread_id``）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Literal

from fastapi.testclient import TestClient

from config_loader.models import Config
from tests.unit.test_web_app_lifespan import _seed_password
from web.app import create_app
from web.auth import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from web.threads.metadata import ThreadMetadata
from web.whiteboard.manager import encode_project_dir

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}

THREAD_WITH_CWD = "thread-aaaaaaaaaaa1"
THREAD_NO_CWD = "thread-bbbbbbbbbbb2"


class FakeTM:
    """支持 list_threads 查 thread + cwd 的最小 ThreadManager 实现。"""

    def __init__(self, threads: list[ThreadMetadata] | None = None) -> None:
        self._threads: dict[str, ThreadMetadata] = {meta.id: meta for meta in (threads or [])}
        self._started = False
        self._closed = False

    # lifecycle ----------------------------------------------------------
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

    # CRUD（whiteboard 路由不消费这些，给 Protocol 充门面） -------------
    async def create_thread(
        self,
        name: str,
        preset_id: str = "",
        *,
        backend_kind: Literal["generic_chat", "claude_code", "codex"] = "generic_chat",
        cwd: str = "",
    ) -> ThreadMetadata:
        del name, preset_id, backend_kind, cwd
        raise NotImplementedError

    async def rename_thread(self, thread_id: str, new_name: str) -> ThreadMetadata:
        del thread_id, new_name
        raise NotImplementedError

    async def pin_thread(self, thread_id: str, is_pinned: bool) -> ThreadMetadata:
        del thread_id, is_pinned
        raise NotImplementedError

    async def delete_thread(self, thread_id: str, *, keep_history: bool = False) -> None:
        del thread_id, keep_history

    async def boot_or_attach(self, thread_id: str) -> Any:
        del thread_id
        raise NotImplementedError

    async def evict_cell(
        self,
        thread_id: str,
        reason: str,
        message: str | None = None,
        *,
        notify_ws: bool = True,
    ) -> None:
        del thread_id, reason, message, notify_ws

    # 查询 ---------------------------------------------------------------
    def list_threads(self) -> list[ThreadMetadata]:
        return list(self._threads.values())

    def list_cells(self) -> list[Any]:
        return []

    def get_cell(self, thread_id: str) -> Any:
        del thread_id
        return None

    def find_thread_by_claude_thread_id(self, claude_thread_id: str) -> Any:
        del claude_thread_id
        return None

    def find_thread_by_codex_thread_id(self, codex_thread_id: str) -> Any:
        del codex_thread_id
        return None

    async def bind_claude_thread(
        self,
        thread_id: str,
        claude_thread_id: str,
        cwd: str,
    ) -> Any:
        del thread_id, claude_thread_id, cwd
        raise NotImplementedError

    async def bind_codex_thread(
        self,
        thread_id: str,
        codex_thread_id: str,
        cwd: str,
    ) -> Any:
        del thread_id, codex_thread_id, cwd
        raise NotImplementedError

    @property
    def usage_manager(self) -> Any:
        # whiteboard 路由不消费 usage manager；返回最小占位
        return None

    def resolve_approval(self, thread_id: str, call_id: str, approved: bool) -> None:
        del thread_id, call_id, approved


def _make_cfg() -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {
                "enabled": True,
                "dev_mode": True,
            },
        }
    )


def _make_meta(thread_id: str, cwd: str) -> ThreadMetadata:
    return ThreadMetadata(
        id=thread_id,
        name="t",
        preset_id="p",
        cwd=cwd,
        created_at=1.0,
        updated_at=2.0,
        message_count=0,
    )


def _login_client(
    tmp_path: Path,
    threads: list[ThreadMetadata],
) -> tuple[TestClient, Path]:
    """启动 app + 登录；返回 (client, kongming_home)。

    kongming_home = tmp_path / "home"，whiteboard 根落在 ``home/whiteboard/``。
    """
    home = tmp_path / "home"
    _seed_password(home, "pwd")
    cfg = _make_cfg()
    tm = FakeTM(threads=threads)
    app = create_app(cfg, tm, home_dir=home)
    client = TestClient(app)
    client.__enter__()
    resp = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert resp.status_code == 200
    return client, home


# ---------------------------------------------------------------------------
# R1：GET 不存在的 thread → 404
# ---------------------------------------------------------------------------
def test_R1_get_whiteboard_thread_not_found_returns_404(tmp_path: Path) -> None:
    client, _home = _login_client(tmp_path, threads=[])
    try:
        resp = client.get(f"/api/threads/{THREAD_WITH_CWD}/whiteboard")
        assert resp.status_code == 404
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# R2：GET 有 cwd → 200，完整 DTO（global_title + project_title 同时存在）
# ---------------------------------------------------------------------------
def test_R2_get_whiteboard_with_cwd_returns_full_dto(tmp_path: Path) -> None:
    project_cwd = str(tmp_path / "project_a")
    threads = [_make_meta(THREAD_WITH_CWD, project_cwd)]
    client, _home = _login_client(tmp_path, threads=threads)
    try:
        # 先在 project workspace 落一张卡片，确保 project_title 非 None
        create_resp = client.post(
            f"/api/threads/{THREAD_WITH_CWD}/whiteboard/cards",
            json={"scope": "project", "title": "Pcard", "content": "p"},
            headers=CSRF_HEADERS,
        )
        assert create_resp.status_code == 201

        resp = client.get(f"/api/threads/{THREAD_WITH_CWD}/whiteboard")
        assert resp.status_code == 200
        body = resp.json()
        assert body["global_title"] == "Whiteboard"
        # project 卡已创建 → project_title 非 None
        assert body["project_title"] is not None
        assert len(body["cards"]) == 1
        assert body["cards"][0]["scope"] == "project"
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# R3：GET cwd="" → 200，project_title=None，只含 global cards
# ---------------------------------------------------------------------------
def test_R3_get_whiteboard_empty_cwd_only_global(tmp_path: Path) -> None:
    threads = [_make_meta(THREAD_NO_CWD, "")]
    client, _home = _login_client(tmp_path, threads=threads)
    try:
        # 落一张 global 卡
        client.post(
            f"/api/threads/{THREAD_NO_CWD}/whiteboard/cards",
            json={"scope": "global", "title": "Gcard", "content": "g"},
            headers=CSRF_HEADERS,
        )

        resp = client.get(f"/api/threads/{THREAD_NO_CWD}/whiteboard")
        assert resp.status_code == 200
        body = resp.json()
        assert body["global_title"] == "Whiteboard"
        assert body["project_title"] is None
        assert len(body["cards"]) == 1
        assert body["cards"][0]["scope"] == "global"
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# R4：POST scope=project + cwd="" → 422
# ---------------------------------------------------------------------------
def test_R4_post_card_project_scope_with_empty_cwd_returns_422(tmp_path: Path) -> None:
    threads = [_make_meta(THREAD_NO_CWD, "")]
    client, _home = _login_client(tmp_path, threads=threads)
    try:
        resp = client.post(
            f"/api/threads/{THREAD_NO_CWD}/whiteboard/cards",
            json={"scope": "project", "title": "Try", "content": "x"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# R5：POST scope=global + cwd="" → 201 写到 global
# ---------------------------------------------------------------------------
def test_R5_post_card_global_scope_with_empty_cwd_succeeds(tmp_path: Path) -> None:
    threads = [_make_meta(THREAD_NO_CWD, "")]
    client, home = _login_client(tmp_path, threads=threads)
    try:
        resp = client.post(
            f"/api/threads/{THREAD_NO_CWD}/whiteboard/cards",
            json={"scope": "global", "title": "Gcard", "content": "g"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["scope"] == "global"
        # 卡片确实落在 global workspace（board.json 与 cards/ 直接在 workspace 根）
        global_board = home / "whiteboard" / "global" / "board.json"
        assert global_board.exists()
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# R6：PATCH layout scope=project + cwd="" → 422
# ---------------------------------------------------------------------------
def test_R6_patch_layout_project_scope_with_empty_cwd_returns_422(tmp_path: Path) -> None:
    threads = [_make_meta(THREAD_NO_CWD, "")]
    client, _home = _login_client(tmp_path, threads=threads)
    try:
        resp = client.patch(
            f"/api/threads/{THREAD_NO_CWD}/whiteboard/layout",
            json={"scope": "project", "title": "x", "cards": []},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# R7：DELETE 不存在的 card → 404
# ---------------------------------------------------------------------------
def test_R7_delete_nonexistent_card_returns_404(tmp_path: Path) -> None:
    threads = [_make_meta(THREAD_WITH_CWD, str(tmp_path / "p"))]
    client, _home = _login_client(tmp_path, threads=threads)
    try:
        # card_id 形如合法但不存在
        resp = client.delete(
            f"/api/threads/{THREAD_WITH_CWD}/whiteboard/cards/card-deadbeef0001",
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 404
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Happy path: POST scope=project（cwd 非空）→ 201 写到 projects/<encoded>/
# ---------------------------------------------------------------------------
def test_post_project_card_with_cwd_writes_to_project_workspace(tmp_path: Path) -> None:
    project_cwd = str(tmp_path / "project_b")
    threads = [_make_meta(THREAD_WITH_CWD, project_cwd)]
    client, home = _login_client(tmp_path, threads=threads)
    try:
        resp = client.post(
            f"/api/threads/{THREAD_WITH_CWD}/whiteboard/cards",
            json={"scope": "project", "title": "Pcard", "content": "p"},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 201
        assert resp.json()["scope"] == "project"
        encoded = encode_project_dir(project_cwd)
        proj_root = home / "whiteboard" / "projects" / encoded
        # project workspace 是 ``<root>/projects/<encoded>/``，board.json 直接在 workspace 根
        assert (proj_root / "board.json").exists()
        # 首次写 project 时 manager 落 meta.json 记录原始 cwd（在 project workspace 根）
        assert (proj_root / "meta.json").exists()
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Happy path: PATCH layout scope=project（cwd 非空）→ 200
# ---------------------------------------------------------------------------
def test_patch_layout_project_scope_happy_path(tmp_path: Path) -> None:
    project_cwd = str(tmp_path / "project_c")
    threads = [_make_meta(THREAD_WITH_CWD, project_cwd)]
    client, _home = _login_client(tmp_path, threads=threads)
    try:
        create_resp = client.post(
            f"/api/threads/{THREAD_WITH_CWD}/whiteboard/cards",
            json={"scope": "project", "title": "Move me", "content": "x"},
            headers=CSRF_HEADERS,
        )
        assert create_resp.status_code == 201
        created = create_resp.json()

        patch_resp = client.patch(
            f"/api/threads/{THREAD_WITH_CWD}/whiteboard/layout",
            json={
                "scope": "project",
                "title": "Project Board",
                "cards": [
                    {
                        "id": created["id"],
                        "x": 220,
                        "y": 180,
                        "height": 420,
                        "collapsed": True,
                        "z_index": 9,
                    }
                ],
            },
            headers=CSRF_HEADERS,
        )
        assert patch_resp.status_code == 200
        snapshot = patch_resp.json()
        assert snapshot["project_title"] == "Project Board"
        assert snapshot["cards"][0]["x"] == 220
        assert snapshot["cards"][0]["z_index"] == 9
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Happy path: DELETE 已存在的卡片 → 204，再 GET 看不到
# ---------------------------------------------------------------------------
def test_delete_card_happy_path_returns_204(tmp_path: Path) -> None:
    project_cwd = str(tmp_path / "project_d")
    threads = [_make_meta(THREAD_WITH_CWD, project_cwd)]
    client, _home = _login_client(tmp_path, threads=threads)
    try:
        create_resp = client.post(
            f"/api/threads/{THREAD_WITH_CWD}/whiteboard/cards",
            json={"scope": "project", "title": "Trash", "content": "x"},
            headers=CSRF_HEADERS,
        )
        assert create_resp.status_code == 201
        created = create_resp.json()

        del_resp = client.delete(
            f"/api/threads/{THREAD_WITH_CWD}/whiteboard/cards/{created['id']}",
            headers=CSRF_HEADERS,
        )
        assert del_resp.status_code == 204

        get_resp = client.get(f"/api/threads/{THREAD_WITH_CWD}/whiteboard")
        assert get_resp.status_code == 200
        assert get_resp.json()["cards"] == []
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Happy path: PUT 卡片内容并冲突
# ---------------------------------------------------------------------------
def test_put_card_content_conflict_returns_409(tmp_path: Path) -> None:
    project_cwd = str(tmp_path / "project_e")
    threads = [_make_meta(THREAD_WITH_CWD, project_cwd)]
    client, home = _login_client(tmp_path, threads=threads)
    try:
        create_resp = client.post(
            f"/api/threads/{THREAD_WITH_CWD}/whiteboard/cards",
            json={"scope": "project", "title": "C", "content": "v1"},
            headers=CSRF_HEADERS,
        )
        assert create_resp.status_code == 201
        created = create_resp.json()

        # 通过外部直接改文件触发冲突（mtime 变化让 expected_updated_at 失配）
        time.sleep(0.02)
        encoded = encode_project_dir(project_cwd)
        proj_root = home / "whiteboard" / "projects" / encoded
        card_path = proj_root / "cards" / created["filename"]
        card_path.write_text("edited outside api", encoding="utf-8")

        put_resp = client.put(
            f"/api/threads/{THREAD_WITH_CWD}/whiteboard/cards/{created['id']}",
            json={
                "content": "v2",
                "expected_updated_at": created["updated_at"],
            },
            headers=CSRF_HEADERS,
        )
        assert put_resp.status_code == 409
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 非法 card_id → 422
# ---------------------------------------------------------------------------
def test_invalid_card_id_returns_422(tmp_path: Path) -> None:
    threads = [_make_meta(THREAD_WITH_CWD, str(tmp_path / "px"))]
    client, _home = _login_client(tmp_path, threads=threads)
    try:
        resp = client.put(
            f"/api/threads/{THREAD_WITH_CWD}/whiteboard/cards/not-a-card",
            json={"content": "x", "expected_updated_at": None},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 非法 thread_id 格式 → 422（router 入口 _validate_thread_id 拦下）
# ---------------------------------------------------------------------------
def test_invalid_thread_id_returns_422(tmp_path: Path) -> None:
    client, _home = _login_client(tmp_path, threads=[])
    try:
        resp = client.get("/api/threads/bad-thread-id/whiteboard")
        assert resp.status_code == 422
    finally:
        client.__exit__(None, None, None)
