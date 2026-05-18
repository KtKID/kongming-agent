"""integration/web/global_approvals 共享 fixture。

提供：
- :func:`make_authed_client`：登录态的 ``TestClient``，建联 ``/ws/thread-status``
  时自动带 session cookie
- :func:`reset_singletons`：autouse — 每个 test 前后重置
  ``ApprovalInboxBroadcaster`` + ``ThreadStatusBroadcaster`` 单例，避免污染
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config_loader.models import Config
from tests.unit.test_web_app_lifespan import _seed_password
from tests.unit.test_web_ws_endpoint import WSFakeTM
from web.app import create_app
from web.auth import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from web.global_approvals.broadcaster import reset_inbox_broadcaster_for_testing
from web.thread_status_ws import reset_broadcaster_for_testing

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


@pytest.fixture(autouse=True)
def reset_singletons() -> Iterator[None]:
    """重置 broadcaster 单例，隔离每个测试。"""
    reset_inbox_broadcaster_for_testing()
    reset_broadcaster_for_testing()
    yield
    reset_inbox_broadcaster_for_testing()
    reset_broadcaster_for_testing()


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


@pytest.fixture()
def authed_client(tmp_path: Path) -> Iterator[TestClient]:
    """登录态 TestClient + WSFakeTM。WS handshake 自动带 cookie。"""
    tm = WSFakeTM()
    _seed_password(tmp_path, "pwd")
    cfg = _make_cfg()
    app = create_app(cfg, tm, home_dir=tmp_path)

    client = TestClient(app)
    client.__enter__()
    try:
        r = client.post(
            "/api/auth/login",
            json={"password": "pwd"},
            headers=CSRF_HEADERS,
        )
        assert r.status_code == 200, r.text
        yield client
    finally:
        client.__exit__(None, None, None)
