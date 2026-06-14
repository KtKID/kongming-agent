"""Web AgentWorkflowManager Deep Research 绑定单元测试。

本脚本验证 Web thread 装配函数把 deep_research source provider 注入 AgentWorkflowManager。
作用是固定 `_bind_agent_workflow_manager` 与 Web provider factory 的边界，保证 Web 运行时能把用户搜索 provider 交给 workflow manager。
关键执行流程：monkeypatch Web provider 构造函数返回哨兵 provider，调用绑定函数，断言 handle 收到的 manager 持有该 provider；
同时通过 runtime factory 薄链路测试覆盖真实工具注册、provider factory 构造和 workflow handle 绑定。
关键函数：test_bind_agent_workflow_manager_injects_deep_research_source_provider 覆盖绑定函数；
test_make_runtime_factory_binds_deep_research_provider_from_tool_registry 覆盖 Web runtime factory 主路径。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from application.agent_workflows.strategies.deep_research.contracts import ResearchSourceQuery
from core.contracts import ToolContext, ToolResult
from hosts.web import research_source_provider as provider_module
from hosts.web import run as web_run
from infrastructure.config.models import (
    ApprovalConfig,
    Config,
    LLMPresetConfig,
    ModelConfig,
    WebConfig,
)
from tools import ToolRegistry
from tools.agent_workflow_tool import AgentWorkflowHandle


class _CaptureHandle:
    """测试用 workflow handle，记录 bind 输入。"""

    def __init__(self) -> None:
        """初始化 handle，输入为空，输出为可记录绑定的实例。"""
        self.manager: Any | None = None
        self.session_id: str | None = None

    def bind(self, manager: Any, *, session_id: str | None = None) -> None:
        """记录绑定参数，输入为 manager 和 session_id，输出为内存状态。"""
        self.manager = manager
        self.session_id = session_id


class _ProviderSentinel:
    """测试用 provider 哨兵，标识 Web factory 的注入结果。"""

    name = "web_user_source"


def test_bind_agent_workflow_manager_injects_deep_research_source_provider(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """验证 Web 绑定，输入为 provider factory 哨兵，输出为 manager 持有同一 provider。"""
    provider = _ProviderSentinel()
    build_calls: list[Any] = []

    class _Factory:
        """测试用 factory，记录 config 并返回 provider 哨兵。"""

        def __init__(self, config: Any) -> None:
            """初始化 factory，输入为 config，输出为可 build 实例。"""
            build_calls.append(config)

        def build(self, tool_registry: Any) -> Any:
            """构造 provider，输入为 tool_registry，输出为 build result。"""
            build_calls.append(tool_registry)
            return SimpleResult(provider=provider)

    monkeypatch.setattr(provider_module, "WebResearchSourceProviderFactory", _Factory)
    handle = _CaptureHandle()
    tool_registry = object()
    config = _config(tmp_path)
    role_manager = object()

    web_run._bind_agent_workflow_manager(
        handle=handle,
        thread_id="thread-web-deep-research",
        runtime=object(),
        config=config,
        workspace_root=tmp_path,
        role_manager=role_manager,
        tool_registry=tool_registry,
    )

    assert handle.session_id == "thread-web-deep-research"
    assert handle.manager is not None
    assert handle.manager.deep_research_source_provider is provider
    assert handle.manager.deep_research_source_diagnostics is not None
    assert handle.manager.deep_research_source_diagnostics.reason == "ok"
    assert build_calls == [config, tool_registry]


@pytest.mark.asyncio
async def test_make_runtime_factory_binds_deep_research_provider_from_tool_registry(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """验证 Web runtime factory，输入为含 web_search 的 registry，输出为 manager 绑定真实 provider。"""
    home = tmp_path / "home"
    role_dir = home / "agent_roles"
    role_dir.mkdir(parents=True)
    registry = ToolRegistry([_RuntimeSearchTool()])
    bind_calls: list[tuple[Any, str | None]] = []

    async def fake_assemble_instructions(**_kwargs: Any) -> tuple[str, list[str]]:
        """返回稳定 instructions，输入为装配参数，输出为模板文本和来源。"""
        return "test instructions", ["test"]

    async def fake_load_skill_specs(*_args: Any, **_kwargs: Any) -> list[Any]:
        """返回空 skill 列表，输入为装载参数，输出为空列表。"""
        return []

    def fake_native_runtime_build(*_args: Any, **_kwargs: Any) -> _FakeRuntime:
        """构造 fake runtime，输入为 NativeRuntime.build 参数，输出为轻量 runtime。"""
        assert _kwargs["tools"] is registry
        assert "web_search" in _kwargs["enabled_tool_names"]
        return _FakeRuntime()

    original_bind = AgentWorkflowHandle.bind

    def capture_bind(
        self: AgentWorkflowHandle,
        manager: Any,
        *,
        session_id: str | None = None,
    ) -> None:
        """记录 workflow handle 绑定，输入为 manager 和 session_id，输出为捕获列表。"""
        bind_calls.append((manager, session_id))
        original_bind(self, manager, session_id=session_id)

    monkeypatch.setattr("infrastructure.config.paths.get_kongming_home", lambda: home)
    monkeypatch.setattr(
        "prompting.instructions.instruction_loader.assemble_instructions",
        fake_assemble_instructions,
    )
    monkeypatch.setattr(
        "prompting.skills.skill_loader.load_skill_specs",
        fake_load_skill_specs,
    )
    monkeypatch.setattr(
        "prompting.skills.skill_loader.format_skill_listing",
        lambda _specs: "",
    )
    monkeypatch.setattr("tools.build_default_registry", lambda **_kwargs: registry)
    monkeypatch.setattr("tools.register_schedule_tool_if_enabled", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "tools.register_evolution_write_tool_if_enabled",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("tools.register_task_progress_tool", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "runtime_assembly.native_runtime.NativeRuntime.build",
        staticmethod(fake_native_runtime_build),
    )
    monkeypatch.setattr("hosts.shared.session_bridge.SessionBridge", _FakeBridge)
    monkeypatch.setattr(AgentWorkflowHandle, "bind", capture_bind)

    factory = web_run._make_runtime_factory(_web_runtime_config(tmp_path))
    runtime, bridge = await factory("thread-runtime-factory", "local", object(), ())

    assert isinstance(runtime, _FakeRuntime)
    assert isinstance(bridge, _FakeBridge)
    assert bind_calls
    manager, session_id = bind_calls[-1]
    assert session_id == "thread-runtime-factory"
    provider = manager.deep_research_source_provider
    assert provider is not None
    assert manager.deep_research_source_diagnostics.reason == "ok"
    assert manager.deep_research_source_diagnostics.search_tool_name == "web_search"

    candidates = await provider.search(
        ResearchSourceQuery(
            query_id="q-runtime",
            line="runtime factory deep research",
            intent="overview",
            max_results=1,
        )
    )
    assert len(candidates) == 1
    assert candidates[0].url == "https://example.com/runtime-provider"
    assert candidates[0].provider_name == "web_user_tool_research_source"


class SimpleResult:
    """测试用 build result，模拟 Web factory 返回值。"""

    def __init__(self, *, provider: Any) -> None:
        """初始化结果，输入为 provider，输出含 diagnostics 的对象。"""
        self.provider = provider
        self.diagnostics = SimpleDiagnostics()


class SimpleDiagnostics:
    """测试用 diagnostics，提供绑定函数日志读取字段。"""

    provider_name = "web_user_source"
    search_tool_name = "web_search"
    fetch_tool_name = "web_fetch"
    reason = "ok"
    fallback_reason = None


def _config(tmp_path: Path) -> Config:
    """构造测试配置，输入为临时目录，输出为 Config。"""
    cfg = Config(
        model=ModelConfig(
            name="fake-model",
            base_url="http://127.0.0.1:1234/v1",
            api_key="",
        )
    )
    cfg.session.file_store_path = str(tmp_path / "sessions")
    return cfg


def _web_runtime_config(tmp_path: Path) -> Config:
    """构造 Web runtime factory 配置，输入为临时目录，输出含本地 preset 的 Config。"""
    cfg = Config(
        model=ModelConfig(
            name="base-model",
            base_url="http://127.0.0.1:1234/v1",
            api_key="",
        ),
        approval=ApprovalConfig(mode="auto_allow"),
        web=WebConfig(
            llm_presets=[
                LLMPresetConfig(
                    id="local",
                    display_name="Local",
                    base_url="http://127.0.0.1:1234/v1",
                    model="preset-model",
                )
            ]
        ),
    )
    cfg.session.file_store_path = str(tmp_path / "sessions")
    return cfg


class _RuntimeSearchTool:
    """runtime factory 测试用搜索工具。"""

    name = "web_search"
    description = "fake web search for deep research"
    input_schema = {"type": "object", "properties": {"query": {"type": "string"}}}

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """返回稳定搜索结果，输入为查询参数和上下文，输出为 ToolResult。"""
        return ToolResult(
            ok=True,
            content="found runtime provider source",
            data={
                "results": [
                    {
                        "url": "https://example.com/runtime-provider",
                        "title": "Runtime Provider",
                        "snippet": f"matched {args.get('query', '')}",
                    }
                ]
            },
        )


class _FakeRuntime:
    """runtime factory 测试用 runtime。"""


class _FakeBridge:
    """runtime factory 测试用 SessionBridge。"""

    def __init__(
        self,
        *,
        runtime: Any,
        adapter: Any,
        session_id: str,
        echo_final_content: bool,
    ) -> None:
        """记录桥接参数，输入为 runtime、adapter、session_id，输出为实例属性。"""
        self.runtime = runtime
        self.adapter = adapter
        self.session_id = session_id
        self.echo_final_content = echo_final_content
