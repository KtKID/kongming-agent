from __future__ import annotations

from click.testing import CliRunner

import cli.main as cli_main
from config_loader.models import (
    ApprovalConfig,
    Config,
    ModelConfig,
    RunnerConfig,
    SessionConfig,
    TraceConfig,
)


class _FakeUUID:
    hex = "1234567890abcdef1234567890abcdef"


class _DummyRuntime:
    """最小 runtime stub：满足 CLI `_run` try/finally 调 `await runtime.aclose()` 的合约。"""

    async def aclose(self) -> None:
        return None


def test_generate_cli_session_id_uses_12_hex_chars(monkeypatch) -> None:
    monkeypatch.setattr(cli_main.uuid, "uuid4", lambda: _FakeUUID())

    session_id = cli_main._generate_cli_session_id()

    assert session_id == "cli-1234567890ab"


def test_cli_help_mentions_hex12() -> None:
    runner = CliRunner()

    result = runner.invoke(cli_main.main, ["--help"])

    assert result.exit_code == 0
    assert "cli-<hex12>" in result.output


def _build_cfg() -> Config:
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="stub-model",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        runner=RunnerConfig(max_turns=3),
        session=SessionConfig(backend="memory", store_path=".kongming/sessions.db"),
        trace=TraceConfig(output_path=".kongming/traces/test.jsonl"),
        approval=ApprovalConfig(mode="auto_allow"),
    )


async def test_run_prefers_explicit_session_id(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class _DummyRegistry:
        def register(self, _tool) -> None:
            return None

        def names(self) -> list[str]:
            return []

    class _DummyBridge:
        def __init__(
            self,
            *,
            runtime,
            adapter,
            session_id: str,
            echo_final_content: bool = True,
        ) -> None:
            del runtime, adapter, echo_final_content
            captured["session_id"] = session_id
            self.session_id = session_id

        async def run_loop(self) -> None:
            return None

    monkeypatch.setattr(cli_main, "_load_config_or_exit", lambda _: _build_cfg())

    async def _fake_assemble_instructions(_cfg, _files):
        return "system", [], None

    monkeypatch.setattr(cli_main, "_assemble_instructions", _fake_assemble_instructions)
    monkeypatch.setattr(cli_main, "build_default_registry", lambda **_: _DummyRegistry())
    monkeypatch.setattr(cli_main, "build_default_approval", lambda *_, **__: object())
    monkeypatch.setattr(
        cli_main.NativeRuntime, "build", staticmethod(lambda *_, **__: _DummyRuntime())
    )
    monkeypatch.setattr(cli_main, "SessionBridge", _DummyBridge)
    monkeypatch.setattr(cli_main, "_generate_cli_session_id", lambda: "cli-generated")
    monkeypatch.setattr(cli_main, "_print_banner", lambda *_, **__: None)

    await cli_main._run(
        config_path=None,
        session_id="manual-session",
        verbose=False,
        smoke=False,
        instructions_files=[],
        trace_enabled=False,
        reasoning_effort=None,
    )

    assert captured["session_id"] == "manual-session"


async def test_run_reasoning_effort_overrides_config(monkeypatch) -> None:
    """--reasoning-effort CLI 参数应覆盖 cfg.model.reasoning_effort。"""
    captured: dict = {}

    class _DummyRegistry:
        def register(self, _tool) -> None:
            return None

        def names(self) -> list[str]:
            return []

    class _DummyBridge:
        def __init__(
            self,
            *,
            runtime,
            adapter,
            session_id: str,
            echo_final_content: bool = True,
        ) -> None:
            del runtime, adapter, session_id, echo_final_content
            self.session_id = "x"

        async def run_loop(self) -> None:
            return None

    monkeypatch.setattr(cli_main, "_load_config_or_exit", lambda _: _build_cfg())

    async def _fake_assemble_instructions(_cfg, _files):
        return "system", [], None

    monkeypatch.setattr(cli_main, "_assemble_instructions", _fake_assemble_instructions)
    monkeypatch.setattr(cli_main, "build_default_registry", lambda **_: _DummyRegistry())
    monkeypatch.setattr(cli_main, "build_default_approval", lambda *_, **__: object())

    def _capture_build(cfg, **kwargs):
        captured["reasoning_effort"] = cfg.model.reasoning_effort
        return _DummyRuntime()

    monkeypatch.setattr(cli_main.NativeRuntime, "build", staticmethod(_capture_build))
    monkeypatch.setattr(cli_main, "SessionBridge", _DummyBridge)
    monkeypatch.setattr(cli_main, "_generate_cli_session_id", lambda: "cli-x")
    monkeypatch.setattr(cli_main, "_print_banner", lambda *_, **__: None)

    await cli_main._run(
        config_path=None,
        session_id=None,
        verbose=False,
        smoke=False,
        instructions_files=[],
        trace_enabled=False,
        reasoning_effort="low",
    )

    assert captured["reasoning_effort"] == "low"


def test_cli_help_shows_reasoning_effort_option() -> None:
    """--help 应展示 --reasoning-effort 选项。"""
    runner = CliRunner()
    result = runner.invoke(cli_main.main, ["--help"])
    assert result.exit_code == 0
    assert "reasoning-effort" in result.output


def test_cli_help_shows_debug_option() -> None:
    """--help 应展示 --debug 选项。"""
    runner = CliRunner()
    result = runner.invoke(cli_main.main, ["--help"])
    assert result.exit_code == 0
    assert "--debug" in result.output


async def test_run_debug_passes_prompt_debug_sink(monkeypatch) -> None:
    """--debug 进入 _run 后应给 NativeRuntime.build 注入 prompt debug sink。"""
    captured: dict = {}

    class _DummyRegistry:
        def register(self, _tool) -> None:
            return None

        def names(self) -> list[str]:
            return []

    class _DummyBridge:
        def __init__(
            self,
            *,
            runtime,
            adapter,
            session_id: str,
            echo_final_content: bool = True,
        ) -> None:
            del runtime, adapter, session_id, echo_final_content
            self.session_id = "x"

        async def run_loop(self) -> None:
            return None

    monkeypatch.setattr(cli_main, "_load_config_or_exit", lambda _: _build_cfg())

    async def _fake_assemble_instructions(_cfg, _files):
        return "system", ["agent_spec"], None

    monkeypatch.setattr(cli_main, "_assemble_instructions", _fake_assemble_instructions)
    monkeypatch.setattr(cli_main, "build_default_registry", lambda **_: _DummyRegistry())
    monkeypatch.setattr(cli_main, "build_default_approval", lambda *_, **__: object())

    def _capture_build(_cfg, **kwargs):
        captured.update(kwargs)
        return _DummyRuntime()

    monkeypatch.setattr(cli_main.NativeRuntime, "build", staticmethod(_capture_build))
    monkeypatch.setattr(cli_main, "SessionBridge", _DummyBridge)
    monkeypatch.setattr(cli_main, "_generate_cli_session_id", lambda: "cli-debug")
    monkeypatch.setattr(cli_main, "_print_banner", lambda *_, **__: None)

    await cli_main._run(
        config_path=None,
        session_id=None,
        verbose=False,
        smoke=False,
        instructions_files=[],
        trace_enabled=False,
        reasoning_effort=None,
        prompt_debug=True,
    )

    assert captured["prompt_debug_sink"] is not None
    assert captured["instruction_origins"] == ["agent_spec"]
