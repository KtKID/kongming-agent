"""routers/whiteboard.py 单测。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from config_loader.models import Config
from tests.unit.test_web_app_lifespan import _seed_password
from web.app import create_app
from web.auth import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from web.whiteboard_store import whiteboard_card_path

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


class FakeTM:
    def __init__(self) -> None:
        self._started = False
        self._closed = False

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

    async def create_thread(self, name: str, preset_id: str) -> Any:
        raise NotImplementedError

    async def rename_thread(self, thread_id: str, new_name: str) -> Any:
        raise NotImplementedError

    async def delete_thread(self, thread_id: str, *, keep_history: bool = False) -> None:
        del thread_id, keep_history
        return None

    async def boot_or_attach(self, thread_id: str) -> Any:
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
        return None

    def list_threads(self) -> list[Any]:
        return []

    def list_cells(self) -> list[Any]:
        return []

    def get_cell(self, thread_id: str) -> Any:
        del thread_id
        return None

    def resolve_approval(self, thread_id: str, call_id: str, approved: bool) -> None:
        del thread_id, call_id, approved
        return None


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


def _login_client(tmp_path: Path, monkeypatch: Any) -> tuple[TestClient, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    home = tmp_path / "home"
    _seed_password(home, "pwd")

    app = create_app(_make_cfg(), FakeTM(), home_dir=home)
    client = TestClient(app)
    client.__enter__()
    resp = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert resp.status_code == 200
    return client, workspace


def test_get_whiteboard_empty(tmp_path: Path, monkeypatch: Any) -> None:
    client, _workspace = _login_client(tmp_path, monkeypatch)
    try:
        resp = client.get("/api/whiteboard")
        assert resp.status_code == 200
        assert resp.json() == {
            "title": "Whiteboard",
            "cards": [],
            "schema_version": 1,
        }
    finally:
        client.__exit__(None, None, None)


def test_create_card_and_get_snapshot(tmp_path: Path, monkeypatch: Any) -> None:
    client, _workspace = _login_client(tmp_path, monkeypatch)
    try:
        create_resp = client.post(
            "/api/whiteboard/cards",
            json={
                "title": "Todo",
                "category": "ops",
                "content": "- [ ] ship",
                "x": 80,
                "y": 100,
                "height": 340,
                "collapsed": False,
            },
            headers=CSRF_HEADERS,
        )
        assert create_resp.status_code == 201
        body = create_resp.json()
        assert body["title"] == "Todo"
        assert body["content"] == "- [ ] ship"

        get_resp = client.get("/api/whiteboard")
        assert get_resp.status_code == 200
        snapshot = get_resp.json()
        assert snapshot["title"] == "Whiteboard"
        assert len(snapshot["cards"]) == 1
        assert snapshot["cards"][0]["id"] == body["id"]
    finally:
        client.__exit__(None, None, None)


def test_put_card_updates_content(tmp_path: Path, monkeypatch: Any) -> None:
    client, _workspace = _login_client(tmp_path, monkeypatch)
    try:
        create_resp = client.post(
            "/api/whiteboard/cards",
            json={"title": "Draft", "content": "v1"},
            headers=CSRF_HEADERS,
        )
        created = create_resp.json()

        update_resp = client.put(
            f"/api/whiteboard/cards/{created['id']}",
            json={
                "content": "v2",
                "expected_updated_at": created["updated_at"],
                "title": "Final",
                "category": "done",
            },
            headers=CSRF_HEADERS,
        )
        assert update_resp.status_code == 200
        updated = update_resp.json()
        assert updated["content"] == "v2"
        assert updated["title"] == "Final"
        assert updated["category"] == "done"
    finally:
        client.__exit__(None, None, None)


def test_put_card_conflict_returns_409(tmp_path: Path, monkeypatch: Any) -> None:
    client, workspace = _login_client(tmp_path, monkeypatch)
    try:
        create_resp = client.post(
            "/api/whiteboard/cards",
            json={"title": "Conflict", "content": "v1"},
            headers=CSRF_HEADERS,
        )
        created = create_resp.json()

        time.sleep(0.02)
        whiteboard_card_path(workspace, created["filename"]).write_text(
            "edited outside api",
            encoding="utf-8",
        )

        update_resp = client.put(
            f"/api/whiteboard/cards/{created['id']}",
            json={
                "content": "v2",
                "expected_updated_at": created["updated_at"],
            },
            headers=CSRF_HEADERS,
        )
        assert update_resp.status_code == 409
        assert update_resp.json()["message"].startswith("whiteboard card content conflict")
    finally:
        client.__exit__(None, None, None)


def test_patch_layout_updates_board(tmp_path: Path, monkeypatch: Any) -> None:
    client, _workspace = _login_client(tmp_path, monkeypatch)
    try:
        create_resp = client.post(
            "/api/whiteboard/cards",
            json={"title": "Move Me", "content": "x"},
            headers=CSRF_HEADERS,
        )
        created = create_resp.json()

        patch_resp = client.patch(
            "/api/whiteboard/layout",
            json={
                "title": "Board A",
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
        assert snapshot["title"] == "Board A"
        assert snapshot["cards"][0]["x"] == 220
        assert snapshot["cards"][0]["y"] == 180
        assert snapshot["cards"][0]["height"] == 420
        assert snapshot["cards"][0]["collapsed"] is True
        assert snapshot["cards"][0]["z_index"] == 9
    finally:
        client.__exit__(None, None, None)


def test_delete_card_returns_204(tmp_path: Path, monkeypatch: Any) -> None:
    client, _workspace = _login_client(tmp_path, monkeypatch)
    try:
        create_resp = client.post(
            "/api/whiteboard/cards",
            json={"title": "Trash", "content": "x"},
            headers=CSRF_HEADERS,
        )
        created = create_resp.json()

        delete_resp = client.delete(
            f"/api/whiteboard/cards/{created['id']}",
            headers=CSRF_HEADERS,
        )
        assert delete_resp.status_code == 204

        get_resp = client.get("/api/whiteboard")
        assert get_resp.status_code == 200
        assert get_resp.json()["cards"] == []
    finally:
        client.__exit__(None, None, None)


def test_invalid_card_id_returns_422(tmp_path: Path, monkeypatch: Any) -> None:
    client, _workspace = _login_client(tmp_path, monkeypatch)
    try:
        resp = client.put(
            "/api/whiteboard/cards/not-a-card",
            json={"content": "x", "expected_updated_at": None},
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422
    finally:
        client.__exit__(None, None, None)
