"""Web thread Deep Research smoke 测试。

本脚本覆盖 Web REST/WS 入口到 deep_research workflow artifact 的薄端到端链路。
作用是验证浏览器同源链路中的创建 thread、连接 generic WebSocket、触发 thread runtime、
构造 Web source provider、绑定 AgentWorkflowManager 和写入 workflow 产物。
关键执行流程：TestClient 创建 thread，经 `/ws/threads/{thread_id}` 发送 user.input，
测试 bridge 触发 deep_research，最后读取 result/audit/sources/report 断言来源可追踪。
关键函数：test_web_thread_deep_research_uses_user_search_provider 覆盖用户搜索工具路径；
test_web_thread_deep_research_missing_search_tool_records_fallback 覆盖缺失工具 fallback 路径。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from core.contracts import ToolContext, ToolResult
from hosts.web import run as web_run
from hosts.web.app import create_app
from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from hosts.web.auth.secrets import hash_password
from hosts.web.threads.manager import ThreadManager
from infrastructure.config.models import (
    ApprovalConfig,
    Config,
    LLMPresetConfig,
    ModelConfig,
    SchedulerConfig,
    WebConfig,
    WorkflowConfig,
)
from network.manager import reset_network_manager_for_test
from tools import ToolRegistry
from tools.agent_workflow_tool import AgentWorkflowHandle

pytestmark = [pytest.mark.e2e, pytest.mark.smoke]

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


def test_web_thread_deep_research_uses_user_search_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 Web thread 用户搜索路径，输入为 fake web_search/web_fetch，输出为用户 URL artifact。"""
    harness = _make_web_harness(
        tmp_path,
        monkeypatch,
        registry=ToolRegistry([_WebSearchTool(), _WebFetchTool()]),
        payload=_provider_payload(),
    )

    workflow_dir = harness.run_thread_message("run provider deep research")
    board_dir = workflow_dir / "deep_research"
    result_payload = _json(workflow_dir / "result.json")
    audit_rows = _jsonl(workflow_dir / "audit.jsonl")
    sources = _jsonl(board_dir / "sources.jsonl")
    report_markdown = (board_dir / "report.md").read_text(encoding="utf-8")

    assert result_payload["deep_research"]["source_provider"] == "web_user_tool_research_source"
    assert sources[0]["provider_name"] == "web_user_tool_research_source"
    assert sources[0]["url"] == "https://example.com/web-thread-source"
    assert "https://example.com/web-thread-source" in report_markdown
    assert any(row["action"] == "deep_research.workflow_started" for row in audit_rows)


def test_web_thread_deep_research_missing_search_tool_records_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 Web thread 缺搜索工具路径，输入为空 registry，输出为 fallback diagnostics。"""
    harness = _make_web_harness(
        tmp_path,
        monkeypatch,
        registry=ToolRegistry(),
        payload={"topic": "Web thread missing search tool"},
    )

    workflow_dir = harness.run_thread_message("run missing-provider deep research")
    result_payload = _json(workflow_dir / "result.json")
    audit_rows = _jsonl(workflow_dir / "audit.jsonl")
    diagnostic_events = [
        row for row in audit_rows if row["action"] == "deep_research.source_provider_diagnostics"
    ]

    assert result_payload["deep_research"]["source_provider"] == "deterministic_research_source"
    diagnostics = result_payload["deep_research"]["source_provider_diagnostics"]
    assert diagnostics["reason"] == "search_tool_missing"
    assert diagnostics["fallback_reason"] == "no configured or default search tool is registered"
    assert diagnostic_events[0]["payload"]["reason"] == "search_tool_missing"
    assert "web_search" in diagnostic_events[0]["payload"]["missing_tools"]


def _make_web_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    registry: ToolRegistry,
    payload: dict[str, Any],
) -> _WebDeepResearchHarness:
    """构造 Web smoke harness，输入为 registry/payload，输出为可运行 thread 消息的对象。"""
    reset_network_manager_for_test()
    home = tmp_path / "kongming-home"
    _seed_password(home, "pwd")
    config = _config(tmp_path, home)
    state = _BridgeState(payload=payload)

    async def fake_assemble_instructions(**_kwargs: Any) -> tuple[str, list[str]]:
        """返回稳定 instructions，输入为装配参数，输出为模板文本和来源。"""
        return "web deep research smoke instructions", ["test"]

    async def fake_load_skill_specs(*_args: Any, **_kwargs: Any) -> list[Any]:
        """返回空 skill 列表，输入为装载参数，输出为空列表。"""
        return []

    def capture_bind(
        self: AgentWorkflowHandle,
        manager: Any,
        *,
        session_id: str | None = None,
    ) -> None:
        """记录 workflow manager，输入为 handle、manager、session_id，输出为共享状态。"""
        if session_id is not None:
            state.managers[session_id] = manager
        _ORIGINAL_AGENT_WORKFLOW_BIND(self, manager, session_id=session_id)

    def fake_native_runtime_build(*_args: Any, **_kwargs: Any) -> _RuntimeWithSessions:
        """构造测试 runtime，输入为 NativeRuntime.build 参数，输出带 _sessions 的 runtime。"""
        assert _kwargs["tools"] is registry
        return _RuntimeWithSessions()

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
    monkeypatch.setattr(
        "runtime_assembly.native_runtime.NativeRuntime.build",
        staticmethod(fake_native_runtime_build),
    )
    monkeypatch.setattr(
        "hosts.shared.session_bridge.SessionBridge",
        _bridge_class(state),
    )
    monkeypatch.setattr(AgentWorkflowHandle, "bind", capture_bind)

    runtime_factory = web_run._make_runtime_factory(config)
    thread_manager = ThreadManager(config, kongming_home=home, runtime_factory=runtime_factory)
    app = create_app(config, thread_manager, home_dir=home, lifespan_shutdown_timeout=1.0)
    setattr(runtime_factory, "_app", app)
    return _WebDeepResearchHarness(app=app, state=state, workspace_root=tmp_path)


class _WebDeepResearchHarness:
    """Web deep_research smoke 测试门面。"""

    def __init__(self, *, app: Any, state: _BridgeState, workspace_root: Path) -> None:
        """初始化 harness，输入为 app/state/workspace，输出为可执行实例。"""
        self._app = app
        self._state = state
        self._workspace_root = workspace_root

    def run_thread_message(self, text: str) -> Path:
        """创建 thread 并发送 WS 消息，输入为用户文本，输出为 workflow 目录。"""
        try:
            with TestClient(self._app) as client:
                login = client.post(
                    "/api/auth/login",
                    json={"password": "pwd"},
                    headers=CSRF_HEADERS,
                )
                assert login.status_code == 200
                created = client.post(
                    "/api/threads",
                    json={
                        "name": "deep research smoke",
                        "preset_id": "local",
                        "backend_kind": "generic_chat",
                        "cwd": str(self._workspace_root),
                    },
                    headers=CSRF_HEADERS,
                )
                assert created.status_code == 201
                thread_id = str(created.json()["id"])

                with client.websocket_connect(f"/ws/threads/{thread_id}") as ws:
                    history = ws.receive_json()
                    assert history["frame_type"] == "thread.history"
                    ws.send_json(
                        {
                            "frame_type": "user.input",
                            "text": text,
                            "request_id": "req-deep-research-smoke",
                        }
                    )
                    assert self._state.done.wait(timeout=10), (
                        "deep_research workflow did not finish"
                    )
        finally:
            reset_network_manager_for_test()

        if self._state.error is not None:
            raise AssertionError("deep_research workflow failed") from self._state.error
        assert self._state.workflow_dir is not None
        return self._state.workflow_dir


class _BridgeState:
    """测试 bridge 共享状态。"""

    def __init__(self, *, payload: dict[str, Any]) -> None:
        """初始化状态，输入为 workflow payload，输出为跨线程共享容器。"""
        self.payload = payload
        self.done = threading.Event()
        self.managers: dict[str, Any] = {}
        self.workflow_dir: Path | None = None
        self.error: BaseException | None = None


def _bridge_class(state: _BridgeState) -> type:
    """构造绑定共享状态的 bridge 类，输入为状态，输出为 SessionBridge 替身类。"""

    class _DeepResearchBridge:
        """Web SessionBridge 替身，用 deep_research workflow 替代外部 LLM。"""

        def __init__(
            self,
            *,
            runtime: Any,
            adapter: Any,
            session_id: str,
            echo_final_content: bool,
        ) -> None:
            """记录 bridge 参数，输入为 runtime/adapter/session，输出为实例属性。"""
            self.runtime = runtime
            self.adapter = adapter
            self.session_id = session_id
            self.echo_final_content = echo_final_content

        async def run_once(
            self,
            text: str,
            *,
            reasoning_effort: str | None = None,
            attachments: list[dict[str, Any]] | None = None,
        ) -> None:
            """执行 deep_research workflow，输入为用户消息，输出为 workflow artifact。"""
            del text, reasoning_effort, attachments
            try:
                manager = state.managers[self.session_id]
                result = await manager.run_workflow_payload(
                    mode="deep_research",
                    parent_session_id=self.session_id,
                    payload=state.payload,
                    desc="Web thread deep_research smoke",
                )
                state.workflow_dir = result.workflow_dir
            except BaseException as exc:
                state.error = exc
                raise
            finally:
                state.done.set()

    return _DeepResearchBridge


class _RuntimeWithSessions:
    """测试 runtime，提供 WS history 读取所需的 _sessions 字段。"""

    _sessions: dict[str, Any] = {}


class _WebSearchTool:
    """测试搜索工具，返回固定用户 URL。"""

    name = "web_search"
    description = "fake web search for deep research web smoke"
    input_schema = {"type": "object", "properties": {"query": {"type": "string"}}}

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """返回搜索结果，输入为查询参数和上下文，输出为 ToolResult。"""
        return ToolResult(
            ok=True,
            content="found web thread source",
            data={
                "results": [
                    {
                        "url": "https://example.com/web-thread-source",
                        "title": "Web Thread Source",
                        "snippet": f"matched {args.get('query', '')}",
                    }
                ]
            },
        )


class _WebFetchTool:
    """测试读取工具，返回固定正文。"""

    name = "web_fetch"
    description = "fake web fetch for deep research web smoke"
    input_schema = {"type": "object", "properties": {"url": {"type": "string"}}}

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """返回正文结果，输入为 URL 参数和上下文，输出为 ToolResult。"""
        return ToolResult(
            ok=True,
            content="fetched web thread source",
            data={
                "content_text": (
                    "Web thread source content says provider injection reaches REST and WS."
                ),
                "url": args.get("url"),
                "provider_name": "web_user_tool_research_source",
            },
        )


def _config(tmp_path: Path, home: Path) -> Config:
    """构造 Web smoke 配置，输入为临时路径和 home，输出为 Config。"""
    cfg = Config(
        model=ModelConfig(
            name="base-model",
            base_url="http://127.0.0.1:1234/v1",
            api_key="",
        ),
        approval=ApprovalConfig(mode="auto_allow"),
        scheduler=SchedulerConfig(enabled=False),
        workflow=WorkflowConfig(enabled=False),
        web=WebConfig(
            enabled=True,
            dev_mode=True,
            initial_password="pwd",
            idle_timeout_seconds=1800,
            idle_check_interval_seconds=60,
            llm_presets=[
                LLMPresetConfig(
                    id="local",
                    display_name="Local",
                    base_url="http://127.0.0.1:1234/v1",
                    model="preset-model",
                )
            ],
        ),
    )
    cfg.session.file_store_path = str(home / "sessions")
    cfg.trace.output_path = str(home / "traces" / "trace.jsonl")
    return cfg


def _provider_payload() -> dict[str, Any]:
    """构造用户 provider workflow payload，输入为空，输出为低预算 deep_research payload。"""
    return {
        "topic": "Web thread provider smoke",
        "source_queries": [
            {
                "query_id": "q-web",
                "line": "Web thread provider URL",
                "intent": "overview",
                "max_results": 1,
            }
        ],
        "limits": {
            "source_budget": 1,
            "fetch_budget": 1,
            "fact_cap": 2,
            "jury_size": 1,
            "reject_quorum": 1,
            "search_results_per_line": 1,
            "fetch_concurrency": 1,
            "jury_concurrency": 1,
            "workflow_timeout_seconds": 120,
        },
        "output_contract": "deep_research_report",
    }


def _seed_password(home: Path, password: str) -> None:
    """写入测试登录密码 hash，输入为 home 和明文密码，输出为 password.hash 文件。"""
    web_dir = home / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    (web_dir / "password.hash").write_text(hash_password(password), encoding="utf-8")


def _json(path: Path) -> dict[str, Any]:
    """读取 JSON 文件，输入为路径，输出为 dict。"""
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL 文件，输入为路径，输出为 dict 列表。"""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


_ORIGINAL_AGENT_WORKFLOW_BIND = AgentWorkflowHandle.bind
