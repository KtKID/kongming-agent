"""smart-approval-v1 route 集成测：toggle / query / 连接初始 push state。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from config_loader.models import Config
from tests.unit.test_web_app_lifespan import FakeThreadManager, _seed_password
from web.app import create_app
from web.auth import CSRF_HEADER_NAME, CSRF_HEADER_VALUE

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


def _make_cfg() -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {"enabled": True, "dev_mode": True},
        },
    )


@pytest.fixture()
def app_client(tmp_path: Path) -> Iterator[TestClient]:
    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    tm = FakeThreadManager()
    app = create_app(cfg, tm, home_dir=tmp_path)
    client = TestClient(app)
    client.__enter__()
    # 登录
    r = client.post("/api/auth/login", json={"password": "pwd"}, headers=CSRF_HEADERS)
    assert r.status_code == 200
    try:
        yield client
    finally:
        client.__exit__(None, None, None)


# ---------- 装配验证：app.state 真的挂上了 policy / audit ----------


def test_app_state_has_auto_approval(app_client: TestClient) -> None:
    app = app_client.app
    assert hasattr(app.state, "auto_approval_policy")
    assert hasattr(app.state, "auto_approval_audit")
    assert app.state.auto_approval_policy is not None
    assert app.state.auto_approval_audit is not None


# ---------- toggle 路由 ----------


def test_auto_approval_toggle_persists(app_client: TestClient, tmp_path: Path) -> None:
    """toggle → 后端写盘 → 回 state 帧。"""
    with app_client.websocket_connect("/ws/claude-code") as ws:
        ws.send_json(
            {
                "type": "auto-approval-toggle",
                "cwd": "/proj/test",
                "enabled": True,
            },
        )
        msg = _wait_for_kind(ws, "auto_approval_state")
        assert msg["channel"] == "claude_code"
        assert msg["cwd"] == "/proj/test"
        assert msg["enabled"] is True
        assert msg["timeoutMs"] == 10000  # default
        assert msg["ruleOverrides"] == {}

    # 持久化验证：app.state.policy 应能再读出
    policy = app_client.app.state.auto_approval_policy
    cfg = policy.get_config("/proj/test")
    assert cfg.enabled is True


def test_auto_approval_toggle_off_persists(app_client: TestClient) -> None:
    with app_client.websocket_connect("/ws/claude-code") as ws:
        ws.send_json({"type": "auto-approval-toggle", "cwd": "/p", "enabled": True})
        _wait_for_kind(ws, "auto_approval_state")
        ws.send_json({"type": "auto-approval-toggle", "cwd": "/p", "enabled": False})
        msg = _wait_for_kind(ws, "auto_approval_state")
        assert msg["enabled"] is False


def test_auto_approval_toggle_missing_cwd_returns_error(app_client: TestClient) -> None:
    with app_client.websocket_connect("/ws/claude-code") as ws:
        ws.send_json({"type": "auto-approval-toggle", "enabled": True})
        msg = ws.receive_json()
        assert msg.get("kind") == "error"
        assert "cwd required" in msg.get("error", "")


def test_auto_approval_toggle_empty_cwd_returns_error(app_client: TestClient) -> None:
    with app_client.websocket_connect("/ws/claude-code") as ws:
        ws.send_json({"type": "auto-approval-toggle", "cwd": "", "enabled": True})
        msg = ws.receive_json()
        assert msg.get("kind") == "error"


# ---------- query 路由 ----------


def test_auto_approval_query_new_cwd_returns_default_off(app_client: TestClient) -> None:
    """query 新 cwd → 默认 enabled=False。"""
    with app_client.websocket_connect("/ws/claude-code") as ws:
        ws.send_json({"type": "auto-approval-query", "cwd": "/new/never/seen"})
        msg = _wait_for_kind(ws, "auto_approval_state")
        assert msg["cwd"] == "/new/never/seen"
        assert msg["enabled"] is False
        assert msg["timeoutMs"] == 10000
        assert msg["ruleOverrides"] == {}


def test_auto_approval_query_after_toggle_reflects_state(app_client: TestClient) -> None:
    with app_client.websocket_connect("/ws/claude-code") as ws:
        ws.send_json({"type": "auto-approval-toggle", "cwd": "/p", "enabled": True})
        _wait_for_kind(ws, "auto_approval_state")
        ws.send_json({"type": "auto-approval-query", "cwd": "/p"})
        msg = _wait_for_kind(ws, "auto_approval_state")
        assert msg["enabled"] is True


def test_auto_approval_query_missing_cwd_returns_error(app_client: TestClient) -> None:
    with app_client.websocket_connect("/ws/claude-code") as ws:
        ws.send_json({"type": "auto-approval-query"})
        msg = ws.receive_json()
        assert msg.get("kind") == "error"


# ---------- 辅助 ----------


def _wait_for_kind(ws: Any, kind: str, max_msgs: int = 10) -> dict[str, Any]:
    """等收到指定 kind 的 frame；最多读 max_msgs 个消息。"""
    for _ in range(max_msgs):
        msg = ws.receive_json()
        if msg.get("kind") == kind:
            return msg  # type: ignore[no-any-return]
    raise AssertionError(f"did not receive kind={kind}")
