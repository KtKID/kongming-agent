"""Unit tests for web/run.py:_make_runtime_factory.

Mock SessionEngine.build and verify:
- Preset lookup and error on unknown preset_id
- Catalog preset resolution through ModelCatalogManager
- Session factory with bootstrap
- Approval wrapping (build_default_approval with adapter.prompt_approval)
- HostDispatcher 构造参数(runtime/session_id/queued_result_handler)
- Instructions lazy-load and cache
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from infrastructure.config.models import Config, ModelSelectionConfig


def _make_test_config() -> Config:
    """Build a minimal v0.6 Config for runtime-factory testing."""
    return Config(model=ModelSelectionConfig(preset_id="test-local"))


@pytest.fixture()
def test_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """提供 user catalog 中的本地测试 preset。"""
    import yaml

    monkeypatch.setenv("KONGMING_HOME", str(tmp_path))
    catalog = {
        "version": 2,
        "providers": [
            {
                "provider_id": "test-provider",
                "default_preset_id": "test-local",
                "display_name": "Test Provider",
                "region_label": "Test",
                "description": "Web runtime factory tests.",
                "logo_text": "T",
                "protocol": "openai",
                "default_base_url": "http://127.0.0.1:1234/v1",
                "request_defaults": {
                    "timeout_seconds": 30,
                    "max_tokens": 2048,
                    "temperature": 0.7,
                },
                "models": [
                    {
                        "preset_id": "test-local",
                        "display_name": "Test Local",
                        "model": "test-model",
                    }
                ],
            }
        ],
    }
    (tmp_path / "model-providers.yaml").write_text(
        yaml.safe_dump(catalog, sort_keys=False),
        encoding="utf-8",
    )
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
    from prompting.instructions.instruction_loader import InstructionSource

    with (
        patch("runtime_assembly.session_engine.SessionEngine") as MockRuntime,
        patch("hosts.shared.host_dispatcher.HostDispatcher") as MockHostDispatcher,
        patch(
            "prompting.instructions.instruction_loader.assemble_instructions",
            new_callable=AsyncMock,
        ) as mock_asm,
        patch(
            "prompting.instructions.instruction_loader.load_instruction_sources",
            new_callable=AsyncMock,
        ) as mock_sources,
        patch(
            "prompting.skills.skill_loader.load_skill_specs", new_callable=AsyncMock
        ) as mock_skills,
        patch("tools.build_default_approval") as mock_approval,
        patch("tools.build_default_registry") as mock_registry,
        patch("tools.register_schedule_tool_if_enabled") as mock_register_schedule,
        patch("safety.approval.chain.build_safety_chain") as mock_safety_chain,
    ):
        mock_asm.return_value = ("test instructions", ["prompts", "env", "runtime"])

        async def _fake_instruction_sources(**kwargs: object) -> list[InstructionSource]:
            sources = [
                InstructionSource(origin="runtime", content="runtime context"),
            ]
            workflow_catalog = str(kwargs.get("workflow_catalog") or "")
            if workflow_catalog.strip():
                sources.append(
                    InstructionSource(
                        origin="workflow_catalog",
                        content=workflow_catalog,
                    )
                )
            sources.append(InstructionSource(origin="agent_spec", content="test instructions"))
            skill_listing = str(kwargs.get("skill_listing") or "")
            if skill_listing.strip():
                sources.append(InstructionSource(origin="skills", content=skill_listing))
            memory_store = kwargs.get("memory_store")
            if kwargs.get("inject_memory") and memory_store is not None:
                snapshot = getattr(memory_store, "snapshot", None)
                memory_prompt = snapshot.render_prompt() if snapshot is not None else None
                if memory_prompt:
                    sources.append(InstructionSource(origin="memory", content=memory_prompt))
            return sources

        mock_sources.side_effect = _fake_instruction_sources
        mock_skills.return_value = []
        mock_runtime_instance = MagicMock()
        MockRuntime.build.return_value = mock_runtime_instance
        MockHostDispatcher.return_value = MagicMock()
        mock_approval.return_value = MagicMock()
        mock_reg_instance = MagicMock()
        mock_reg_instance.names.return_value = ["read_file", "shell"]
        mock_registry.return_value = mock_reg_instance
        mock_register_schedule.return_value = None
        mock_safety_chain.return_value = MagicMock(name="cron_safety_chain")

        yield {
            "SessionEngine": MockRuntime,
            "HostDispatcher": MockHostDispatcher,
            "assemble_instructions": mock_asm,
            "load_instruction_sources": mock_sources,
            "load_skill_specs": mock_skills,
            "build_default_approval": mock_approval,
            "build_default_registry": mock_registry,
            "registry": mock_reg_instance,
            "register_schedule_tool_if_enabled": mock_register_schedule,
            "build_safety_chain": mock_safety_chain,
        }


class TestPresetLookup:
    """Preset resolution and error handling."""

    @pytest.mark.asyncio
    async def test_unknown_preset_raises_value_error(self, test_cfg, mock_adapter):
        from hosts.web.run import _make_runtime_factory

        factory = _make_runtime_factory(test_cfg)
        with pytest.raises(ValueError, match="unknown preset_id"):
            await factory("thread-1", "nonexistent", mock_adapter, [])

    @pytest.mark.asyncio
    async def test_valid_preset_found(self, test_cfg, mock_adapter, mock_deps):
        from hosts.web.run import _make_runtime_factory

        factory = _make_runtime_factory(test_cfg)
        await factory("thread-1", "test-local", mock_adapter, [])

        mock_deps["SessionEngine"].build.assert_called_once()


class TestMemoryWiring:
    """Web generic_chat 默认启用长期记忆工具。"""

    @pytest.mark.asyncio
    async def test_default_memory_registers_tool_and_forwards_to_runtime(
        self,
        test_cfg,
        mock_adapter,
        mock_deps,
        tmp_path,
        monkeypatch,
    ):
        from hosts.shared.memory_refresh_sink import MemoryRefreshSink
        from hosts.web.run import _make_runtime_factory
        from infrastructure.config.models import EvolutionConfig
        from tools import ToolRegistry

        monkeypatch.setenv("KONGMING_HOME", str(tmp_path))
        test_cfg.evolution = EvolutionConfig()
        assert test_cfg.evolution.memory.enabled is True
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "MEMORY.md").write_text(
            "默认记忆已加载",
            encoding="utf-8",
        )

        registry = ToolRegistry()
        mock_deps["build_default_registry"].return_value = registry

        factory = _make_runtime_factory(test_cfg)
        await factory("thread-memory", "test-local", mock_adapter, [])

        assert "memory" in registry.names()
        runtime_kwargs = mock_deps["SessionEngine"].build.call_args.kwargs
        assert "memory" in runtime_kwargs["enabled_tool_names"]
        assert "# memory\n" in runtime_kwargs["instructions"]
        assert any(isinstance(sink, MemoryRefreshSink) for sink in runtime_kwargs["event_sinks"])
        source_kwargs = mock_deps["load_instruction_sources"].call_args.kwargs
        assert source_kwargs["memory_store"] is not None
        assert source_kwargs["inject_memory"] is True

    @pytest.mark.asyncio
    async def test_disabled_memory_omits_tool_and_store(
        self,
        test_cfg,
        mock_adapter,
        mock_deps,
        tmp_path,
        monkeypatch,
    ):
        from hosts.shared.memory_refresh_sink import MemoryRefreshSink
        from hosts.web.run import _make_runtime_factory
        from infrastructure.config.models import EvolutionConfig, EvolutionMemoryConfig
        from tools import ToolRegistry

        monkeypatch.setenv("KONGMING_HOME", str(tmp_path))
        test_cfg.evolution = EvolutionConfig(
            memory=EvolutionMemoryConfig(enabled=False),
        )
        registry = ToolRegistry()
        mock_deps["build_default_registry"].return_value = registry

        factory = _make_runtime_factory(test_cfg)
        await factory("thread-memory-disabled", "test-local", mock_adapter, [])

        assert "memory" not in registry.names()
        runtime_kwargs = mock_deps["SessionEngine"].build.call_args.kwargs
        assert "memory" not in runtime_kwargs["enabled_tool_names"]
        assert all(
            not isinstance(sink, MemoryRefreshSink) for sink in runtime_kwargs["event_sinks"]
        )
        source_kwargs = mock_deps["load_instruction_sources"].call_args.kwargs
        assert source_kwargs["memory_store"] is None


class TestApprovalWiring:
    """smart-approval-manager-stage1：generic_chat web 装配点 prompt_fn 由
    ``make_manager_prompt_fn(manager, thread_id)`` 提供（不再是 adapter.prompt_approval）。

    阶段 1 改造点：把 generic_chat 通道审批从 WebHostAdapter 旧模态切到 ApprovalManager
    单例 + InboxEventSink → 浮窗。详见 docs/safety-approval-manager-v0.5/。
    """

    @pytest.mark.asyncio
    async def test_interactive_mode_passes_manager_prompt_fn(
        self, test_cfg, mock_adapter, mock_deps
    ):
        """验证 build_default_approval 收到的是 make_manager_prompt_fn 工厂返回的闭包。"""
        from hosts.web.run import _make_runtime_factory

        factory = _make_runtime_factory(test_cfg)
        await factory("thread-1", "test-local", mock_adapter, [])

        # 不再断言具体 prompt_fn 引用（manager 工厂返回闭包每次新建）；
        # 只断言：调用过一次 + mode='interactive' + 传了某个 callable 作 prompt_fn
        mock_deps["build_default_approval"].assert_called_once()
        call = mock_deps["build_default_approval"].call_args
        assert call.args == ("interactive",)
        assert callable(call.kwargs.get("prompt_fn"))
        # 关键回归：装配后 prompt_fn 必须存在（不能 None；interactive 模式下必须有人接审批）
        assert call.kwargs["prompt_fn"] is not None


class TestHostDispatcher:
    """Verify HostDispatcher 构造参数(runtime/session_id/queued_result_handler)。"""

    @pytest.mark.asyncio
    async def test_host_dispatcher_constructed_with_runtime_session_id(
        self, test_cfg, mock_adapter, mock_deps
    ):
        from hosts.web.run import _make_runtime_factory

        factory = _make_runtime_factory(test_cfg)
        await factory("thread-abc", "test-local", mock_adapter, [])

        mock_deps["HostDispatcher"].assert_called_once_with(
            runtime=mock_deps["SessionEngine"].build.return_value,
            session_id="thread-abc",
            queued_result_handler=mock_adapter.render_result,
            agent_tree_runtime_router=factory._agent_tree_runtime_router,
            approval_canceller=None,
        )


class TestLifecycleHooks:
    """Lifecycle hook registration from web runtime factory."""

    @pytest.mark.asyncio
    async def test_evolution_hook_registered_after_runtime_build(
        self, test_cfg, mock_adapter, mock_deps, tmp_path
    ):
        import evolution.lifecycle as evolution_lifecycle
        from hosts.web.run import _make_runtime_factory

        class _EvolutionManagerStub:
            enabled = True
            private_tool_names = frozenset({"evolution_write"})

            def register_runtime_tools(self, _registry, *, event_sinks=()):
                del event_sinks
                return True

            def enabled_tool_names(self, tool_names, *, lifecycle_bound):
                from evolution.evolution_manager import EvolutionManager

                return EvolutionManager.filter_runtime_tool_names(
                    tool_names,
                    lifecycle_bound=lifecycle_bound,
                )

        manager = _EvolutionManagerStub()
        app = SimpleNamespace(
            state=SimpleNamespace(
                workspace_root=tmp_path,
                kongming_home=tmp_path,
                evolution_manager=manager,
            ),
        )
        factory = _make_runtime_factory(test_cfg)
        setattr(factory, "_app", app)
        registration_calls = []

        def _capture_lifecycle_registration(*, runtime, manager):  # type: ignore[no-untyped-def]
            registration_calls.append((runtime, manager))
            return True

        with patch.object(
            evolution_lifecycle,
            "register_evolution_lifecycle_hook",
            _capture_lifecycle_registration,
        ):
            await factory("thread-evolution", "test-local", mock_adapter, [])

        runtime = mock_deps["SessionEngine"].build.return_value
        assert registration_calls == [(runtime, manager)]
        runtime.add_lifecycle_hook.assert_not_called()

    @pytest.mark.asyncio
    async def test_web_main_registers_public_review_tool_and_scheduler_omits_it(
        self,
        test_cfg,
        mock_adapter,
        mock_deps,
        tmp_path,
    ):
        from evolution.evolution_manager import EvolutionManager
        from hosts.web.run import _make_runtime_factory
        from infrastructure.config.models import EvolutionMemoryConfig
        from tools import ToolRegistry

        test_cfg.evolution = test_cfg.evolution.model_copy(
            update={
                "memory": EvolutionMemoryConfig(enabled=False),
                "learning": test_cfg.evolution.learning.model_copy(
                    update={
                        "enabled": True,
                        "auto_trigger_enabled": False,
                        "root_path": str(tmp_path / "evolution"),
                    }
                ),
            }
        )
        test_cfg.scheduler.enabled = True
        registry = ToolRegistry()
        mock_deps["build_default_registry"].return_value = registry
        manager = EvolutionManager(config=test_cfg, kongming_home=tmp_path)
        app = SimpleNamespace(
            state=SimpleNamespace(
                workspace_root=tmp_path,
                kongming_home=tmp_path,
                evolution_manager=manager,
            )
        )
        factory = _make_runtime_factory(test_cfg)
        setattr(factory, "_app", app)

        await factory("thread-evolution-tools", "test-local", mock_adapter, [])

        assert "request_evolution_review" in registry.names()
        assert "evolution_write" in registry.names()
        runtime_kwargs = mock_deps["SessionEngine"].build.call_args.kwargs
        assert "request_evolution_review" in runtime_kwargs["enabled_tool_names"]
        assert "evolution_write" not in runtime_kwargs["enabled_tool_names"]
        scheduler_factory = getattr(factory, "_scheduler_runtime_factory")
        with patch("scheduler.runtime_factory.build_scheduled_run_manager") as mock_bridge:
            scheduler_factory(object())
        scheduler_names = mock_bridge.call_args.kwargs["enabled_tool_names"]
        assert "request_evolution_review" not in scheduler_names
        assert "evolution_write" not in scheduler_names
        await manager.aclose()


class TestPluginToolEnableState:
    """Web runtime factory 按插件 enabled bool 创建新 runtime 工具白名单。"""

    @pytest.mark.asyncio
    async def test_new_runtime_reads_latest_plugin_enabled_bool(
        self, test_cfg, mock_adapter, mock_deps, tmp_path
    ):
        from hosts.web.plugin_management import PluginManagementManager, PluginToolStateStore
        from hosts.web.run import _make_runtime_factory

        mcp_tool = SimpleNamespace(
            name="mcp__minimax__web_search",
            description="Search with MiniMax MCP",
            metadata={
                "server_id": "minimax",
                "mcp_tool_name": "web_search",
                "canonical_name": "mcp__minimax__web_search",
                "kongming_tool_name": "mcp__minimax__web_search",
                "is_alias": False,
                "title": "Web Search",
            },
        )
        mock_deps["registry"].names.return_value = [
            "read_file",
            "mcp__minimax__web_search",
            "evolution_write",
        ]
        mock_deps["registry"].__iter__.return_value = iter([mcp_tool])
        manager = PluginManagementManager(PluginToolStateStore(tmp_path / "plugin-tools.json"))
        app = SimpleNamespace(
            state=SimpleNamespace(
                workspace_root=tmp_path,
                kongming_home=tmp_path,
                plugin_management_manager=manager,
            )
        )

        factory = _make_runtime_factory(test_cfg)
        setattr(factory, "_app", app)

        await factory("thread-1", "test-local", mock_adapter, [])
        first_kwargs = mock_deps["SessionEngine"].build.call_args.kwargs
        assert first_kwargs["enabled_tool_names"] == [
            "read_file",
            "mcp__minimax__web_search",
        ]

        manager.set_enabled("mcp__minimax__web_search", False)
        await factory("thread-2", "test-local", mock_adapter, [])
        second_kwargs = mock_deps["SessionEngine"].build.call_args.kwargs
        assert second_kwargs["enabled_tool_names"] == ["read_file"]

    @pytest.mark.asyncio
    async def test_management_sync_refreshes_plugin_store_without_building_runtime(
        self, test_cfg, mock_adapter, mock_deps, tmp_path
    ):
        from hosts.web.plugin_management import PluginManagementManager, PluginToolStateStore
        from hosts.web.run import _make_runtime_factory

        mcp_tool = SimpleNamespace(
            name="mcp__minimax__web_search",
            description="Search with MiniMax MCP",
            metadata={
                "server_id": "minimax",
                "mcp_tool_name": "web_search",
                "canonical_name": "mcp__minimax__web_search",
                "kongming_tool_name": "mcp__minimax__web_search",
                "is_alias": False,
                "title": "Web Search",
            },
        )
        mock_deps["registry"].names.return_value = [
            "read_file",
            "mcp__minimax__web_search",
            "evolution_write",
        ]
        mock_deps["registry"].__iter__.return_value = iter([mcp_tool])
        manager = PluginManagementManager(PluginToolStateStore(tmp_path / "plugin-tools.json"))
        app = SimpleNamespace(
            state=SimpleNamespace(
                workspace_root=tmp_path,
                kongming_home=tmp_path,
                plugin_management_manager=manager,
            )
        )

        factory = _make_runtime_factory(test_cfg)
        setattr(factory, "_app", app)

        await factory.sync_plugin_tools_for_management()

        plugins = manager.list_registered_plugins()
        assert [plugin.id for plugin in plugins] == ["mcp__minimax__web_search"]
        mock_deps["SessionEngine"].build.assert_not_called()


class TestInstructionsCaching:
    """Verify instructions are loaded once and cached."""

    @staticmethod
    def _app_for_cwds(
        *,
        entries: dict[str, str],
        workspace_root,
    ) -> SimpleNamespace:
        metas = [SimpleNamespace(id=thread_id, cwd=cwd) for thread_id, cwd in entries.items()]
        thread_manager = SimpleNamespace(list_threads=lambda: list(metas))
        return SimpleNamespace(
            state=SimpleNamespace(
                thread_manager=thread_manager,
                workspace_root=workspace_root,
            )
        )

    @staticmethod
    def _runtime_sources_for_cwd():
        from prompting.instructions.instruction_loader import InstructionSource

        async def _fake_sources(**kwargs):
            cwd = kwargs["cwd"]
            return [
                InstructionSource(origin="runtime", content=f"runtime cwd={cwd}"),
                InstructionSource(origin="agent_spec", content="test instructions"),
            ]

        return _fake_sources

    @pytest.mark.asyncio
    async def test_instructions_loaded_once_for_multiple_cells(
        self, test_cfg, mock_adapter, mock_deps
    ):
        from hosts.web.run import _make_runtime_factory

        factory = _make_runtime_factory(test_cfg)

        await factory("thread-1", "test-local", mock_adapter, [])
        await factory("thread-2", "test-local", mock_adapter, [])

        mock_deps["assemble_instructions"].assert_not_called()
        assert mock_deps["load_instruction_sources"].call_count == 2
        assert mock_deps["SessionEngine"].build.call_count == 2

    @pytest.mark.asyncio
    async def test_runtime_instructions_include_workflow_catalog(
        self, test_cfg, mock_adapter, mock_deps
    ):
        from hosts.web.run import _make_runtime_factory

        factory = _make_runtime_factory(test_cfg)
        await factory("thread-1", "test-local", mock_adapter, [])

        _args, kwargs = mock_deps["SessionEngine"].build.call_args
        instructions = kwargs["instructions"]
        assert "# workflow_catalog" in instructions
        assert "describe_agent_workflow_strategy" in instructions
        assert "run_agent_workflow" in instructions
        assert "# runtime\nruntime context" in instructions
        assert "# agent_spec\ntest instructions" in instructions

        cache_key = getattr(factory, "_instructions_cache_key")
        assert cache_key == "sha256:" + hashlib.sha256(instructions.encode()).hexdigest()

    @pytest.mark.asyncio
    async def test_stable_base_instruction_hash_keeps_shared_assets_cached(
        self, test_cfg, mock_adapter, mock_deps
    ):
        from hosts.web.run import _make_runtime_factory

        factory = _make_runtime_factory(test_cfg)
        await factory("thread-1", "test-local", mock_adapter, [])
        await factory("thread-2", "test-local", mock_adapter, [])

        assert mock_deps["assemble_instructions"].call_count == 0
        assert mock_deps["load_instruction_sources"].call_count == 2
        _args, kwargs = mock_deps["SessionEngine"].build.call_args
        assert "# runtime\nruntime context" in kwargs["instructions"]
        assert "# agent_spec\ntest instructions" in kwargs["instructions"]
        assert "# workflow_catalog" in kwargs["instructions"]

    @pytest.mark.asyncio
    async def test_runtime_instructions_use_thread_cwd_when_metadata_set(
        self, test_cfg, mock_adapter, mock_deps, tmp_path
    ):
        from hosts.web.run import _make_runtime_factory

        project_dir = tmp_path / "project-a"
        project_dir.mkdir()
        fallback_root = tmp_path / "fallback"
        fallback_root.mkdir()
        mock_deps["load_instruction_sources"].side_effect = self._runtime_sources_for_cwd()

        factory = _make_runtime_factory(test_cfg)
        setattr(
            factory,
            "_app",
            self._app_for_cwds(
                entries={"thread-project": str(project_dir)},
                workspace_root=fallback_root,
            ),
        )
        await factory("thread-project", "test-local", mock_adapter, [])

        _args, kwargs = mock_deps["SessionEngine"].build.call_args
        assert f"# runtime\nruntime cwd={project_dir}" in kwargs["instructions"]
        assert kwargs["tool_context_metadata"] == {"cwd": str(project_dir)}
        assert mock_deps["load_instruction_sources"].call_args.kwargs["cwd"] == str(project_dir)
        assert mock_deps["load_skill_specs"].call_args.kwargs["workspace"] == project_dir.resolve()

    @pytest.mark.asyncio
    async def test_runtime_instructions_fall_back_to_workspace_root_when_thread_cwd_empty(
        self, test_cfg, mock_adapter, mock_deps, tmp_path
    ):
        from hosts.web.run import _make_runtime_factory

        fallback_root = tmp_path / "kongming-home"
        fallback_root.mkdir()
        expected_cwd = fallback_root.resolve().as_posix()
        mock_deps["load_instruction_sources"].side_effect = self._runtime_sources_for_cwd()

        factory = _make_runtime_factory(test_cfg)
        setattr(
            factory,
            "_app",
            self._app_for_cwds(
                entries={"thread-empty-cwd": ""},
                workspace_root=fallback_root,
            ),
        )
        await factory("thread-empty-cwd", "test-local", mock_adapter, [])

        _args, kwargs = mock_deps["SessionEngine"].build.call_args
        assert f"# runtime\nruntime cwd={expected_cwd}" in kwargs["instructions"]
        assert kwargs["tool_context_metadata"] == {"cwd": expected_cwd}
        assert mock_deps["load_instruction_sources"].call_args.kwargs["cwd"] == expected_cwd
        assert (
            mock_deps["load_skill_specs"].call_args.kwargs["workspace"] == fallback_root.resolve()
        )

    @pytest.mark.asyncio
    async def test_runtime_instructions_do_not_bleed_between_thread_cwds(
        self, test_cfg, mock_adapter, mock_deps, tmp_path
    ):
        from hosts.web.run import _make_runtime_factory

        project_a = tmp_path / "project-a"
        project_b = tmp_path / "project-b"
        project_a.mkdir()
        project_b.mkdir()
        mock_deps["load_instruction_sources"].side_effect = self._runtime_sources_for_cwd()

        factory = _make_runtime_factory(test_cfg)
        setattr(
            factory,
            "_app",
            self._app_for_cwds(
                entries={"thread-a": str(project_a), "thread-b": str(project_b)},
                workspace_root=tmp_path,
            ),
        )

        await factory("thread-a", "test-local", mock_adapter, [])
        instructions_a = mock_deps["SessionEngine"].build.call_args.kwargs["instructions"]
        await factory("thread-b", "test-local", mock_adapter, [])
        instructions_b = mock_deps["SessionEngine"].build.call_args.kwargs["instructions"]

        assert f"runtime cwd={project_a}" in instructions_a
        assert f"runtime cwd={project_b}" in instructions_b
        assert f"runtime cwd={project_b}" not in instructions_a
        assert f"runtime cwd={project_a}" not in instructions_b

    @pytest.mark.asyncio
    async def test_scheduler_runtime_factory_keeps_own_thread_instructions(
        self, test_cfg, mock_adapter, mock_deps, tmp_path
    ):
        from hosts.web.run import _make_runtime_factory

        project_a = tmp_path / "project-a"
        project_b = tmp_path / "project-b"
        project_a.mkdir()
        project_b.mkdir()
        mock_deps["load_instruction_sources"].side_effect = self._runtime_sources_for_cwd()

        factory = _make_runtime_factory(test_cfg)
        setattr(
            factory,
            "_app",
            self._app_for_cwds(
                entries={"thread-a": str(project_a), "thread-b": str(project_b)},
                workspace_root=tmp_path,
            ),
        )
        await factory("thread-a", "test-local", mock_adapter, [])
        scheduler_factory_a = getattr(factory, "_scheduler_runtime_factory")

        await factory("thread-b", "test-local", mock_adapter, [])

        with patch("scheduler.runtime_factory.build_scheduled_run_manager") as mock_bridge:
            scheduler_factory_a(object())

        _args, kwargs = mock_bridge.call_args
        assert f"runtime cwd={project_a}" in kwargs["instructions"]
        assert f"runtime cwd={project_b}" not in kwargs["instructions"]

    @pytest.mark.asyncio
    async def test_base_instructions_hash_change_refreshes_cache(
        self, test_cfg, mock_adapter, mock_deps
    ):
        from hosts.web.run import _make_runtime_factory
        from prompting.instructions.instruction_loader import InstructionSource

        base_one = [
            InstructionSource(origin="runtime", content="base-one"),
            InstructionSource(origin="workflow_catalog", content="workflow-one"),
        ]
        base_two = [
            InstructionSource(origin="runtime", content="base-two"),
            InstructionSource(origin="workflow_catalog", content="workflow-two"),
        ]
        mock_deps["load_instruction_sources"].side_effect = [
            base_one,
            base_two,
        ]
        factory = _make_runtime_factory(test_cfg)
        await factory("thread-1", "test-local", mock_adapter, [])
        await factory("thread-2", "test-local", mock_adapter, [])

        mock_deps["assemble_instructions"].assert_not_called()
        assert mock_deps["load_instruction_sources"].call_count == 2
        assert getattr(factory, "_instructions_cache_key") == (
            "sha256:"
            + hashlib.sha256(b"# runtime\nbase-two\n\n# workflow_catalog\nworkflow-two").hexdigest()
        )
        _args, kwargs = mock_deps["SessionEngine"].build.call_args
        assert "# runtime\nbase-two" in kwargs["instructions"]
        assert "# workflow_catalog\nworkflow-two" in kwargs["instructions"]


class TestSchedulerRuntimeFactory:
    """Verify shared scheduler runtime factory inherits the same tool wiring."""

    @pytest.mark.asyncio
    async def test_scheduler_runtime_factory_forwards_tools_and_enabled_names(
        self, test_cfg, mock_adapter, mock_deps
    ):
        from hosts.web.run import _make_runtime_factory

        test_cfg.scheduler.enabled = True
        factory = _make_runtime_factory(test_cfg)
        await factory("thread-1", "test-local", mock_adapter, [])

        scheduler_factory = getattr(factory, "_scheduler_runtime_factory", None)
        assert scheduler_factory is not None

        with patch("scheduler.runtime_factory.build_scheduled_run_manager") as mock_bridge:
            dummy_store = object()
            scheduler_factory(dummy_store)

        mock_bridge.assert_called_once()
        _, kwargs = mock_bridge.call_args
        assert kwargs["tools"] is mock_deps["registry"]
        assert kwargs["enabled_tool_names"] == ["read_file", "shell"]
        assert "# workflow_catalog" in kwargs["instructions"]
        assert "# agent_spec\ntest instructions" in kwargs["instructions"]

    @pytest.mark.asyncio
    async def test_scheduler_approval_factory_returns_interactive_leaf_for_thread_tasks(
        self, test_cfg, mock_adapter, mock_deps
    ):
        from hosts.web.run import _make_runtime_factory

        test_cfg.scheduler.enabled = True
        factory = _make_runtime_factory(test_cfg)
        await factory("thread-1", "test-local", mock_adapter, [])

        scheduler_factory = getattr(factory, "_scheduler_runtime_factory", None)
        assert scheduler_factory is not None

        with patch("scheduler.runtime_factory.build_scheduled_run_manager") as mock_bridge:
            scheduler_factory(object())

        _, kwargs = mock_bridge.call_args
        approval_factory = kwargs["interactive_approval_factory"]
        task = SimpleNamespace(thread_id="thread-cron", delivery=None)

        mock_deps["build_default_approval"].reset_mock()
        mock_deps["build_safety_chain"].reset_mock()

        approval = approval_factory(task)

        assert approval is mock_deps["build_default_approval"].return_value
        mock_deps["build_default_approval"].assert_called_once()
        call = mock_deps["build_default_approval"].call_args
        assert call.args == ("interactive",)
        assert callable(call.kwargs["prompt_fn"])
        mock_deps["build_safety_chain"].assert_not_called()

    @pytest.mark.asyncio
    async def test_scheduler_approval_factory_without_thread_uses_bridge_default(
        self, test_cfg, mock_adapter, mock_deps
    ):
        from hosts.web.run import _make_runtime_factory

        test_cfg.scheduler.enabled = True
        factory = _make_runtime_factory(test_cfg)
        await factory("thread-1", "test-local", mock_adapter, [])

        scheduler_factory = getattr(factory, "_scheduler_runtime_factory", None)
        assert scheduler_factory is not None

        with patch("scheduler.runtime_factory.build_scheduled_run_manager") as mock_bridge:
            scheduler_factory(object())

        _, kwargs = mock_bridge.call_args
        approval_factory = kwargs["interactive_approval_factory"]

        mock_deps["build_default_approval"].reset_mock()
        mock_deps["build_safety_chain"].reset_mock()

        approval = approval_factory(SimpleNamespace(thread_id="", delivery=None))

        assert approval is None
        mock_deps["build_default_approval"].assert_not_called()
        mock_deps["build_safety_chain"].assert_not_called()


class TestEventSinks:
    """Verify event sinks are passed through correctly."""

    @pytest.mark.asyncio
    async def test_sinks_passed_to_runtime(self, test_cfg, mock_adapter, mock_deps):
        from hosts.web.run import _make_runtime_factory
        from infrastructure.tracing import JsonlTraceSink

        factory = _make_runtime_factory(test_cfg)
        mock_sink = MagicMock()
        test_cfg.session = test_cfg.session.model_copy(
            update={"file_store_path": ".kongming/custom-sessions"}
        )

        await factory("thread-1", "test-local", mock_adapter, [mock_sink])

        # Sinks list 包含调用方传入的 mock_sink + factory 自己装配的 JsonlTraceSink
        # （按 thread_id 拆目录，落到对应 session 目录内的 trace.jsonl）
        call_kwargs = mock_deps["SessionEngine"].build.call_args
        passed_sinks = call_kwargs[1]["event_sinks"]
        assert mock_sink in passed_sinks
        jsonl_sinks = [s for s in passed_sinks if isinstance(s, JsonlTraceSink)]
        assert len(jsonl_sinks) == 1
        # 验证 trace 进入 thread 的 session 目录
        assert jsonl_sinks[0].output_path.parts[-3:] == (
            "custom-sessions",
            "thread-1",
            "trace.jsonl",
        )

    @pytest.mark.asyncio
    async def test_jsonl_sink_path_per_thread_isolation(self, test_cfg, mock_adapter, mock_deps):
        """两个不同 thread_id 各自分配独立的 JsonlTraceSink，文件路径不冲突。"""
        from hosts.web.run import _make_runtime_factory
        from infrastructure.tracing import JsonlTraceSink

        factory = _make_runtime_factory(test_cfg)

        await factory("thread-aaa111bbb222", "test-local", mock_adapter, [])
        sinks_a = mock_deps["SessionEngine"].build.call_args[1]["event_sinks"]
        path_a = next(s for s in sinks_a if isinstance(s, JsonlTraceSink)).output_path

        await factory("thread-ccc333ddd444", "test-local", mock_adapter, [])
        sinks_b = mock_deps["SessionEngine"].build.call_args[1]["event_sinks"]
        path_b = next(s for s in sinks_b if isinstance(s, JsonlTraceSink)).output_path

        assert path_a != path_b
        assert path_a.parts[-3:] == ("sessions", "thread-aaa111bbb222", "trace.jsonl")
        assert path_b.parts[-3:] == ("sessions", "thread-ccc333ddd444", "trace.jsonl")
        # 父目录按 thread 隔离
        assert path_a.parent != path_b.parent
