"""hosts.web.run sidecar 参数与 ready JSON 单测。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from hosts.web.run import (
    _build_ready_payload,
    _format_base_url,
    _load_config_with_runtime_overrides,
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


def _write_config(tmp_path: Path, *, host_environment: str = "browser") -> Path:
    """写入最小 Web 配置，输出配置文件路径。"""
    config_path = tmp_path / "setting.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f"""
model:
  name: fake
  base_url: http://127.0.0.1:1234/v1
  api_key: ""
web:
  enabled: true
  host_environment: {host_environment}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config_path


def test_resolve_runtime_options_cli_wins(tmp_path: Path, monkeypatch) -> None:
    """CLI 参数优先于环境变量，并写回 home / dist env。"""
    monkeypatch.setenv("KONGMING_WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("KONGMING_WEB_PORT", "8080")
    monkeypatch.setenv("KONGMING_WEB_SERVER_ORIGIN", "http://10.0.0.10:8080")
    monkeypatch.setenv("KONGMING_WEB_HOST_ENVIRONMENT", "xspace")
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
            "--server-origin",
            "http://192.168.31.23:57567/",
            "--host-environment",
            "browser",
            "--print-ready-json",
        ]
    )

    assert options.host == "127.0.0.1"
    assert options.port == 0
    assert options.server_origin == "http://192.168.31.23:57567"
    assert options.public_origin == "http://192.168.31.23:57567"
    assert options.home == home.resolve()
    assert options.config_path == config.resolve()
    assert options.dist_dir == dist.resolve()
    assert options.host_environment == "browser"
    assert options.print_ready_json is True
    assert os.environ["KONGMING_WEB_DIST"] == str(dist.resolve())
    assert os.environ["KONGMING_WEB_HOST_ENVIRONMENT"] == "browser"


def test_resolve_runtime_options_uses_home_root_setting_yaml(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """未显式传 --config 时优先读取 <home>/setting.yaml。"""
    monkeypatch.delenv("KONGMING_CONFIG", raising=False)
    home = tmp_path / "home"
    config = home / "setting.yaml"
    home.mkdir()
    config.write_text("model:\n  name: local\n  base_url: http://127.0.0.1:1234/v1\n")

    options = _resolve_runtime_options(["--home", str(home)])

    assert options.config_path == config.resolve()
    assert os.environ["KONGMING_CONFIG"] == str(config.resolve())


def test_resolve_runtime_options_xspace_launch_keeps_explicit_config_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """XSpace 启动只设置宿主 env，不按配置内容改写配置路径。"""
    monkeypatch.delenv("KONGMING_CONFIG", raising=False)
    home = tmp_path / "home"
    template = _write_config(tmp_path / "resource", host_environment="xspace")

    options = _resolve_runtime_options(
        [
            "--home",
            str(home),
            "--config",
            str(template),
            "--host-environment",
            "xspace",
        ]
    )

    assert options.config_path == template.resolve()
    assert not (home / "setting.yaml").exists()
    assert os.environ["KONGMING_CONFIG"] == str(template.resolve())
    assert os.environ["KONGMING_WEB_HOST_ENVIRONMENT"] == "xspace"


def test_resolve_runtime_options_uses_host_environment_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """CLI 未指定时读取 KONGMING_WEB_HOST_ENVIRONMENT。"""
    monkeypatch.delenv("KONGMING_CONFIG", raising=False)
    monkeypatch.setenv("KONGMING_WEB_HOST_ENVIRONMENT", "xspace")
    home = tmp_path / "home"

    options = _resolve_runtime_options(["--home", str(home)])

    assert options.host_environment == "xspace"
    assert os.environ["KONGMING_WEB_HOST_ENVIRONMENT"] == "xspace"


def test_resolve_runtime_options_supports_public_origin_alias(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """旧 --public-origin alias 回填 server_origin。"""
    monkeypatch.delenv("KONGMING_CONFIG", raising=False)
    home = tmp_path / "home"

    options = _resolve_runtime_options(
        [
            "--home",
            str(home),
            "--public-origin",
            "https://kongming.example.com/",
        ]
    )

    assert options.server_origin == "https://kongming.example.com"
    assert options.public_origin == "https://kongming.example.com"


def test_load_config_runtime_host_environment_defaults_to_browser(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """普通 Web 启动默认用 browser 覆盖 setting 里的宿主环境字段。"""
    config = _write_config(tmp_path, host_environment="xspace")
    monkeypatch.delenv("KONGMING_WEB_HOST_ENVIRONMENT", raising=False)

    options = _resolve_runtime_options(["--home", str(tmp_path / "home"), "--config", str(config)])
    cfg = _load_config_with_runtime_overrides(
        options.config_path,
        host_environment=options.host_environment,
    )

    assert cfg.web.host_environment == "browser"
    assert os.environ["KONGMING_WEB_HOST_ENVIRONMENT"] == "browser"


def test_load_config_runtime_host_environment_cli_masks_bad_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """CLI 宿主环境覆盖应先于坏 env 进入配置校验。"""
    config = _write_config(tmp_path, host_environment="xspace")
    monkeypatch.setenv("KONGMING_WEB_HOST_ENVIRONMENT", "bad")

    options = _resolve_runtime_options(
        [
            "--home",
            str(tmp_path / "home"),
            "--config",
            str(config),
            "--host-environment",
            "browser",
        ]
    )
    cfg = _load_config_with_runtime_overrides(
        options.config_path,
        host_environment=options.host_environment,
    )

    assert cfg.web.host_environment == "browser"
    assert os.environ["KONGMING_WEB_HOST_ENVIRONMENT"] == "browser"


def test_load_config_runtime_host_environment_normalizes_env_before_validation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """env 宿主环境带空白或大小写时按运行时解析值进入配置校验。"""
    config = _write_config(tmp_path, host_environment="browser")
    monkeypatch.setenv("KONGMING_WEB_HOST_ENVIRONMENT", " XSPACE ")

    options = _resolve_runtime_options(["--home", str(tmp_path / "home"), "--config", str(config)])
    cfg = _load_config_with_runtime_overrides(
        options.config_path,
        host_environment=options.host_environment,
    )

    assert cfg.web.host_environment == "xspace"
    assert os.environ["KONGMING_WEB_HOST_ENVIRONMENT"] == "xspace"


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
        server_origin="http://192.168.31.23:49152",
        host_environment="xspace",
    )
    assert cfg.web.host == "127.0.0.1"
    assert cfg.web.port == 49152
    assert cfg.web.server_origin == "http://192.168.31.23:49152"
    assert cfg.web.public_origin == "http://192.168.31.23:49152"
    assert cfg.web.host_environment == "xspace"


def test_ready_payload_schema(tmp_path: Path) -> None:
    """ready JSON / server.json schema 包含宿主发现字段且不含 auth。"""
    payload = _build_ready_payload(
        host="127.0.0.1",
        port=60000,
        home=tmp_path,
        dist_dir=tmp_path / "dist",
        server_origin="http://192.168.31.23:60000",
    )

    assert payload["type"] == "kongming_web_ready"
    assert payload["base_url"] == "http://127.0.0.1:60000"
    assert payload["server_origin"] == "http://192.168.31.23:60000"
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
        server_origin="http://192.168.31.23:60000",
        timezone_name="Asia/Shanghai",
        print_ready_json=True,
    )

    server_json = tmp_path / "web" / "server.json"
    assert server_json.is_file()
    from_file = json.loads(server_json.read_text(encoding="utf-8"))
    from_stdout = json.loads(capsys.readouterr().out)
    assert from_file["type"] == "kongming_web_ready"
    assert from_file["server_origin"] == "http://192.168.31.23:60000"
    assert from_file["public_origin"] == "http://192.168.31.23:60000"
    assert datetime.fromisoformat(from_file["started_at"]).utcoffset() == timedelta(hours=8)
    assert from_stdout["server_json"] == str(server_json)
    assert from_stdout["started_at"] == from_file["started_at"]


def test_format_base_url_handles_ipv6() -> None:
    """IPv6 host 需要加方括号。"""
    assert _format_base_url("::1", 60000) == "http://[::1]:60000"
    assert _format_base_url("127.0.0.1", 60000) == "http://127.0.0.1:60000"
