from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
import yaml

from web.auth import CSRF_HEADER_NAME, CSRF_HEADER_VALUE

REPO_ROOT = Path(__file__).resolve().parents[2]
SHANGHAI_TZ = timezone(timedelta(hours=8))


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(base_url: str, method: str, path: str, cookie: str, body: dict | None = None):
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{base_url}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Cookie", cookie)
    req.add_header(CSRF_HEADER_NAME, CSRF_HEADER_VALUE)
    try:
        resp = urllib.request.urlopen(req)
        raw = resp.read().decode("utf-8")
        return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"raw": raw}


def _login(base_url: str, password: str) -> str:
    import urllib.request

    req = urllib.request.Request(
        f"{base_url}/api/auth/login",
        data=json.dumps({"password": password}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            CSRF_HEADER_NAME: CSRF_HEADER_VALUE,
        },
    )
    resp = urllib.request.urlopen(req)
    set_cookie = resp.headers.get("Set-Cookie", "")
    for part in set_cookie.split(";"):
        part = part.strip()
        if part.startswith("kongming_session="):
            return part
    raise AssertionError(f"missing session cookie: {set_cookie}")


def _build_temp_config(tmp_path: Path, port: int, password: str) -> Path:
    with (REPO_ROOT / "config" / "setting.yaml").open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    data.setdefault("web", {})
    data["web"]["enabled"] = True
    data["web"]["dev_mode"] = True
    data["web"]["host"] = "127.0.0.1"
    data["web"]["port"] = port
    data["web"]["initial_password"] = password

    data.setdefault("scheduler", {})
    data["scheduler"]["enabled"] = True
    data["scheduler"]["interval"] = 0.2
    data["scheduler"]["max_inflight"] = 1
    data["scheduler"]["home"] = str((tmp_path / ".kongming" / "cron").resolve())
    data["scheduler"]["default_timezone"] = "Asia/Shanghai"

    data.setdefault("tool", {})
    data["tool"].setdefault("file", {})
    data["tool"]["file"]["enabled"] = True

    config_path = tmp_path / "setting.e2e.yaml"
    config_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return config_path


@pytest.fixture
def live_server(tmp_path: Path):
    port = _find_free_port()
    password = "123456"
    config_path = _build_temp_config(tmp_path, port, password)
    env = os.environ.copy()
    env["KONGMING_CONFIG"] = str(config_path)
    env["KONGMING_HOME"] = str((tmp_path / ".kongming").resolve())

    proc = subprocess.Popen(
        [sys.executable, "-m", "web.run"],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )

    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            time.sleep(0.5)
    else:
        output = proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else ""
        proc.terminate()
        raise AssertionError(f"server did not start on port {port}: {output[:2000]}")

    yield f"http://127.0.0.1:{port}", password, tmp_path

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    if proc.stdout is not None:
        proc.stdout.close()


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv("KONGMING_E2E_REAL_MODEL") != "1",
    reason="set KONGMING_E2E_REAL_MODEL=1 to enable real scheduler trigger e2e",
)
def test_live_once_task_creates_file_after_trigger(live_server) -> None:
    base_url, password, tmp_path = live_server
    cookie = _login(base_url, password)
    target_file = REPO_ROOT / "定时任务测试用例.txt"
    if target_file.exists():
        target_file.unlink()

    once_at = (datetime.now(SHANGHAI_TZ) + timedelta(seconds=8)).isoformat()
    code, body = _request(
        base_url,
        "POST",
        "/api/cron/tasks",
        cookie,
        {
            "name": "trigger-create-file-e2e",
            "agent_name": "default",
            "input_text": "在当前目录创建 定时任务测试用例.txt 文件。直接使用文件工具创建，写入文本：scheduled trigger ok。",
            "schedule_type": "once",
            "once_at": once_at,
            "timezone": "Asia/Shanghai",
        },
    )
    assert code == 201, f"create task failed: {code} {body}"

    deadline = time.time() + 120
    while time.time() < deadline:
        if target_file.exists():
            content = target_file.read_text(encoding="utf-8")
            assert "scheduled trigger ok" in content
            break
        time.sleep(1)
    else:
        traces_dir = tmp_path / ".kongming" / "cron" / "traces"
        trace_files = sorted(p.name for p in traces_dir.glob("*.jsonl")) if traces_dir.exists() else []
        raise AssertionError(f"scheduled file was not created; traces={trace_files}")

    target_file.unlink()
