"""Kongming Web 后端 sidecar exe smoke 测试。"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXE_PATH = REPO_ROOT / "dist" / "kongming-web-backend" / "kongming-web-backend.exe"


def _read_json(path: Path) -> dict[str, Any]:
    """读取 JSON 文件并返回 dict。"""
    return json.loads(path.read_text(encoding="utf-8"))


def _process_diagnostics(proc: subprocess.Popen, stderr_path: Path) -> str:
    """收集子进程退出码和 stderr 片段，用于 smoke 失败诊断。"""
    stderr = (
        stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
    )
    return f"returncode={proc.poll()} stderr_tail={stderr[-4000:]}"


def _wait_for_server_json(
    *,
    path: Path,
    proc: subprocess.Popen,
    stderr_path: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    """等待 sidecar 写入 server.json。

    Args:
        path: 预期的 server.json 路径。
        proc: sidecar 子进程。
        stderr_path: stderr 捕获文件。
        timeout_seconds: 最大等待秒数。

    Returns:
        server.json 中的 ready payload。
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.is_file():
            return _read_json(path)
        if proc.poll() is not None:
            pytest.fail(
                f"sidecar exited before server.json: {_process_diagnostics(proc, stderr_path)}"
            )
        time.sleep(0.2)
    pytest.fail(f"timed out waiting for server.json: {_process_diagnostics(proc, stderr_path)}")


def _wait_for_health(
    *,
    url: str,
    proc: subprocess.Popen,
    stderr_path: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    """轮询 /health 直到返回 200。

    Args:
        url: health URL。
        proc: sidecar 子进程。
        stderr_path: stderr 捕获文件。
        timeout_seconds: 最大等待秒数。

    Returns:
        health JSON 响应。
    """
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(
                f"sidecar exited before health ok: {_process_diagnostics(proc, stderr_path)}"
            )
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # pragma: no cover - 只用于轮询诊断
            last_error = exc
            time.sleep(0.2)
    pytest.fail(
        f"timed out waiting for health {url}: {last_error}; "
        f"{_process_diagnostics(proc, stderr_path)}"
    )


def _terminate(proc: subprocess.Popen, *, home: Path) -> None:
    """终止 sidecar 子进程。

    Args:
        proc: `subprocess.Popen` 返回的进程句柄。
        home: 本次 smoke 使用的 home，用于清理 PyInstaller onefile 派生出的真实进程。
    """
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if platform.system() == "Windows":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                proc.kill()
            proc.wait(timeout=10)

    if platform.system() == "Windows":
        script = """
$homePath = [Console]::In.ReadToEnd().Trim()
$procs = Get-CimInstance Win32_Process |
  Where-Object {
    $_.Name -eq 'kongming-web-backend.exe' -and
    $_.CommandLine -like "*--home $homePath*"
  }
foreach ($p in $procs) {
  taskkill /PID $p.ProcessId /T /F | Out-Null
}
"""
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            input=str(home),
            text=True,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


@pytest.mark.skipif(not EXE_PATH.is_file(), reason="kongming-web-backend.exe has not been built")
def test_kongming_web_backend_exe_ready_health_and_home_isolation(tmp_path: Path) -> None:
    """exe 可启动、写 ready JSON、通过 /health，并把运行时文件写入 --home。"""
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    server_json = home / "web" / "server.json"
    dist_dir = REPO_ROOT / "web" / "dist"

    command = [
        str(EXE_PATH),
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--home",
        str(home),
        "--config",
        str(REPO_ROOT / "config" / "setting.yaml"),
        "--dist-dir",
        str(dist_dir),
        "--print-ready-json",
    ]
    env = os.environ.copy()
    env.pop("KONGMING_WEB_DIST", None)
    env["PYTHONUTF8"] = "1"
    env["KONGMING_WEB_PASSWORD"] = "kongming-web-backend-smoke-password"

    with (
        stdout_path.open("w", encoding="utf-8") as stdout,
        stderr_path.open(
            "w",
            encoding="utf-8",
        ) as stderr,
    ):
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
        try:
            payload = _wait_for_server_json(
                path=server_json,
                proc=proc,
                stderr_path=stderr_path,
                timeout_seconds=90,
            )
            health = _wait_for_health(
                url=str(payload["health_url"]),
                proc=proc,
                stderr_path=stderr_path,
                timeout_seconds=30,
            )

            assert payload["type"] == "kongming_web_ready"
            assert payload["host"] == "127.0.0.1"
            assert int(payload["port"]) > 0
            assert payload["base_url"] == f"http://127.0.0.1:{payload['port']}"
            assert payload["server_json"] == str(server_json)
            assert payload["home"] == str(home.resolve())
            assert payload["dist_dir"] == str(dist_dir.resolve())
            assert health == {"status": "ok"}
            assert not (cwd / ".kongming").exists()
        finally:
            _terminate(proc, home=home)

    stdout_lines = [line for line in stdout_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(stdout_lines) == 1
    stdout_payload = json.loads(stdout_lines[0])
    assert stdout_payload["type"] == "kongming_web_ready"
    assert stdout_payload["server_json"] == str(server_json)
