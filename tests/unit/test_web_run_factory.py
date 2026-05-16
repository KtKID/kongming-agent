"""Unit tests for web/run.py:_make_runtime_factory.

Mock NativeRuntime.build and verify:
- Preset lookup and error on unknown preset_id
- ModelConfig override (name, base_url, api_key, provider, reasoning_effort)
- API key read from env via api_key_env
- Session factory with bootstrap
- Approval wrapping (build_default_approval with adapter.prompt_approval)
- echo_final_content=False on SessionBridge
- Instructions lazy-load and cache
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config_loader.models import Config


def _make_test_config() -> Config:
    """Build a minimal Config with two presets for testing."""
    from io import StringIO

    import yaml

    yaml_text = """
model:
  name: base-model
  base_url: http://127.0.0.1:1234/v1
  api_key: ""
  timeout: 30
  max_tokens: 2048
  temperature: 0.7

runner:
  max_turns: 5

session:
  backend: memory

trace:
  output_path: .kongming/trace.jsonl
  auto_flush: true
  raw_llm: false

logging:
  level: WARNING

host:
  kind: cli

approval:
  mode: interactive

tool:
  shell:
    enabled: false
  file:
    enabled: false

web:
  enabled: true
  host: "127.0.0.1"
  port: 8080
  idle_timeout_seconds: 60
  llm_presets:
    - id: test-local
      display_name: "Test Local"
      base_url: http://127.0.0.1:1234/v1
      model: test-model
    - id: test-remote
      display_name: "Test Remote"
      provider: anthropic
      base_url: https://api.anthropic.com
      model: claude-test
      api_key_env: TEST_API_KEY
      reasoning_effort: high

stream:
  enabled: true
  read_timeout: 120.0
  suppress_content_after_tool_call: true
  delta_sampling: none
  periodic_batch_size: 20

compactor:
  enabled: false
  max_messages: 50
  keep_recent: 20
  keep_system: true
  tool_result_max_chars: 2000

retry:
  max_retries: 1
  retry_backoff: 1.0

cli:
  show_reasoning: false

evolution:
  memory:
    enabled: false

safety:
  hard_deny_commands: []
  approval_required_commands: []
  sensitive_paths: []
"""
    data = yaml.safe_load(StringIO(yaml_text))
    return Config(**data)


@pytest.fixture()
def test_cfg():
    return _make_test_config()


@pytest.fixture()
def mock_adapter():
    """Mock WebHostAdapter with prompt_approval method."""
    adapter = MagicMock()
    adapter.prompt_approval = AsyncMock(return_value=True)
    return adapter


@pytest.fixture()
def mock_deps():
    """Patch all external dependencies that _make_runtime_factory imports internally.

    The factory uses lazy imports inside the closure, so we must patch
    at the source module level, not at web.run level.
    """
    with (
        patch("executors.agent_runtime.native_runtime.NativeRuntime") as MockRuntime,
        patch("host.session_bridge.SessionBridge") as MockBridge,
        patch(
            "context.instruction_loader.assemble_instructions", new_callable=AsyncMock
        ) as mock_asm,
        patch("context.skill_loader.load_skill_specs", new_callable=AsyncMock) as mock_skills,
        patch("tools.build_default_approval") as mock_approval,
        patch("tools.build_default_registry") as mock_registry,
        patch("tools.register_schedule_tool_if_enabled") as mock_register_schedule,
        patch("tools.register_evolution_write_tool_if_enabled") as mock_register_evolution,
    ):
        mock_asm.return_value = ("test instructions", ["prompts", "env", "runtime"])
        mock_skills.return_value = []
        mock_runtime_instance = MagicMock()
        MockRuntime.build.return_value = mock_runtime_instance
        MockBridge.return_value = MagicMock()
        mock_approval.return_value = MagicMock()
        mock_reg_instance = MagicMock()
        mock_reg_instance.names.return_value = ["read_file", "shell"]
        mock_registry.return_value = mock_reg_instance
        mock_register_schedule.return_value = None
        mock_register_evolution.return_value = None

        yield {
            "NativeRuntime": MockRuntime,
            "SessionBridge": MockBridge,
            "assemble_instructions": mock_asm,
            "load_skill_specs": mock_skills,
            "build_default_approval": mock_approval,
            "registry": mock_reg_instance,
            "register_schedule_tool_if_enabled": mock_register_schedule,
        }


class TestPresetLookup:
    """Preset resolution and error handling."""

    @pytest.mark.asyncio
    async def test_unknown_preset_raises_value_error(self, test_cfg, mock_adapter):
        from web.run import _make_runtime_factory

        factory = _make_runtime_factory(test_cfg)
        with pytest.raises(ValueError, match="unknown preset_id"):
            await factory("thread-1", "nonexistent", mock_adapter, [])

    @pytest.mark.asyncio
    async def test_valid_preset_found(self, test_cfg, mock_adapter, mock_deps):
        from web.run import _make_runtime_factory

        factory = _make_runtime_factory(test_cfg)
        await factory("thread-1", "test-local", mock_adapter, [])

        mock_deps["NativeRuntime"].build.assert_called_once()


class TestModelConfigOverride:
    """Verify preset fields correctly override cfg.model."""

    @pytest.mark.asyncio
    async def test_local_preset_overrides(self, test_cfg, mock_adapter, mock_deps):
        from web.run import _make_runtime_factory

        factory = _make_runtime_factory(test_cfg)
        await factory("thread-1", "test-local", mock_adapter, [])

        call_kwargs = mock_deps["NativeRuntime"].build.call_args
        preset_cfg = call_kwargs[0][0]

        assert preset_cfg.model.name == "test-model"
        assert preset_cfg.model.base_url == "http://127.0.0.1:1234/v1"
        assert preset_cfg.model.api_key == ""
        assert preset_cfg.model.provider is None
        # Other fields from cfg.model preserved
        assert preset_cfg.model.timeout == 30
        assert preset_cfg.model.max_tokens == 2048

    @pytest.mark.asyncio
    async def test_remote_preset_with_env_key(self, test_cfg, mock_adapter, mock_deps):
        from web.run import _make_runtime_factory

        factory = _make_runtime_factory(test_cfg)
        with patch.dict(os.environ, {"TEST_API_KEY": "sk-test-123"}):
            await factory("thread-2", "test-remote", mock_adapter, [])

        call_kwargs = mock_deps["NativeRuntime"].build.call_args
        preset_cfg = call_kwargs[0][0]

        assert preset_cfg.model.name == "claude-test"
        assert preset_cfg.model.base_url == "https://api.anthropic.com"
        assert preset_cfg.model.api_key == "sk-test-123"
        assert preset_cfg.model.provider == "anthropic"
        assert preset_cfg.model.reasoning_effort == "high"

    @pytest.mark.asyncio
    async def test_missing_env_key_gives_empty(self, test_cfg, mock_adapter, mock_deps):
        from web.run import _make_runtime_factory

        factory = _make_runtime_factory(test_cfg)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_API_KEY", None)
            await factory("thread-2", "test-remote", mock_adapter, [])

        call_kwargs = mock_deps["NativeRuntime"].build.call_args
        preset_cfg = call_kwargs[0][0]
        assert preset_cfg.model.api_key == ""


class TestApprovalWiring:
    """Verify adapter.prompt_approval is passed to build_default_approval."""

    @pytest.mark.asyncio
    async def test_interactive_mode_passes_prompt_fn(self, test_cfg, mock_adapter, mock_deps):
        from web.run import _make_runtime_factory

        factory = _make_runtime_factory(test_cfg)
        await factory("thread-1", "test-local", mock_adapter, [])

        mock_deps["build_default_approval"].assert_called_once_with(
            "interactive", prompt_fn=mock_adapter.prompt_approval
        )


class TestSessionBridge:
    """Verify SessionBridge is constructed with echo_final_content=False."""

    @pytest.mark.asyncio
    async def test_echo_final_content_false(self, test_cfg, mock_adapter, mock_deps):
        from web.run import _make_runtime_factory

        factory = _make_runtime_factory(test_cfg)
        await factory("thread-abc", "test-local", mock_adapter, [])

        mock_deps["SessionBridge"].assert_called_once_with(
            runtime=mock_deps["NativeRuntime"].build.return_value,
            adapter=mock_adapter,
            session_id="thread-abc",
            echo_final_content=False,
        )


class TestInstructionsCaching:
    """Verify instructions are loaded once and cached."""

    @pytest.mark.asyncio
    async def test_instructions_loaded_once_for_multiple_cells(
        self, test_cfg, mock_adapter, mock_deps
    ):
        from web.run import _make_runtime_factory

        factory = _make_runtime_factory(test_cfg)

        # Call factory twice — instructions should only be assembled once
        await factory("thread-1", "test-local", mock_adapter, [])
        await factory("thread-2", "test-local", mock_adapter, [])

        mock_deps["assemble_instructions"].assert_called_once()
        assert mock_deps["NativeRuntime"].build.call_count == 2


class TestSchedulerRuntimeFactory:
    """Verify shared scheduler runtime factory inherits the same tool wiring."""

    @pytest.mark.asyncio
    async def test_scheduler_runtime_factory_forwards_tools_and_enabled_names(
        self, test_cfg, mock_adapter, mock_deps
    ):
        from web.run import _make_runtime_factory

        test_cfg.scheduler.enabled = True
        factory = _make_runtime_factory(test_cfg)
        await factory("thread-1", "test-local", mock_adapter, [])

        scheduler_factory = getattr(factory, "_scheduler_runtime_factory", None)
        assert scheduler_factory is not None

        with patch("scheduler.runtime_factory.build_cron_execution_bridge") as mock_bridge:
            dummy_store = object()
            scheduler_factory(dummy_store)

        mock_bridge.assert_called_once()
        _, kwargs = mock_bridge.call_args
        assert kwargs["tools"] is mock_deps["registry"]
        assert kwargs["enabled_tool_names"] == ["read_file", "shell"]
        assert kwargs["instructions"] == "test instructions"


class TestEventSinks:
    """Verify event sinks are passed through correctly."""

    @pytest.mark.asyncio
    async def test_sinks_passed_to_runtime(self, test_cfg, mock_adapter, mock_deps):
        from observability import JsonlTraceSink
        from web.run import _make_runtime_factory

        factory = _make_runtime_factory(test_cfg)
        mock_sink = MagicMock()

        await factory("thread-1", "test-local", mock_adapter, [mock_sink])

        # Sinks list 包含调用方传入的 mock_sink + factory 自己装配的 JsonlTraceSink
        # （按 thread_id 拆文件，文件名形如 trace.thread-1.jsonl）
        call_kwargs = mock_deps["NativeRuntime"].build.call_args
        passed_sinks = call_kwargs[1]["event_sinks"]
        assert mock_sink in passed_sinks
        jsonl_sinks = [s for s in passed_sinks if isinstance(s, JsonlTraceSink)]
        assert len(jsonl_sinks) == 1
        # 验证按 thread_id 拆文件
        assert "thread-1" in str(jsonl_sinks[0].output_path)

    @pytest.mark.asyncio
    async def test_jsonl_sink_path_per_thread_isolation(self, test_cfg, mock_adapter, mock_deps):
        """两个不同 thread_id 各自分配独立的 JsonlTraceSink，文件路径不冲突。"""
        from observability import JsonlTraceSink
        from web.run import _make_runtime_factory

        factory = _make_runtime_factory(test_cfg)

        await factory("thread-aaa111bbb222", "test-local", mock_adapter, [])
        sinks_a = mock_deps["NativeRuntime"].build.call_args[1]["event_sinks"]
        path_a = next(s for s in sinks_a if isinstance(s, JsonlTraceSink)).output_path

        await factory("thread-ccc333ddd444", "test-local", mock_adapter, [])
        sinks_b = mock_deps["NativeRuntime"].build.call_args[1]["event_sinks"]
        path_b = next(s for s in sinks_b if isinstance(s, JsonlTraceSink)).output_path

        assert path_a != path_b
        assert "thread-aaa111bbb222" in str(path_a)
        assert "thread-ccc333ddd444" in str(path_b)
        # 父目录一致（拆文件不拆目录）
        assert path_a.parent == path_b.parent
