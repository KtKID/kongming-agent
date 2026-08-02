import inspect
from pathlib import Path

import hosts.web.run as web_run
from core.contracts import ToolContext
from hosts.web.run import _register_agent_workflow_tools
from tools import build_default_registry


def test_web_runtime_registers_agent_workflow_tools(tmp_path: Path) -> None:
    registry = build_default_registry(
        file_enabled=True,
        shell_enabled=True,
    )

    role_manager, workflow_handle = _register_agent_workflow_tools(
        registry,
        role_dir=tmp_path / "agent_roles",
    )

    assert role_manager is not None
    assert role_manager._config_path is None  # type: ignore[attr-defined]
    assert role_manager.list_roles() == ()
    assert workflow_handle.manager is None
    assert "list_agent_roles" in registry.names()
    assert "create_agent_role" in registry.names()
    assert "run_agent_workflow" in registry.names()
    assert "run_parallel_subagents" in registry.names()


def test_web_runtime_loads_explicit_home_agent_config(tmp_path: Path) -> None:
    agent_config = tmp_path / "agent.toml"
    agent_config.write_text(
        """
[[agents]]
id = 1
nickname = "root"
model = "root-model"
role_desc = "Root home role."
reasoning_effort = "medium"
max_turns = 3
""".strip(),
        encoding="utf-8",
    )
    registry = build_default_registry(
        file_enabled=True,
        shell_enabled=True,
    )

    role_manager, _ = _register_agent_workflow_tools(
        registry,
        role_dir=tmp_path / "agent_roles",
        agent_config_path=agent_config,
    )

    assert [role.role_id for role in role_manager.list_roles()] == ["1"]


def test_web_runtime_factory_agent_config_materializes_home_result() -> None:
    source = inspect.getsource(web_run._make_runtime_factory)

    assert "agent_config_path=materialize_kongming_home_agent_config(home)," in source
    assert 'Path("config/agent.toml")' not in source


def test_agent_workflow_handle_uses_session_specific_manager(tmp_path: Path) -> None:
    registry = build_default_registry(
        file_enabled=True,
        shell_enabled=True,
    )
    _, workflow_handle = _register_agent_workflow_tools(
        registry,
        role_dir=tmp_path / "agent_roles",
    )
    default_manager = object()
    thread_manager = object()

    workflow_handle.bind(default_manager)
    workflow_handle.bind(thread_manager, session_id="thread-a")

    assert (
        workflow_handle.get(
            ToolContext(
                run_id="run-a",
                session_id="thread-a",
                turn=1,
                call_id="call-a",
            )
        )
        is thread_manager
    )
    assert (
        workflow_handle.get(
            ToolContext(
                run_id="run-b",
                session_id="thread-b",
                turn=1,
                call_id="call-b",
            )
        )
        is default_manager
    )
