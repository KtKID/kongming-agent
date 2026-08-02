"""hosts.web.ctl sidecar 契约单测。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner
from dotenv import dotenv_values

from hosts.web import ctl


def test_main_exports_click_cli() -> None:
    """`kongming-web-ctl = hosts.web.ctl:main` 指向可调用入口。"""
    assert callable(ctl.main)


def test_repo_entrypoint_env_loads_only_home_and_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """仓库 `.env` 只用于引导 KONGMING_HOME / KONGMING_CONFIG。"""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "KONGMING_HOME=/tmp/kongming-home\n"
        "KONGMING_CONFIG=/tmp/kongming.yaml\n"
        "KONGMING_WEB_PORT=1987\n"
        "GLM_API_KEY=glm-live\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ctl, "_REPO_ROOT", tmp_path)
    monkeypatch.delenv("KONGMING_SKIP_DOTENV", raising=False)
    monkeypatch.delenv("KONGMING_HOME", raising=False)
    monkeypatch.delenv("KONGMING_CONFIG", raising=False)
    monkeypatch.delenv("KONGMING_WEB_PORT", raising=False)
    monkeypatch.delenv("GLM_API_KEY", raising=False)

    ctl._load_repo_entrypoint_env()

    assert os.environ["KONGMING_HOME"] == "/tmp/kongming-home"
    assert os.environ["KONGMING_CONFIG"] == "/tmp/kongming.yaml"
    assert "KONGMING_WEB_PORT" not in os.environ
    assert "GLM_API_KEY" not in os.environ


def test_sync_repo_dotenv_to_home_merges_runtime_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """仓库 `.env` 的运行时变量合并到 home `.env`，已有 home 值保持优先。"""
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    (repo / ".env").write_text(
        "KONGMING_HOME=/tmp/entry-home\n"
        "KONGMING_CONFIG=/tmp/entry.yaml\n"
        "KONGMING_WEB_HOST=0.0.0.0\n"
        "KONGMING_WEB_PORT=62000\n"
        "KONGMING_WEB_PASSWORD='123456'\n"
        "GLM_API_KEY=glm-live\n"
        "key=lowercase-ignored\n",
        encoding="utf-8",
    )
    (home / ".env").write_text("KONGMING_WEB_PORT=49152\n", encoding="utf-8")
    monkeypatch.setattr(ctl, "_REPO_ROOT", repo)
    monkeypatch.delenv("KONGMING_SKIP_DOTENV", raising=False)
    monkeypatch.delenv("KONGMING_WEB_HOST", raising=False)
    monkeypatch.delenv("KONGMING_WEB_PORT", raising=False)
    monkeypatch.delenv("KONGMING_WEB_PASSWORD", raising=False)
    monkeypatch.delenv("GLM_API_KEY", raising=False)

    updated = ctl._sync_repo_dotenv_to_home(home)

    assert updated == ["KONGMING_WEB_HOST", "KONGMING_WEB_PASSWORD", "GLM_API_KEY"]
    values = dotenv_values(home / ".env")
    assert values["KONGMING_WEB_HOST"] == "0.0.0.0"
    assert values["KONGMING_WEB_PORT"] == "49152"
    assert values["KONGMING_WEB_PASSWORD"] == "123456"
    assert values["GLM_API_KEY"] == "glm-live"
    assert "KONGMING_HOME" not in values
    assert "KONGMING_CONFIG" not in values
    assert "key" not in values


def test_repo_entrypoint_env_keeps_real_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """真实进程入口 env 优先于仓库 `.env`。"""
    env_path = tmp_path / ".env"
    env_path.write_text("KONGMING_HOME=/tmp/from-file\n", encoding="utf-8")
    monkeypatch.setattr(ctl, "_REPO_ROOT", tmp_path)
    monkeypatch.delenv("KONGMING_SKIP_DOTENV", raising=False)
    monkeypatch.setenv("KONGMING_HOME", "/tmp/from-env")

    ctl._load_repo_entrypoint_env()

    assert os.environ["KONGMING_HOME"] == "/tmp/from-env"


def test_repo_entrypoint_env_respects_skip(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """KONGMING_SKIP_DOTENV 禁用仓库入口 env 引导。"""
    env_path = tmp_path / ".env"
    env_path.write_text("KONGMING_HOME=/tmp/from-file\n", encoding="utf-8")
    monkeypatch.setattr(ctl, "_REPO_ROOT", tmp_path)
    monkeypatch.setenv("KONGMING_SKIP_DOTENV", "1")
    monkeypatch.delenv("KONGMING_HOME", raising=False)

    ctl._load_repo_entrypoint_env()

    assert "KONGMING_HOME" not in os.environ


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
    monkeypatch.delenv("KONGMING_WEB_PORT", raising=False)
    monkeypatch.setenv("KONGMING_SKIP_DOTENV", "1")

    result = CliRunner().invoke(ctl.cli, ["status", "--home", str(home)])

    assert result.exit_code == 0, f"exception={result.exception!r}\noutput={result.output}"
    assert "port 49153" in result.output


def test_configured_host_port_ignore_stale_server_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """start 使用配置态 host/port；server.json 只代表运行态。"""
    home = tmp_path / "kongming-home"
    server_json = home / "web" / "server.json"
    config = home / "setting.yaml"
    server_json.parent.mkdir(parents=True)
    server_json.write_text(
        json.dumps({"host": "127.0.0.1", "port": 49150, "pid": os.getpid()}),
        encoding="utf-8",
    )
    config.write_text(
        """
model:
  name: fake
  base_url: http://127.0.0.1:1234/v1
  api_key: ""
web:
  enabled: true
  host: "127.0.0.1"
  port: 49151
""",
        encoding="utf-8",
    )
    (home / ".env").write_text(
        "KONGMING_WEB_HOST=0.0.0.0\nKONGMING_WEB_PORT=49152\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("KONGMING_CONFIG", raising=False)
    monkeypatch.delenv("KONGMING_WEB_HOST", raising=False)
    monkeypatch.delenv("KONGMING_WEB_PORT", raising=False)
    monkeypatch.delenv("KONGMING_SKIP_DOTENV", raising=False)
    monkeypatch.setenv("KONGMING_HOME", str(home))

    assert ctl._read_port(home) == 49150
    assert ctl._read_host(home) == "127.0.0.1"
    assert ctl._read_configured_port(home) == 49152
    assert ctl._read_configured_host(home) == "0.0.0.0"


def test_status_reads_home_root_setting_yaml(tmp_path: Path, monkeypatch) -> None:
    """server.json 缺 port 时优先读取 <home>/setting.yaml。"""
    home = tmp_path / "kongming-home"
    server_json = home / "web" / "server.json"
    config = home / "setting.yaml"
    server_json.parent.mkdir(parents=True)
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
  port: 49156
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("KONGMING_CONFIG", raising=False)
    monkeypatch.delenv("KONGMING_WEB_PORT", raising=False)
    monkeypatch.setenv("KONGMING_SKIP_DOTENV", "1")

    result = CliRunner().invoke(ctl.cli, ["status", "--home", str(home)])

    assert result.exit_code == 0, f"exception={result.exception!r}\noutput={result.output}"
    assert "port 49156" in result.output


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
