"""Tests for web.run logging configuration."""

from __future__ import annotations

from logging.config import dictConfig

from hosts.web.run import _build_uvicorn_log_config


def test_uvicorn_log_config_includes_timestamps() -> None:
    config = _build_uvicorn_log_config()

    formatters = config["formatters"]
    assert isinstance(formatters, dict)

    default_formatter = formatters["default"]
    access_formatter = formatters["access"]

    assert isinstance(default_formatter, dict)
    assert isinstance(access_formatter, dict)
    assert "%(asctime)s" in str(default_formatter["format"])
    assert "%(asctime)s" in str(access_formatter["format"])
    assert default_formatter["datefmt"] == "%Y-%m-%d %H:%M:%S"

    handlers = config["handlers"]
    assert isinstance(handlers, dict)
    assert handlers["default"]["stream"] == "ext://sys.stderr"
    assert handlers["access"]["stream"] == "ext://sys.stderr"

    dictConfig(config)
