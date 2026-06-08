"""诊断 POST /api/cron/tasks 405 问题。

思路：启动一个真实 uvicorn 子进程，用 httpx 直连测试。
这与 TestClient 不同——TestClient 绕过了 uvicorn 的 HTTP 解析层。

适用：``uv run pytest tests/e2e/test_cron_create_live.py -v``
需要：scheduler.enabled=True（从 .env / setting.yaml 读取）
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_PID_FILE = _REPO / ".kongming" / "web" / "server.pid"
_LOG_FILE = _REPO / ".kongming" / "web" / "server.err.log"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """启动一个临时端口的 uvicorn 进程，yield base_url，测试完停止。"""
    port = _find_free_port()
    env = os.environ.copy()
    env["KONGMING_WEB_PORT"] = str(port)
    env["KONGMING_WEB_DEV_MODE"] = "true"

    proc = subprocess.Popen(
        [sys.executable, "-m", "hosts.web.run"],
        cwd=str(_REPO),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )

    # 等端口就绪
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            time.sleep(0.5)
    else:
        out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
        pytest.skip(f"server did not start on port {port}: {out[:500]}")

    yield f"http://127.0.0.1:{port}"

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _login(base_url: str, password: str = "123456") -> str:
    """登录返回 cookie 字符串。"""
    import urllib.request

    data = json.dumps({"password": password}).encode()
    req = urllib.request.Request(
        f"{base_url}/api/auth/login",
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    resp = urllib.request.urlopen(req)
    set_cookie = resp.headers.get("Set-Cookie", "")
    # 提取 kongming_session=xxx 部分
    for part in set_cookie.split(";"):
        part = part.strip()
        if part.startswith("kongming_session="):
            return part
    pytest.skip(f"login succeeded but no session cookie in: {set_cookie}")


def _request(base_url: str, method: str, path: str, cookie: str, body: dict | None = None):
    """发 HTTP 请求并返回 (status_code, response_body_dict)。"""
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method=method,
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Requested-With", "XMLHttpRequest")
    req.add_header("Cookie", cookie)

    try:
        resp = urllib.request.urlopen(req)
        raw = resp.read().decode()
        return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


def test_live_get_tasks(live_server):
    """GET /api/cron/tasks 应 200。"""
    base = live_server
    cookie = _login(base)
    code, body = _request(base, "GET", "/api/cron/tasks", cookie)
    assert code == 200, f"GET /api/cron/tasks returned {code}: {body}"


def test_live_post_create_task(live_server):
    """POST /api/cron/tasks 应 201（或 503 如果 store 未挂）。

    核心诊断：如果返回 405，说明 uvicorn 层面路由注册有问题。
    """
    base = live_server
    cookie = _login(base)
    code, body = _request(
        base,
        "POST",
        "/api/cron/tasks",
        cookie,
        {
            "name": "live-test-daily",
            "agent_name": "default",
            "input_text": "创建 scheduler-test-daily.txt",
            "schedule_type": "cron",
            "cron_expr": "0 9 * * *",
            "timezone": "Asia/Shanghai",
        },
    )
    # 201 = 创建成功；503 = store 未挂（scheduler 没启用）；405 = bug
    assert code in (201, 503), f"POST /api/cron/tasks returned {code}: {body}"
    if code == 201:
        assert body["name"] == "live-test-daily"
        assert body["trigger_type"] == "cron"


def test_live_post_once_task(live_server):
    """POST /api/cron/tasks — 一次性任务。"""
    from datetime import UTC, datetime, timedelta

    base = live_server
    cookie = _login(base)
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    code, body = _request(
        base,
        "POST",
        "/api/cron/tasks",
        cookie,
        {
            "name": "live-test-once",
            "agent_name": "default",
            "input_text": "创建 scheduler-test-once.txt",
            "schedule_type": "once",
            "once_at": future,
            "timezone": "Asia/Shanghai",
        },
    )
    assert code in (201, 503), f"POST returned {code}: {body}"
    if code == 201:
        assert body["trigger_type"] == "once"


def test_live_post_weekly_task(live_server):
    """POST /api/cron/tasks — 每周任务。"""
    base = live_server
    cookie = _login(base)
    code, body = _request(
        base,
        "POST",
        "/api/cron/tasks",
        cookie,
        {
            "name": "live-test-weekly",
            "agent_name": "default",
            "input_text": "创建 scheduler-test-weekly.txt",
            "schedule_type": "cron",
            "cron_expr": "0 9 * * 3",
            "timezone": "Asia/Shanghai",
        },
    )
    assert code in (201, 503), f"POST returned {code}: {body}"
    if code == 201:
        assert body["trigger_expr"] == "0 9 * * 3"
