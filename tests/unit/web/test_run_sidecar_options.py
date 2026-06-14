"""hosts.web.run sidecar 参数与 ready JSON 单测。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from hosts.web.run import (
    _build_ready_payload,
    _format_base_url,
    _override_web_bind_config,
    _resolve_runtime_options,
    _write_ready_payload,
)
from infrastructure.config.models import Config


def _cfg() -> Config:
    """构造最小 Web Config。"""
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {"enabled": True, "host": "0.0.0.0", "port": 8080},
        }
    )


def test_resolve_runtime_options_cli_wins(tmp_path: Path, monkeypatch) -> None:
    """CLI 参数优先于环境变量，并写回 home / dist env。"""
    monkeypatch.setenv("KONGMING_WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("KONGMING_WEB_PORT", "8080")
    monkeypatch.setenv("KONGMING_WEB_PUBLIC_ORIGIN", "http://10.0.0.10:8080")
    monkeypatch.setenv("KONGMING_HOME", str(tmp_path / "env-home"))
    monkeypatch.setenv("KONGMING_CONFIG", str(tmp_path / "env.yaml"))
    monkeypatch.setenv("KONGMING_WEB_DIST", str(tmp_path / "env-dist"))
    home = tmp_path / "cli-home"
    config = tmp_path / "setting.yaml"
    dist = tmp_path / "dist"

    options = _resolve_runtime_options(
        [
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--home",
            str(home),
            "--config",
            str(config),
            "--dist-dir",
            str(dist),
            "--public-origin",
            "http://192.168.31.23:57567/",
            "--print-ready-json",
        ]
    )

    assert options.host == "127.0.0.1"
    assert options.port == 0
    assert options.public_origin == "http://192.168.31.23:57567"
    assert options.home == home.resolve()
    assert options.config_path == config.resolve()
    assert options.dist_dir == dist.resolve()
    assert options.print_ready_json is True
    assert os.environ["KONGMING_WEB_DIST"] == str(dist.resolve())


def test_resolve_runtime_options_accepts_once_ready_alias(tmp_path: Path, monkeypatch) -> None:
    """旧契约名 --once-ready-json 与 --print-ready-json 语义一致。"""
    monkeypatch.delenv("KONGMING_CONFIG", raising=False)
    home = tmp_path / "home"
    options = _resolve_runtime_options(["--home", str(home), "--once-ready-json"])
    assert options.print_ready_json is True


def test_override_web_bind_config_sets_actual_host_port() -> None:
    """实际绑定地址会写入 Config 副本。"""
    cfg = _override_web_bind_config(
        _cfg(),
        host="127.0.0.1",
        port=49152,
        public_origin="http://192.168.31.23:49152",
    )
    assert cfg.web.host == "127.0.0.1"
    assert cfg.web.port == 49152
    assert cfg.web.public_origin == "http://192.168.31.23:49152"


def test_ready_payload_schema(tmp_path: Path) -> None:
    """ready JSON / server.json schema 包含宿主发现字段且不含 auth。"""
    payload = _build_ready_payload(
        host="127.0.0.1",
        port=60000,
        home=tmp_path,
        dist_dir=tmp_path / "dist",
        public_origin="http://192.168.31.23:60000",
    )

    assert payload["type"] == "kongming_web_ready"
    assert payload["base_url"] == "http://127.0.0.1:60000"
    assert payload["public_origin"] == "http://192.168.31.23:60000"
    assert payload["health_url"] == "http://127.0.0.1:60000/health"
    assert payload["server_json"] == str(tmp_path / "web" / "server.json")
    assert payload["dist_dir"] == str(tmp_path / "dist")
    assert "token" not in payload
    assert "auth" not in payload


def test_ready_payload_started_at_uses_configured_timezone(tmp_path: Path) -> None:
    """server.json started_at 使用配置时区。"""
    payload = _build_ready_payload(
        host="127.0.0.1",
        port=60000,
        home=tmp_path,
        dist_dir=tmp_path / "dist",
        timezone_name="Asia/Shanghai",
    )

    started_at = payload["started_at"]
    assert isinstance(started_at, str)
    parsed = datetime.fromisoformat(started_at)
    assert parsed.utcoffset() == timedelta(hours=8)


def test_write_ready_payload_writes_file_and_stdout(tmp_path: Path, capsys) -> None:
    """ready payload 原子写入 server.json，并按需输出一行 stdout。"""
    _write_ready_payload(
        host="127.0.0.1",
        port=60000,
        home=tmp_path,
        dist_dir=tmp_path / "dist",
        public_origin="http://192.168.31.23:60000",
        timezone_name="Asia/Shanghai",
        print_ready_json=True,
    )

    server_json = tmp_path / "web" / "server.json"
    assert server_json.is_file()
    from_file = json.loads(server_json.read_text(encoding="utf-8"))
    from_stdout = json.loads(capsys.readouterr().out)
    assert from_file["type"] == "kongming_web_ready"
    assert from_file["public_origin"] == "http://192.168.31.23:60000"
    assert datetime.fromisoformat(from_file["started_at"]).utcoffset() == timedelta(hours=8)
    assert from_stdout["server_json"] == str(server_json)
    assert from_stdout["started_at"] == from_file["started_at"]


def test_format_base_url_handles_ipv6() -> None:
    """IPv6 host 需要加方括号。"""
    assert _format_base_url("::1", 60000) == "http://[::1]:60000"
    assert _format_base_url("127.0.0.1", 60000) == "http://127.0.0.1:60000"
