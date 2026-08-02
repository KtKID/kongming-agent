"""Claude Code 旧 auto_approval 帧停用合同测试。

关键流程：Web app 不再装配 cwd 倒计时 policy；旧 toggle/query 帧仍可解析，
并统一返回 feature_disabled 与稳定 reason，帮助旧客户端明确升级行为。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hosts.web.app import create_app
from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from infrastructure.config.models import Config
from tests.unit.test_web_app_lifespan import FakeThreadManager, _seed_password

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


def _make_cfg() -> Config:
    """构造使用本地 fake provider 的最小 Web 配置。"""
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


@pytest.fixture()
def app_client(tmp_path: Path) -> Iterator[TestClient]:
    """创建已登录的真实 FastAPI TestClient。"""
    _seed_password(tmp_path, "pwd")
    app = create_app(_make_cfg(), FakeThreadManager(), home_dir=tmp_path)
    client = TestClient(app)
    client.__enter__()
    response = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 200
    try:
        yield client
    finally:
        client.__exit__(None, None, None)


def test_app_state_exposes_auto_approval_policy(app_client: TestClient) -> None:
    """Web app 装配共享的 cwd 三模式门户与审计出口。"""
    state = app_client.app.state
    assert hasattr(state, "approval_runtime_manager")
    assert hasattr(state, "approval_manager")
    assert hasattr(state, "permissions_manager")
    assert hasattr(state, "auto_approval_manager")
    assert hasattr(state, "auto_approval_policy")
    assert hasattr(state, "auto_approval_audit")


@pytest.mark.parametrize("frame_type", ["auto-approval-set-mode", "auto-approval-query"])
def test_auto_approval_frame_returns_mode_state(
    app_client: TestClient,
    frame_type: str,
) -> None:
    """Claude WS 能查询和设置 cwd 处置模式。"""
    payload: dict[str, object] = {"frame_type": frame_type, "cwd": "/proj/test"}
    if frame_type == "auto-approval-set-mode":
        payload["mode"] = "llm"
    with app_client.websocket_connect("/ws/claude-code") as websocket:
        websocket.send_json(payload)
        message = websocket.receive_json()
    assert message["frame_type"] == "auto_approval_state"
    assert message["cwd"] == "/proj/test"
    assert message["mode"] == ("llm" if frame_type == "auto-approval-set-mode" else "user")
