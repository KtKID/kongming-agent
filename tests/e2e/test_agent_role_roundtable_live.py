"""agent role roundtable live e2e。

本脚本验证真实父 LLM 能按角色库流程发起 roundtable 编排。
作用是覆盖 `list_agent_roles -> create_agent_role -> run_agent_workflow`
这条真实 tool-call 链路，并确认 tool 与 workflow strategy 共享同一个
AgentRoleManager。
关键执行流程：加载 MiniMax M3 配置，注册角色工具和 workflow 工具，真实父 LLM
先列角色、创建角色，再用 `participants.select` 启动 roundtable；子 agent manager
使用 fake 输出控制成本和稳定性。
关键函数：test_minimax_parent_creates_agent_role_and_runs_roundtable 执行 live e2e。
test_minimax_parent_runs_real_us_iran_roundtable_and_writes_child_logs 执行真实子 agent e2e。

运行方式：
    KONGMING_E2E_REAL_MODEL=1 uv run pytest tests/e2e/test_agent_role_roundtable_live.py -v -s
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from application.agent_roles import AgentRoleManager
from application.agent_workflows.manager import AgentWorkflowManager
from application.subagents.manager import SubAgentManager, SubAgentRun
from core import AgentSpec, InMemorySession
from hosts.cli.main import _apply_model_preset_or_exit
from infrastructure.config import load_config
from runtime_assembly.native_runtime import NativeRuntime
from sessions import SessionBootstrap, build_session
from tools import (
    AutoAllowApproval,
    ToolRegistry,
    build_file_tools,
    register_agent_role_tool,
    register_agent_workflow_tool,
)
from tools.agent_workflow_tool import AgentWorkflowHandle

pytestmark = pytest.mark.skipif(
    os.getenv("KONGMING_E2E_REAL_MODEL") != "1",
    reason="set KONGMING_E2E_REAL_MODEL=1 to run real agent role roundtable e2e",
)


class _FakeRoundtableSubAgentManager:
    """测试用 roundtable 子 agent manager，按 stage 返回稳定结构化输出。"""

    def __init__(self) -> None:
        """初始化 fake manager，输入为空，输出为记录任务的实例。"""
        self.tasks: list[Any] = []

    async def run_task(
        self,
        *,
        workflow_id: str,
        parent_session_id: str,
        task: Any,
        audit_writer: Any,
    ) -> SubAgentRun:
        """运行 fake 子 agent，输入为任务 metadata，输出 completed SubAgentRun。"""
        del workflow_id, parent_session_id, audit_writer
        self.tasks.append(task)
        stage = str(task.metadata.get("roundtable_stage"))
        agent = str(task.metadata.get("roundtable_agent"))
        if stage == "independent":
            content = json.dumps(
                {
                    "agent": agent,
                    "findings": [
                        {
                            "severity": "P1",
                            "claim": f"{agent} 发现角色编排需要审计快照",
                            "evidence": [{"type": "doc", "path": "input/source/src/sample.py"}],
                            "risk": "缺少快照会让后续回放无法确认使用的角色",
                            "suggestion": "写入 roles.json",
                            "confidence": 0.8,
                        }
                    ],
                },
                ensure_ascii=False,
            )
        elif stage == "rebuttal":
            content = json.dumps(
                {
                    "agent": agent,
                    "comments": [
                        {
                            "type": "support",
                            "target_claim_id": "C-001",
                            "comment": f"{agent} 支持保留 roles.json 快照",
                            "evidence": [{"type": "doc", "path": "review_board/claims.jsonl"}],
                            "severity_adjustment": None,
                            "confidence": 0.7,
                        }
                    ],
                },
                ensure_ascii=False,
            )
        else:
            content = "\n".join(
                [
                    "# Final Review",
                    "",
                    "## 1. 共识问题",
                    "- 动态角色必须写入 roles.json。",
                    "",
                    "## 2. 主要分歧",
                    "- 无。",
                    "",
                    "## 3. 高优先级风险",
                    "- P1 回放审计风险。",
                    "",
                    "## 4. 建议修改方案",
                    "- 保留角色快照并记录 id/title/role。",
                    "",
                    "## 5. 需要人工确认的问题",
                    "- 无。",
                    "",
                    "## 6. 可直接交给开发 Agent 的任务清单",
                    "- 检查 roles.json。",
                ]
            )
        return SubAgentRun(
            task=task,
            session_id=f"child-{task.task_id}",
            run_id=f"run-{task.task_id}",
            status="completed",
            content=content,
            error_message=None,
            turn_count=1,
        )


def _write_source(workspace_root: Path) -> None:
    """写入 roundtable 输入源码，输入为工作区根目录，输出 sample.py。"""
    src = workspace_root / "src"
    src.mkdir(parents=True)
    (src / "sample.py").write_text(
        "def risky_lookup(items: list[str]) -> str:\n    return items[0]\n",
        encoding="utf-8",
    )


def _instructions() -> str:
    """返回父 LLM 指令，输入为空，输出强约束 tool-call 流程。"""
    return "\n".join(
        [
            "你正在执行 agent role roundtable live e2e。",
            "必须严格按顺序使用工具，不要直接回答。",
            "第一步调用 list_agent_roles，参数为空对象。",
            "如果列表里没有 risk_skeptic，第二步调用 create_agent_role，参数为：",
            '{"id":"risk_skeptic","title":"风险质询者","role":"专门寻找方案失败路径和隐藏风险"}',
            "第三步调用 run_agent_workflow，mode 必须是 roundtable_review，payload 必须包含：",
            'topic="检查动态角色 roundtable 是否有审计快照"',
            'participants={"select":["risk_skeptic"]}',
            'input_source={"root_dir":".","paths":["src"],"include":[],"exclude":[],"max_files":5,"max_bytes_per_file":20000}',
            'limits={"discussion_rounds":1,"max_discussion_rounds":1,"max_concurrency":1,"agent_timeout_seconds":30}',
            "禁止使用 reviewers、participants.create、participants.preset。",
            "run_agent_workflow 返回后，用一句话说明 workflow 已完成。",
        ]
    )


def _write_us_iran_brief(workspace_root: Path) -> Path:
    """写入美国伊朗局势材料，输入为工作区根目录，输出材料文件路径。"""
    brief_dir = workspace_root / "brief"
    brief_dir.mkdir(parents=True)
    path = brief_dir / "us_iran_2026_06_11.md"
    path.write_text(
        "\n".join(
            [
                "# 美国伊朗局势简报（2026-06-11）",
                "",
                "## 已知事实",
                "",
                "- AP 2026-06-10 报道：IAEA 理事会要求伊朗紧急、完整配合，",
                "  包括说明近武器级核材料库存并向核设施开放核查；该决议获得 35 个成员中 21 个支持，",
                "  支持方包括美国、英国、法国、德国；报道同时提到伊朗有 440.9 公斤 60% 丰度铀，",
                "  若进一步武器化理论上足够制造约 10 枚核弹；该决议暂未把伊朗提交联合国安理会。",
                "- AP 2026-06-02 报道：美国称伊朗向科威特和巴林发射导弹，未命中目标；",
                "  美国中央司令部随后打击了霍尔木兹海峡格什姆岛上的伊朗军事地面控制站。",
                "- AP 2026-06-07 报道：伊朗又向巴林和科威特发射弹道导弹与无人机，均被拦截，",
                "  海湾脆弱停火继续承压。",
                "- The Guardian 2026-06-09 报道：美国副总统 JD Vance 称美国与伊朗“非常接近”达成和平协议，",
                "  但同日报道也描述美国在伊朗击落 Apache 直升机后发动报复性打击，外交窗口与军事升级并行。",
                "",
                "## 分析任务",
                "",
                "围绕升级风险、外交窗口、误判风险做圆桌讨论。",
                "请区分：已经被来源明确支持的事实、合理推断、需要继续确认的信息。",
                "",
                "## 来源链接",
                "",
                "- https://apnews.com/article/iran-nuclear-material-access-resolution-vote-iaea-b8050494bc01a2e596a3a59952bfc8eb",
                "- https://apnews.com/article/iran-us-israel-war-2-june-2026-9bde9a3425d4b9ff70f157bdae0fb982",
                "- https://apnews.com/article/iran-us-bahrain-kuwait-missiles-drones-df859624fb659cb28cec798200cc85d4",
                "- https://www.theguardian.com/us-news/2026/jun/09/jd-vance-iran-peace-deal",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _us_iran_instructions() -> str:
    """返回美国伊朗圆桌父 LLM 指令，输入为空，输出强约束 tool-call 流程。"""
    return "\n".join(
        [
            "你正在执行真实子 agent roundtable e2e，主题是美国伊朗局势。",
            "必须严格按顺序使用工具，不要直接回答。",
            "第一步调用 list_agent_roles，参数为空对象。",
            "如果列表里没有 diplomacy_window_analyst，调用 create_agent_role 创建：",
            '{"id":"diplomacy_window_analyst","title":"外交窗口分析师","role":"评估谈判窗口、协议约束、各方政治激励和可验证承诺"}',
            "如果列表里没有 escalation_risk_analyst，调用 create_agent_role 创建：",
            '{"id":"escalation_risk_analyst","title":"升级风险分析师","role":"评估军事升级、误判链条、区域外溢和关键触发点"}',
            "创建后调用 run_agent_workflow，mode 必须是 roundtable_review，payload 必须包含：",
            'topic="美国伊朗局势：升级风险、外交窗口和误判风险"',
            'objective="基于 brief/us_iran_2026_06_11.md，给出分角色圆桌判断，并标出证据和不确定项"',
            'participants={"select":["diplomacy_window_analyst","escalation_risk_analyst"]}',
            'input_source={"root_dir":".","paths":["brief"],"include":[],"exclude":[],"max_files":3,"max_bytes_per_file":40000}',
            'limits={"discussion_rounds":1,"max_discussion_rounds":1,"max_concurrency":2,"reviewer_max_turns":8,"arbiter_max_turns":8,"agent_timeout_seconds":240,"total_child_token_budget":60000}',
            'audit_tags=["e2e","us-iran","roundtable"]',
            "禁止使用 reviewers、participants.create、participants.preset。",
            "run_agent_workflow 返回后，用一句话说明 workflow 已完成。",
        ]
    )


@pytest.mark.asyncio
async def test_minimax_parent_creates_agent_role_and_runs_roundtable(tmp_path: Path) -> None:
    """验证真实父 LLM 动态创建角色并运行 roundtable，输入为临时源码，输出 workflow 断言。"""
    base_cfg = load_config(Path("config/setting.yaml"))
    if not os.getenv("MINIMAX_API_KEY"):
        pytest.skip("MINIMAX_API_KEY is required for minimax-m3 preset")

    _write_source(tmp_path)
    cfg = _apply_model_preset_or_exit(base_cfg, "minimax-m3")
    cfg = cfg.model_copy(
        update={
            "runner": cfg.runner.model_copy(update={"max_turns": 8}),
            "session": cfg.session.model_copy(
                update={"backend": "memory", "file_store_path": str(tmp_path / "sessions")}
            ),
            "trace": cfg.trace.model_copy(
                update={"raw_llm": False, "output_path": str(tmp_path / "trace.jsonl")}
            ),
            "scheduler": cfg.scheduler.model_copy(update={"enabled": False}),
        }
    )
    role_manager = AgentRoleManager(role_dir=tmp_path / "roles")
    handle = AgentWorkflowHandle()
    registry = ToolRegistry()
    register_agent_role_tool(registry, role_manager)
    register_agent_workflow_tool(registry, handle)
    fake_subagents = _FakeRoundtableSubAgentManager()
    manager = AgentWorkflowManager(
        subagents=fake_subagents,  # type: ignore[arg-type]
        config=cfg,
        workspace_root=tmp_path,
        role_manager=role_manager,
    )
    handle.bind(manager)
    sessions: dict[str, InMemorySession] = {}

    def _session_factory(session_id: str) -> InMemorySession:
        """构造并缓存 session，输入为 session id，输出 InMemorySession。"""
        session = InMemorySession(session_id)
        sessions[session_id] = session
        return session

    runtime = NativeRuntime.build(
        cfg,
        approval=AutoAllowApproval(),
        tools=registry,
        enabled_tool_names=registry.names(),
        session_factory=_session_factory,
        agent_spec=AgentSpec(
            name="agent-role-live-parent",
            instructions=_instructions(),
            default_model=cfg.model.name,
            tool_names=(),
            max_turns=8,
            reasoning_effort=cfg.model.reasoning_effort,
        ),
    )

    try:
        result = await runtime.run(
            "按系统指令执行 agent role roundtable live e2e。",
            session_id="live-agent-role-roundtable",
        )
    finally:
        await runtime.aclose()

    session = sessions["live-agent-role-roundtable"]
    history = await session.history()
    tool_messages = [message for message in history if message.role == "tool"]
    tool_names = [message.name for message in tool_messages]
    assert result.status == "completed", result.error
    assert "list_agent_roles" in tool_names
    assert "create_agent_role" in tool_names
    assert "run_agent_workflow" in tool_names
    assert tool_names.index("list_agent_roles") < tool_names.index("create_agent_role")
    assert tool_names.index("create_agent_role") < tool_names.index("run_agent_workflow")

    created = next(message for message in tool_messages if message.name == "create_agent_role")
    assert created.metadata["data"]["role"]["id"] == "risk_skeptic"
    workflow_message = next(
        message for message in tool_messages if message.name == "run_agent_workflow"
    )
    workflow_data = workflow_message.metadata["data"]
    workflow_dir = Path(workflow_data["workflow_dir"])
    roles = json.loads((workflow_dir / "roles.json").read_text(encoding="utf-8"))
    assert roles["roles"] == [
        {
            "id": "risk_skeptic",
            "title": "风险质询者",
            "role": "专门寻找方案失败路径和隐藏风险",
        }
    ]
    assert any(task.metadata["roundtable_agent"] == "risk_skeptic" for task in fake_subagents.tasks)

    print(f"workflow_dir={workflow_dir}")
    print(f"tool_names={tool_names}")
    print(f"roles_json={workflow_dir / 'roles.json'}")


@pytest.mark.asyncio
async def test_minimax_parent_runs_real_us_iran_roundtable_and_writes_child_logs(
    tmp_path: Path,
) -> None:
    """验证真实子 agent 圆桌讨论并写入聊天日志，输入为局势材料，输出日志路径断言。"""
    base_cfg = load_config(Path("config/setting.yaml"))
    if not os.getenv("MINIMAX_API_KEY"):
        pytest.skip("MINIMAX_API_KEY is required for minimax-m3 preset")

    brief_path = _write_us_iran_brief(tmp_path)
    cfg = _apply_model_preset_or_exit(base_cfg, "minimax-m3")
    cfg = cfg.model_copy(
        update={
            "runner": cfg.runner.model_copy(update={"max_turns": 20}),
            "session": cfg.session.model_copy(
                update={"backend": "file", "file_store_path": str(tmp_path / "sessions")}
            ),
            "trace": cfg.trace.model_copy(
                update={"raw_llm": False, "output_path": str(tmp_path / "trace.jsonl")}
            ),
            "scheduler": cfg.scheduler.model_copy(update={"enabled": False}),
        }
    )
    bootstrap = SessionBootstrap(
        agent_name="agent-role-us-iran-live",
        model_name=cfg.model.name,
        instruction_sources=[],
        instruction_text_hash="test",
        created_at=1.0,
        cwd=str(tmp_path),
    )

    def _session_factory(session_id: str):  # type: ignore[no-untyped-def]
        """构造 file session，输入为 session id，输出可落盘会话。"""
        return build_session(cfg, session_id, bootstrap=bootstrap)

    role_manager = AgentRoleManager(role_dir=tmp_path / "roles")
    handle = AgentWorkflowHandle()
    registry = ToolRegistry(build_file_tools())
    register_agent_role_tool(registry, role_manager)
    register_agent_workflow_tool(registry, handle)
    runtime = NativeRuntime.build(
        cfg,
        approval=AutoAllowApproval(),
        tools=registry,
        enabled_tool_names=registry.names(),
        session_factory=_session_factory,
        agent_spec=AgentSpec(
            name="agent-role-us-iran-parent",
            instructions=_us_iran_instructions(),
            default_model=cfg.model.name,
            tool_names=(),
            max_turns=20,
            reasoning_effort=cfg.model.reasoning_effort,
        ),
    )
    manager = AgentWorkflowManager(
        subagents=SubAgentManager(runtime),
        config=cfg,
        workspace_root=tmp_path,
        role_manager=role_manager,
    )
    handle.bind(manager)

    try:
        result = await runtime.run(
            "按系统指令执行美国伊朗局势真实子 agent roundtable e2e。",
            session_id="live-agent-role-us-iran-roundtable",
        )
    finally:
        await runtime.aclose()

    parent_session = _session_factory("live-agent-role-us-iran-roundtable")
    history = await parent_session.history()
    tool_messages = [message for message in history if message.role == "tool"]
    tool_names = [message.name for message in tool_messages]
    assert result.status == "completed", result.error
    assert "list_agent_roles" in tool_names
    assert "create_agent_role" in tool_names
    assert "run_agent_workflow" in tool_names

    workflow_message = next(
        message for message in tool_messages if message.name == "run_agent_workflow"
    )
    workflow_data = workflow_message.metadata["data"]
    workflow_dir = Path(workflow_data["workflow_dir"])
    roles = json.loads((workflow_dir / "roles.json").read_text(encoding="utf-8"))
    assert [role["id"] for role in roles["roles"]] == [
        "diplomacy_window_analyst",
        "escalation_risk_analyst",
    ]
    workflow_manifest = json.loads((workflow_dir / "workflow.json").read_text(encoding="utf-8"))
    assert workflow_manifest["status"] == "completed"
    report_index = json.loads((workflow_dir / "reports" / "index.json").read_text(encoding="utf-8"))
    report_statuses = {report["task_name"]: report["status"] for report in report_index["reports"]}
    assert report_statuses == {
        "independent-diplomacy_window_analyst": "completed",
        "independent-escalation_risk_analyst": "completed",
        "arbiter-agent": "completed",
    }
    assert (workflow_dir / "review_board" / "final_report.md").is_file()
    final_report = (workflow_dir / "review_board" / "final_report.md").read_text(encoding="utf-8")
    assert "伊朗" in final_report or "Iran" in final_report
    assert "fallback_reason" not in final_report

    subagent_records: list[dict[str, Any]] = []
    for path in sorted(workflow_dir.glob("agents/*/subagent.json")):
        subagent_records.append(json.loads(path.read_text(encoding="utf-8")))
    assert len(subagent_records) == 3
    child_logs = [Path(record["child_session_log_path"]) for record in subagent_records]
    for child_log in child_logs:
        assert child_log.is_file(), child_log
        assert child_log.read_text(encoding="utf-8").strip(), child_log

    print(f"brief_path={brief_path}")
    print(f"workflow_dir={workflow_dir}")
    print(f"final_report={workflow_dir / 'review_board' / 'final_report.md'}")
    print("child_session_logs=")
    for record, child_log in zip(subagent_records, child_logs, strict=True):
        print(f"- {record['task_run_id']} | {record['task_name']} | {child_log}")
