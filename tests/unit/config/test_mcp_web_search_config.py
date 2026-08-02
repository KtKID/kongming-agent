"""MCP 与通用 Web Search 配置契约测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.config import ConfigValidationError, load_config
from infrastructure.config.schema import get_field_meta, list_field_metas


def _write_config(tmp_path: Path, body: str = "") -> Path:
    """写入临时 YAML 配置，输入为附加配置正文，输出配置文件路径。"""
    config_path = tmp_path / "setting.yaml"
    config_path.write_text(
        "\n".join(
            [
                "model:",
                "  name: stub-model",
                "  base_url: http://127.0.0.1:1234/v1",
                "  api_key: ''",
                body.rstrip(),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def test_mcp_and_web_search_defaults(tmp_path: Path) -> None:
    """默认配置提供空 MCP server 列表与可装配的通用 Web Search 候选。"""
    cfg = load_config(_write_config(tmp_path), load_env_file=False)

    assert cfg.mcp.servers == ()
    assert cfg.web_search.enabled is True
    assert cfg.web_search.provider_name == "minimax_web_search"
    assert cfg.web_search.search_tool_name is None
    assert cfg.web_search.search_tool_names == ("web_search", "mcp__minimax__web_search")
    assert cfg.web_search.max_results == 5


def test_mcp_and_web_search_yaml_parsing(tmp_path: Path) -> None:
    """YAML 能解析 MCP server、alias 和 Web Search provider 配置。"""
    cfg = load_config(
        _write_config(
            tmp_path,
            """
mcp:
  servers:
    - server_id: minimax
      enabled: true
      command: uvx
      args:
        - minimax-mcp
        - --stdio
      env:
        MINIMAX_REGION: cn
      secret_env_keys:
        - MINIMAX_API_KEY
      initialize_timeout_ms: 15000
      call_timeout_ms: 45000
      aliases:
        - tool_name: web_search
          alias: web_search
          enabled: true
web_search:
  enabled: true
  provider_name: minimax_web_search
  search_tool_name: mcp__minimax__web_search
  search_tool_names:
    - mcp__minimax__web_search
    - web_search
  max_results: 8
""",
        ),
        load_env_file=False,
    )

    assert len(cfg.mcp.servers) == 1
    server = cfg.mcp.servers[0]
    assert server.server_id == "minimax"
    assert server.enabled is True
    assert server.command == "uvx"
    assert server.args == ("minimax-mcp", "--stdio")
    assert server.env == {"MINIMAX_REGION": "cn"}
    assert server.secret_env_keys == ("MINIMAX_API_KEY",)
    assert server.initialize_timeout_ms == 15000
    assert server.call_timeout_ms == 45000
    assert len(server.aliases) == 1
    assert server.aliases[0].tool_name == "web_search"
    assert server.aliases[0].alias == "web_search"
    assert server.aliases[0].enabled is True
    assert cfg.web_search.search_tool_name == "mcp__minimax__web_search"
    assert cfg.web_search.search_tool_names == ("mcp__minimax__web_search", "web_search")
    assert cfg.web_search.max_results == 8


def test_mcp_and_web_search_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """env 白名单覆盖 Web Search 标量 / tuple 字段。"""
    config_path = _write_config(
        tmp_path,
        """
web_search:
  enabled: true
  provider_name: yaml_provider
  search_tool_name: yaml_search
  search_tool_names:
    - yaml_search
  max_results: 3
""",
    )
    monkeypatch.setenv("KONGMING_WEB_SEARCH_ENABLED", "false")
    monkeypatch.setenv("KONGMING_WEB_SEARCH_PROVIDER_NAME", "env_provider")
    monkeypatch.setenv("KONGMING_WEB_SEARCH_SEARCH_TOOL_NAME", "env_search")
    monkeypatch.setenv("KONGMING_WEB_SEARCH_SEARCH_TOOL_NAMES", "env_a, env_b,env_c")
    monkeypatch.setenv("KONGMING_WEB_SEARCH_MAX_RESULTS", "11")

    cfg = load_config(config_path, load_env_file=False)

    assert cfg.mcp.servers == ()
    assert cfg.web_search.enabled is False
    assert cfg.web_search.provider_name == "env_provider"
    assert cfg.web_search.search_tool_name == "env_search"
    assert cfg.web_search.search_tool_names == ("env_a", "env_b", "env_c")
    assert cfg.web_search.max_results == 11


def test_mcp_server_rejects_illegal_server_id(tmp_path: Path) -> None:
    """server_id 只允许字母、数字、下划线和短横线。"""
    config_path = _write_config(
        tmp_path,
        """
mcp:
  servers:
    - server_id: minimax/web
      command: uvx
""",
    )

    with pytest.raises(ConfigValidationError):
        load_config(config_path, load_env_file=False)


def test_schema_exposes_mcp_and_web_search_field_metas() -> None:
    """schema API 暴露 MCP 与 Web Search 配置字段元信息。"""
    listed_metas = {meta.path: meta for meta in list_field_metas()}
    expectations = {
        "mcp.servers": ("list", False, True),
        "web_search.enabled": ("bool", True, True),
        "web_search.provider_name": ("string", True, True),
        "web_search.search_tool_name": ("string", True, True),
        "web_search.search_tool_names": ("list", False, True),
        "web_search.max_results": ("int", True, True),
    }

    for path, (expected_type, expected_editable, expected_restart_required) in expectations.items():
        listed_meta = listed_metas.get(path)
        assert listed_meta is not None
        assert get_field_meta(path) == listed_meta
        assert listed_meta.type == expected_type
        assert listed_meta.editable is expected_editable
        assert listed_meta.restart_required is expected_restart_required
