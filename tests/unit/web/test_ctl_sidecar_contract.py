"""hosts.web.ctl sidecar 契约单测。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner

from hosts.web import ctl


def test_main_exports_click_cli() -> None:
    """`kongming-web-ctl = hosts.web.ctl:main` 指向可调用入口。"""
    assert callable(ctl.main)


def test_status_accepts_home_and_reads_server_json(tmp_path: Path) -> None:
    """`status --home` 读取指定 home 下的 server.json。"""
    home = tmp_path / "kongming-home"
    server_json = home / "web" / "server.json"
    server_json.parent.mkdir(parents=True)
    server_json.write_text(
        json.dumps(
            {
                "type": "kongming_web_ready",
                "pid": os.getpid(),
                "host": "127.0.0.1",
                "port": 49152,
                "base_url": "http://127.0.0.1:49152",
                "health_url": "http://127.0.0.1:49152/health",
                "home": str(home),
                "server_json": str(server_json),
                "dist_dir": None,
            },
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(ctl.cli, ["status", "--home", str(home)])

    assert result.exit_code == 0, f"exception={result.exception!r}\noutput={result.output}"
    assert f"Starting (PID {os.getpid()}, port 49152)" in result.output
    assert str(home / "web" / "server.log") in result.output


def test_status_handles_server_json_missing_port(tmp_path: Path, monkeypatch) -> None:
    """server.json 缺 port 时回退配置读取，不抛 KeyError。"""
    home = tmp_path / "kongming-home"
    server_json = home / "web" / "server.json"
    config = home / "config" / "setting.yaml"
    server_json.parent.mkdir(parents=True)
    config.parent.mkdir(parents=True)
    server_json.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    config.write_text(
        """
model:
  name: fake
  base_url: http://127.0.0.1:1234/v1
  api_key: ""
web:
  enabled: true
  host: "127.0.0.1"
  port: 49153
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("KONGMING_CONFIG", raising=False)

    result = CliRunner().invoke(ctl.cli, ["status", "--home", str(home)])

    assert result.exit_code == 0, f"exception={result.exception!r}\noutput={result.output}"
    assert "port 49153" in result.output


def test_status_handles_server_json_missing_pid(tmp_path: Path) -> None:
    """server.json 缺 pid 时回退 pid 文件，不抛 KeyError。"""
    home = tmp_path / "kongming-home"
    server_json = home / "web" / "server.json"
    server_json.parent.mkdir(parents=True)
    server_json.write_text(
        json.dumps(
            {
                "host": "127.0.0.1",
                "port": 49154,
                "base_url": "http://127.0.0.1:49154",
            },
        ),
        encoding="utf-8",
    )
    (home / "web" / "server.pid").write_text(str(os.getpid()), encoding="utf-8")

    result = CliRunner().invoke(ctl.cli, ["status", "--home", str(home)])

    assert result.exit_code == 0, f"exception={result.exception!r}\noutput={result.output}"
    assert f"Starting (PID {os.getpid()}, port 49154)" in result.output


def test_status_handles_ipv6_host_without_crashing(tmp_path: Path) -> None:
    """server.json 写 IPv6 host 时 status 不因 AF_INET 崩溃。"""
    home = tmp_path / "kongming-home"
    server_json = home / "web" / "server.json"
    server_json.parent.mkdir(parents=True)
    server_json.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": "::1",
                "port": 49155,
                "base_url": "http://[::1]:49155",
            },
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(ctl.cli, ["status", "--home", str(home)])

    assert result.exit_code == 0, f"exception={result.exception!r}\noutput={result.output}"
    assert f"Starting (PID {os.getpid()}, port 49155)" in result.output
