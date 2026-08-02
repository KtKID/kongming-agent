"""map_reduce workflow 集成单元测试。

本脚本验证 map_reduce 策略注册、通用 payload 入口、fake mapper 子 agent、reducer 产物和 run_agent_workflow tool 分流。
作用是用可重复 pytest 覆盖 map_reduce v0.1 的 runtime 集成边界，避免依赖真实模型 smoke。
关键执行流程：创建临时源码树，构造 MapReduce payload，用确定性 workflow task executor 返回 code_findings JSON，断言 workflow/root/map_reduce/reports 产物完整。
关键函数：_payload 构造 workflow 输入，_mapper_output 构造 mapper JSON，test_* 覆盖 manager 和 tool 入口。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from application.agent_roles import AgentRoleManager
from application.agent_workflows.task_models import SubAgentRun
from core.contracts import ToolContext
from infrastructure.config.models import Config, ModelSelectionConfig
from tests.support.tool_calls import execute_prepared_tool
from tests.support.workflow_strategy_manager import WorkflowStrategyTestManager
from tools.agent_workflow_tool import (
    AgentWorkflowHandle,
    build_run_agent_workflow_tool,
)

_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "agent_workflows" / "map_reduce"


class _FakeWorkflowTaskExecutor:
    """测试用 workflow task executor，按 shard metadata 返回结构化 mapper JSON。"""

    def __init__(self, *, content_suffix: str = "") -> None:
        """初始化 fake manager，输入为空，输出为记录任务的实例。"""
        self.tasks: list[Any] = []
        self._content_suffix = content_suffix

    async def execute_task(
        self, *, workflow_id: str, parent_session_id: str, task: Any, audit_writer: Any
    ) -> SubAgentRun:
        """运行 fake mapper，输入为任务 metadata，输出为 completed SubAgentRun。"""
        self.tasks.append(task)
        shard_id = str(task.metadata["map_reduce_shard_id"])
        files = list(task.metadata["map_reduce_files"])
        content = (
            json.dumps(_mapper_output(shard_id=shard_id, files=files), ensure_ascii=False)
            + self._content_suffix
        )
        return SubAgentRun(
            task=task,
            session_id=f"child-{shard_id}",
            run_id=f"run-{shard_id}",
            status="completed",
            content=content,
            error_message=None,
            turn_count=1,
        )


class _RawTextWorkflowTaskExecutor:
    """测试用 raw_text workflow executor，按任务顺序返回随机数样式文本。"""

    def __init__(self) -> None:
        """初始化 fake manager，输入为空，输出为记录任务的实例。"""
        self.tasks: list[Any] = []

    async def execute_task(
        self, *, workflow_id: str, parent_session_id: str, task: Any, audit_writer: Any
    ) -> SubAgentRun:
        """运行 fake raw_text mapper，输入为任务 metadata，输出为数字文本。"""
        del workflow_id, parent_session_id, audit_writer
        self.tasks.append(task)
        order = int(task.metadata["map_reduce_display_order"])
        number = order * 11
        return SubAgentRun(
            task=task,
            session_id=f"child-raw-{order}",
            run_id=f"run-raw-{order}",
            status="completed",
            content=f"数字: {number}\n宣言: 我抽到了 {number}",
            error_message=None,
            turn_count=1,
        )


class _SlowWorkflowTaskExecutor:
    """测试用慢速 workflow executor，用于触发 mapper timeout。"""

    async def execute_task(
        self, *, workflow_id: str, parent_session_id: str, task: Any, audit_writer: Any
    ) -> SubAgentRun:
        """延迟返回 fake mapper，输入为任务，输出为会被 timeout 截断的 run。"""
        await asyncio.sleep(2)
        shard_id = str(task.metadata["map_reduce_shard_id"])
        return SubAgentRun(
            task=task,
            session_id=f"child-{shard_id}",
            run_id=f"run-{shard_id}",
            status="completed",
            content=json.dumps(_mapper_output(shard_id=shard_id, files=["src/alpha.py"])),
            error_message=None,
            turn_count=1,
        )


def _config(tmp_path: Path) -> Config:
    """构造测试配置，输入为临时目录，输出为 file session 配置。"""
    cfg = Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"))
    cfg.session.file_store_path = str(tmp_path / "sessions")
    return cfg


def _payload() -> dict[str, Any]:
    """构造 map_reduce payload，输入为空，输出为 v0.1 JSON。"""
    return {
        "mode": "map_reduce",
        "objective": "检查 workflow runtime 的边界风险。",
        "input_source": {
            "kind": "path_glob",
            "root_dir": ".",
            "include": ["src/**/*.py", "tests/**/*.py"],
            "exclude": [".venv/**"],
            "files": [],
            "index_provider": "rg",
            "input_digest": None,
        },
        "shard_strategy": {
            "kind": "by_file_count",
            "max_files_per_shard": 1,
            "max_estimated_tokens_per_shard": 20000,
            "min_shards": 1,
            "max_shards": 8,
            "preserve_directory_boundary": True,
            "prefer_dependency_cohesion": False,
        },
        "output_contract": "code_findings",
        "mapper": {
            "name_prefix": "map-unit",
            "prompt_template": "code_findings_v0_1",
            "tool_names": ["read_file", "list_dir", "write_file"],
            "skill_names": [],
            "permission_mode": "scoped_workdir",
            "max_turns": 3,
            "max_output_chars": 60000,
        },
        "reducer": {
            "kind": "deterministic",
            "dedupe_strategy": "exact_dedupe_key",
            "ranking_strategy": "severity_first",
            "max_findings": 20,
            "include_failed_shards": True,
            "reducer_prompt_template": None,
        },
        "limits": {
            "max_concurrency": 2,
            "workflow_timeout_seconds": 300,
            "mapper_timeout_seconds": 120,
            "reducer_timeout_seconds": 60,
            "mapper_retries": 0,
            "validation_repair_retries": 0,
        },
        "audit_tags": ["unit", "task:map-reduce-strategy-integration-v0.1"],
    }


def _load_map_reduce_fixture(name: str) -> dict[str, Any]:
    """读取 map_reduce fixture，输入为文件名，输出为 JSON 对象。"""
    path = _FIXTURE_ROOT / name
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload


def _mapper_output(*, shard_id: str, files: list[str]) -> dict[str, Any]:
    """构造 mapper 输出，输入为 shard 和文件，输出为 code_findings JSON 对象。"""
    first_file = files[0]
    return {
        "output_contract": "code_findings",
        "shard_id": shard_id,
        "status": "completed",
        "summary": f"{shard_id} checked {len(files)} files.",
        "files_seen": files,
        "findings": [
            {
                "dedupe_key": f"{shard_id}:{first_file}:boundary",
                "title": "Workflow boundary finding",
                "category": "architecture",
                "severity": "P1",
                "confidence": 0.8,
                "locations": [
                    {
                        "path": first_file,
                        "line_start": 1,
                        "line_end": 1,
                        "symbol": None,
                        "excerpt": "def sample():",
                    }
                ],
                "evidence": f"{first_file}:1 shows a test boundary.",
                "rationale": "The fake mapper records deterministic evidence.",
                "recommendation": "Keep workflow boundaries covered by tests.",
                "impact_area": ["runtime", "test"],
                "source_shard_id": shard_id,
            }
        ],
        "coverage": {
            "files_assigned": len(files),
            "files_seen_count": len(files),
            "symbols_seen_count": 1,
            "skipped_files": [],
            "skip_reasons": [],
        },
        "errors": [],
    }


def _write_sample_tree(tmp_path: Path) -> None:
    """写入临时源码树，输入为根目录，输出为测试文件。"""
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "alpha.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "beta.py").write_text(
        "def beta():\n    return 2\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_alpha.py").write_text(
        "def test_alpha():\n    assert True\n", encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_map_reduce_workflow_runs_fake_mappers_and_writes_artifacts(tmp_path: Path) -> None:
    """验证 map_reduce 完整 fake 链路，输入为临时源码树，输出为产物和 reducer 断言。"""
    _write_sample_tree(tmp_path)
    subagents = _FakeWorkflowTaskExecutor()
    manager = WorkflowStrategyTestManager(
        task_executor=subagents,
        config=_config(tmp_path),
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )

    catalog = manager.list_workflow_strategies()
    assert [entry.mode for entry in catalog] == [
        "deep_research",
        "map_reduce",
        "parallel",
        "roundtable_review",
        "task_flow",
    ]
    description = manager.describe_workflow_strategy("map_reduce")
    assert description.status == "available"
    assert description.runnable is True

    result = await manager.run_workflow_payload(
        mode="map_reduce",
        parent_session_id="parent-session",
        payload=_payload(),
    )

    assert result.mode == "map_reduce"
    assert result.completed is True
    assert len(result.runs) == 3
    assert all("只输出一个 JSON 对象" in task.prompt for task in subagents.tasks)
    assert all(task.metadata["working_dir"] for task in subagents.tasks)
    assert all(task.metadata["max_turns"] == 3 for task in subagents.tasks)

    workflow_result = json.loads((result.workflow_dir / "result.json").read_text(encoding="utf-8"))
    assert workflow_result["mode"] == "map_reduce"
    assert workflow_result["completed"] is True
    assert workflow_result["map_reduce"]["reducer_output"]["status"] == "completed"
    assert len(workflow_result["map_reduce"]["reducer_output"]["top_findings"]) == 3

    reducer_result_path = result.workflow_dir / "map_reduce" / "reducer" / "result.json"
    reducer_result = json.loads(reducer_result_path.read_text(encoding="utf-8"))
    assert reducer_result["total_shards"] == 3
    assert reducer_result["completed_shards"] == 3
    assert reducer_result["failed_shards"] == 0

    report_index = json.loads(result.report_index_path.read_text(encoding="utf-8"))
    assert report_index["mode"] == "map_reduce"
    assert report_index["status"] == "completed"
    assert (result.workflow_dir / "map_reduce" / "shards.json").is_file()
    mapper_index = json.loads(
        (result.workflow_dir / "map_reduce" / "mappers" / "index.json").read_text(encoding="utf-8")
    )
    assert mapper_index["mapper_count"] == 3
    assert all(mapper["validation_valid"] is True for mapper in mapper_index["mappers"])
    assert all(mapper["report_path"] for mapper in mapper_index["mappers"])
    assert all(
        (Path(str(task.metadata["working_dir"])) / "input_manifest.json").is_file()
        for task in subagents.tasks
    )

    audit_records = [
        json.loads(line)
        for line in (result.workflow_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    actions = [record["action"] for record in audit_records]
    assert "map_reduce_started" in actions
    assert actions.count("map_mapper_started") == 3
    assert actions.count("map_mapper_output_validated") == 3
    assert "map_reduce_completed" in actions
    assert all("subagent_runtime" not in record["payload"] for record in audit_records)
    assert all("runtime_spec" not in record["payload"] for record in audit_records)
    assert all(
        record["payload"]["resolved_runtime"]["model"] == "gemma-4-e4b-it"
        for record in audit_records
    )


@pytest.mark.asyncio
async def test_map_reduce_rejects_validation_repair_retries(tmp_path: Path) -> None:
    """验证 unsupported repair retries 显式拒绝，输入为 retries=1，输出为 ValueError。"""
    _write_sample_tree(tmp_path)
    payload = _payload()
    payload["limits"]["validation_repair_retries"] = 1
    manager = WorkflowStrategyTestManager(
        task_executor=_FakeWorkflowTaskExecutor(),
        config=_config(tmp_path),
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )

    with pytest.raises(ValueError, match="validation_repair_retries"):
        await manager.run_workflow_payload(
            mode="map_reduce",
            parent_session_id="parent-session",
            payload=payload,
        )


@pytest.mark.asyncio
async def test_map_reduce_rejects_single_file_above_token_limit(tmp_path: Path) -> None:
    """验证 planner 遵守 token 上限，输入为过小 token limit，输出为 planner 错误。"""
    _write_sample_tree(tmp_path)
    payload = _payload()
    payload["shard_strategy"]["max_estimated_tokens_per_shard"] = 1
    manager = WorkflowStrategyTestManager(
        task_executor=_FakeWorkflowTaskExecutor(),
        config=_config(tmp_path),
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )

    with pytest.raises(ValueError, match="max_estimated_tokens_per_shard"):
        await manager.run_workflow_payload(
            mode="map_reduce",
            parent_session_id="parent-session",
            payload=payload,
        )


@pytest.mark.asyncio
async def test_map_reduce_marks_oversized_mapper_output_as_failed_shard(tmp_path: Path) -> None:
    """验证 mapper 输出长度上限，输入为超长输出，输出为 failed shard 汇总。"""
    _write_sample_tree(tmp_path)
    payload = _payload()
    payload["mapper"]["max_output_chars"] = 10
    manager = WorkflowStrategyTestManager(
        task_executor=_FakeWorkflowTaskExecutor(content_suffix="oversized"),
        config=_config(tmp_path),
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )

    result = await manager.run_workflow_payload(
        mode="map_reduce",
        parent_session_id="parent-session",
        payload=payload,
    )

    assert result.completed is False
    workflow_result = json.loads((result.workflow_dir / "result.json").read_text(encoding="utf-8"))
    reducer_output = workflow_result["map_reduce"]["reducer_output"]
    assert reducer_output["status"] == "failed"
    assert reducer_output["failed_shards"] == 3
    assert all(
        report["failed_stage"] == "validation" for report in reducer_output["failed_shard_reports"]
    )
    mapper_index = json.loads(
        (result.workflow_dir / "map_reduce" / "mappers" / "index.json").read_text(encoding="utf-8")
    )
    assert mapper_index["mapper_count"] == 3
    assert all(mapper["validation_valid"] is False for mapper in mapper_index["mappers"])
    assert all(mapper["failed_reports"] for mapper in mapper_index["mappers"])


@pytest.mark.asyncio
async def test_map_reduce_records_mapper_timeout_as_failed_shard(tmp_path: Path) -> None:
    """验证 mapper timeout 结构化收口，输入为慢速 fake mapper，输出为 failed shard。"""
    _write_sample_tree(tmp_path)
    payload = _payload()
    payload["input_source"] = {
        "kind": "file_list",
        "root_dir": ".",
        "include": [],
        "exclude": [],
        "files": ["src/alpha.py"],
        "index_provider": "rg",
        "input_digest": None,
    }
    payload["limits"]["max_concurrency"] = 1
    payload["limits"]["mapper_timeout_seconds"] = 1
    manager = WorkflowStrategyTestManager(
        task_executor=_SlowWorkflowTaskExecutor(),
        config=_config(tmp_path),
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )

    result = await manager.run_workflow_payload(
        mode="map_reduce",
        parent_session_id="parent-session",
        payload=payload,
    )

    assert result.completed is False
    assert result.runs[0].status == "failed"
    assert "timed out" in (result.runs[0].error_message or "")

    workflow_result = json.loads((result.workflow_dir / "result.json").read_text(encoding="utf-8"))
    reducer_output = workflow_result["map_reduce"]["reducer_output"]
    assert reducer_output["status"] == "failed"
    assert reducer_output["failed_shard_reports"][0]["failed_stage"] == "mapper"
    mapper_index = json.loads(
        (result.workflow_dir / "map_reduce" / "mappers" / "index.json").read_text(encoding="utf-8")
    )
    assert mapper_index["mapper_count"] == 1
    assert mapper_index["mappers"][0]["run_status"] == "failed"
    assert mapper_index["mappers"][0]["failed_reports"][0]["failed_stage"] == "mapper"

    actions = [
        json.loads(line)["action"]
        for line in (result.workflow_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "map_mapper_timeout" in actions
    assert "subagent_reported" in actions


@pytest.mark.asyncio
async def test_map_reduce_records_reducer_timeout_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 reducer timeout 仍写完整产物，输入为慢速 reducer，输出为 failed root result。"""
    _write_sample_tree(tmp_path)

    async def _slow_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        """延迟执行 to_thread，输入为同步函数，输出为会被 wait_for 取消的结果。"""
        await asyncio.sleep(2)
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _slow_to_thread)
    payload = _payload()
    payload["limits"]["reducer_timeout_seconds"] = 1
    manager = WorkflowStrategyTestManager(
        task_executor=_FakeWorkflowTaskExecutor(),
        config=_config(tmp_path),
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )

    result = await manager.run_workflow_payload(
        mode="map_reduce",
        parent_session_id="parent-session",
        payload=payload,
    )

    assert result.completed is False
    assert result.report_index_path.is_file()

    workflow_result = json.loads((result.workflow_dir / "result.json").read_text(encoding="utf-8"))
    reducer_output = workflow_result["map_reduce"]["reducer_output"]
    assert reducer_output["status"] == "failed"
    assert reducer_output["failed_shard_reports"][-1]["failed_stage"] == "reducer"
    assert (result.workflow_dir / "map_reduce" / "reducer" / "result.json").is_file()

    mapper_index = json.loads(
        (result.workflow_dir / "map_reduce" / "mappers" / "index.json").read_text(encoding="utf-8")
    )
    assert mapper_index["mapper_count"] == 3
    assert all(mapper["validation_valid"] is True for mapper in mapper_index["mappers"])

    actions = [
        json.loads(line)["action"]
        for line in (result.workflow_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "map_reduce_reducer_failed" in actions
    assert "map_reduce_completed" in actions


@pytest.mark.asyncio
async def test_run_agent_workflow_tool_passes_map_reduce_payload_to_manager() -> None:
    """验证 run_agent_workflow tool 分流，输入为 map_reduce payload，输出为 manager 入参断言。"""

    class _Manager:
        """测试用 manager，记录通用 payload 调用。"""

        def __init__(self) -> None:
            """初始化测试 manager，输入为空，输出为可记录调用参数的实例。"""
            self.mode = ""
            self.parent_session_id = ""
            self.payload: dict[str, object] = {}

        async def run_workflow_payload(
            self,
            *,
            mode: str,
            parent_session_id: str,
            payload: dict[str, object],
            parent_agent: dict[str, object] | None = None,
        ) -> Any:
            """记录 workflow payload 调用，输入为 mode/session/payload，输出为 fake 结果。"""
            del parent_agent
            self.mode = mode
            self.parent_session_id = parent_session_id
            self.payload = payload

            class _Result:
                """测试用结果对象，提供 tool 格式化所需字段。"""

                workflow_id = "wf-map"
                mode = "map_reduce"
                workflow_dir = Path("/tmp/wf-map")
                report_index_path = Path("/tmp/wf-map/reports/index.json")
                completed = True
                reports: tuple[Any, ...] = ()
                runs: tuple[Any, ...] = ()
                data = {
                    "map_reduce": {
                        "artifact_paths": {
                            "reducer_result_path": "/tmp/wf-map/map_reduce/reducer/result.json"
                        }
                    }
                }

            return _Result()

    handle = AgentWorkflowHandle()
    manager = _Manager()
    handle.bind(manager)
    tool = build_run_agent_workflow_tool(handle)

    content, data = await tool._run(
        {"mode": "map_reduce", "payload": _payload()},
        ToolContext(run_id="r", session_id="parent-session", turn=1, call_id="c"),
    )

    assert manager.mode == "map_reduce"
    assert manager.parent_session_id == "parent-session"
    assert manager.payload["mode"] == "map_reduce"
    assert "map_reduce_reducer_result" in content
    assert data is not None
    assert data["mode"] == "map_reduce"
    assert data["map_reduce"]["artifact_paths"]["reducer_result_path"].endswith("result.json")


@pytest.mark.asyncio
async def test_run_agent_workflow_tool_unwraps_map_reduce_spec_payload() -> None:
    """验证 tool 容忍模型误加外层类型名，输入为 MapReduceWorkflowSpec 包裹，输出为规范 payload。"""

    class _Manager:
        """测试用 manager，记录解包后的 payload。"""

        def __init__(self) -> None:
            """初始化测试 manager，输入为空，输出为记录 payload 的实例。"""
            self.payload: dict[str, object] = {}

        async def run_workflow_payload(
            self,
            *,
            mode: str,
            parent_session_id: str,
            payload: dict[str, object],
            parent_agent: dict[str, object] | None = None,
        ) -> Any:
            """记录 workflow payload 调用，输入为 mode/session/payload，输出为 fake 结果。"""
            del mode, parent_session_id, parent_agent
            self.payload = payload

            class _Result:
                """测试用结果对象，提供 tool 格式化所需字段。"""

                workflow_id = "wf-map"
                mode = "map_reduce"
                workflow_dir = Path("/tmp/wf-map")
                report_index_path = Path("/tmp/wf-map/reports/index.json")
                completed = True
                reports: tuple[Any, ...] = ()
                runs: tuple[Any, ...] = ()
                data: dict[str, object] = {}

            return _Result()

    handle = AgentWorkflowHandle()
    manager = _Manager()
    handle.bind(manager)
    tool = build_run_agent_workflow_tool(handle)

    wrapped_payload = {"MapReduceWorkflowSpec": _payload()}
    result = await execute_prepared_tool(
        tool,
        {"mode": "map_reduce", "payload": wrapped_payload},
        ToolContext(run_id="r", session_id="parent-session", turn=1, call_id="c"),
    )

    assert result.ok is True
    assert manager.payload["objective"] == "检查 workflow runtime 的边界风险。"
    assert "MapReduceWorkflowSpec" not in manager.payload
    assert manager.payload["mode"] == "map_reduce"


@pytest.mark.asyncio
async def test_run_agent_workflow_tool_normalizes_minimax_tool_argument_shapes() -> None:
    """验证 tool 归一化 MiniMax 常见参数形态，输入为字符串数字和 item 包装，输出为规范类型。"""

    class _Manager:
        """测试用 manager，记录归一化后的 payload。"""

        def __init__(self) -> None:
            """初始化测试 manager，输入为空，输出为记录 payload 的实例。"""
            self.payload: dict[str, Any] = {}

        async def run_workflow_payload(
            self,
            *,
            mode: str,
            parent_session_id: str,
            payload: dict[str, object],
            parent_agent: dict[str, object] | None = None,
        ) -> Any:
            """记录 workflow payload 调用，输入为 mode/session/payload，输出为 fake 结果。"""
            del mode, parent_session_id, parent_agent
            self.payload = dict(payload)

            class _Result:
                """测试用结果对象，提供 tool 格式化所需字段。"""

                workflow_id = "wf-map"
                mode = "map_reduce"
                workflow_dir = Path("/tmp/wf-map")
                report_index_path = Path("/tmp/wf-map/reports/index.json")
                completed = True
                reports: tuple[Any, ...] = ()
                runs: tuple[Any, ...] = ()
                data: dict[str, object] = {}

            return _Result()

    payload = _payload()
    payload["input_source"] = {
        "kind": "path_glob",
        "root_dir": "src/executors/agent_runtime",
        "include": {"item": "**/*.py"},
        "exclude": {"item": "**/__pycache__/**"},
        "files": "",
        "index_provider": "rg",
        "input_digest": "null",
    }
    payload["shard_strategy"] = {
        "kind": "by_directory",
        "max_files_per_shard": "6",
        "max_estimated_tokens_per_shard": "20000",
        "min_shards": "3",
        "max_shards": "4",
        "preserve_directory_boundary": "true",
        "prefer_dependency_cohesion": "false",
    }
    payload["mapper"] = {
        "name_prefix": "map-agent-runtime",
        "prompt_template": "code_findings_v0_1",
        "tool_names": {"item": ["read_file", "list_dir"]},
        "skill_names": "",
        "permission_mode": "scoped_workdir",
        "max_turns": "4",
        "max_output_chars": "60000",
    }
    payload["reducer"] = {
        "kind": "deterministic",
        "dedupe_strategy": "exact_dedupe_key",
        "ranking_strategy": "severity_first",
        "max_findings": "50",
        "include_failed_shards": "true",
        "reducer_prompt_template": "",
    }
    payload["limits"] = {
        "max_concurrency": "3",
        "workflow_timeout_seconds": "1800",
        "mapper_timeout_seconds": "480",
        "reducer_timeout_seconds": "300",
        "mapper_retries": "1",
        "validation_repair_retries": "0",
    }
    payload["audit_tags"] = {"item": ["user_review_map_reduce", "agent_runtime_overview"]}

    handle = AgentWorkflowHandle()
    manager = _Manager()
    handle.bind(manager)
    tool = build_run_agent_workflow_tool(handle)

    result = await execute_prepared_tool(
        tool,
        {"mode": "map_reduce", "payload": payload},
        ToolContext(run_id="r", session_id="parent-session", turn=1, call_id="c"),
    )

    normalized = manager.payload
    input_source = normalized["input_source"]
    shard_strategy = normalized["shard_strategy"]
    mapper = normalized["mapper"]
    reducer = normalized["reducer"]
    limits = normalized["limits"]

    assert result.ok is True
    assert input_source["include"] == ["**/*.py"]
    assert input_source["exclude"] == ["**/__pycache__/**"]
    assert input_source["files"] == []
    assert input_source["input_digest"] is None
    assert shard_strategy["max_files_per_shard"] == 6
    assert shard_strategy["preserve_directory_boundary"] is True
    assert shard_strategy["prefer_dependency_cohesion"] is False
    assert mapper["tool_names"] == ["read_file", "list_dir"]
    assert mapper["skill_names"] == []
    assert mapper["max_turns"] == 4
    assert reducer["max_findings"] == 50
    assert reducer["include_failed_shards"] is True
    assert reducer["reducer_prompt_template"] is None
    assert limits["mapper_retries"] == 1
    assert limits["validation_repair_retries"] == 0
    assert normalized["audit_tags"] == ["user_review_map_reduce", "agent_runtime_overview"]


@pytest.mark.asyncio
async def test_run_agent_workflow_tool_normalizes_cli_session_map_reduce_payload(
    tmp_path: Path,
) -> None:
    """验证 CLI 真实失败参数被归一化，输入为绝对 file_list，输出为可执行 workflow。"""
    _write_sample_tree(tmp_path)
    subagents = _FakeWorkflowTaskExecutor()
    manager = WorkflowStrategyTestManager(
        task_executor=subagents,
        config=_config(tmp_path),
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )
    handle = AgentWorkflowHandle()
    handle.bind(manager)
    tool = build_run_agent_workflow_tool(handle)

    payload = _payload()
    payload["input_source"] = {
        "kind": "file_list",
        "files": [
            str(tmp_path / "src" / "alpha.py"),
            str(tmp_path / "src" / "pkg" / "beta.py"),
            str(tmp_path / "tests" / "test_alpha.py"),
        ],
    }
    payload["shard_strategy"] = {
        "kind": "by_directory",
        "max_files_per_shard": 5,
        "min_shards": 5,
        "max_shards": 5,
        "preserve_directory_boundary": True,
        "prefer_dependency_cohesion": True,
    }
    payload["mapper"]["tool_names"] = ["read_file", "list_dir", "run_shell"]
    payload["mapper"].pop("skill_names")
    payload["limits"] = {
        "max_concurrency": 5,
        "workflow_timeout_seconds": 600,
        "mapper_timeout_seconds": 300,
        "mapper_retries": 1,
    }

    result = await execute_prepared_tool(
        tool,
        {"mode": "map_reduce", "payload": payload},
        ToolContext(run_id="r", session_id="parent-session", turn=1, call_id="c"),
    )

    assert result.ok is True
    assert result.data is not None
    assert result.data["completed"] is True
    assert len(subagents.tasks) == 3
    assert {tuple(task.metadata["map_reduce_files"]) for task in subagents.tasks} == {
        ("src/alpha.py",),
        ("src/pkg/beta.py",),
        ("tests/test_alpha.py",),
    }
    assert all("run_shell" not in task.tool_names for task in subagents.tasks)


@pytest.mark.asyncio
async def test_run_agent_workflow_tool_runs_inline_raw_text_map_reduce(
    tmp_path: Path,
) -> None:
    """验证 CLI 失败 fixture 回放，输入为 noop inline，输出为 3 个 raw_text mapper 报告。"""
    fixture = _load_map_reduce_fixture("cli-5f68e28fc030-inline-noop.json")
    expected = fixture["expected_after_fix"]
    subagents = _RawTextWorkflowTaskExecutor()
    manager = WorkflowStrategyTestManager(
        task_executor=subagents,
        config=_config(tmp_path),
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )
    handle = AgentWorkflowHandle()
    handle.bind(manager)
    tool = build_run_agent_workflow_tool(handle)

    result = await execute_prepared_tool(
        tool,
        fixture["arguments"],
        ToolContext(run_id="r", session_id=fixture["session_id"], turn=1, call_id="inline-call"),
    )

    assert result.ok is True
    assert result.data is not None
    assert result.data["completed"] == expected["completed"]
    assert len(subagents.tasks) == expected["report_count"]
    assert (
        result.data["map_reduce"]["reducer_output"]["output_contract"]
        == expected["output_contract"]
    )
    assert (
        result.data["map_reduce"]["reducer_output"]["completed_shards"] == expected["report_count"]
    )
    summaries = [report["summary"] for report in result.data["reports"]]
    assert summaries == [
        "数字: 11 宣言: 我抽到了 11",
        "数字: 22 宣言: 我抽到了 22",
        "数字: 33 宣言: 我抽到了 33",
    ]
    assert all(task.metadata["map_reduce_files"] for task in subagents.tasks)
    assert all(task.permission.mode == "scoped_workdir" for task in subagents.tasks)


@pytest.mark.asyncio
async def test_run_agent_workflow_tool_runs_absolute_temp_placeholder_map_reduce(
    tmp_path: Path,
) -> None:
    """验证 CLI 临时绝对占位路径 fixture 回放，输入为 /tmp 文件，输出为 5 个 raw_text mapper。"""
    fixture = _load_map_reduce_fixture("cli-7b3b9df541d4-absolute-temp-placeholder.json")
    expected = fixture["expected_after_fix"]
    subagents = _RawTextWorkflowTaskExecutor()
    manager = WorkflowStrategyTestManager(
        task_executor=subagents,
        config=_config(tmp_path),
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )
    handle = AgentWorkflowHandle()
    handle.bind(manager)
    tool = build_run_agent_workflow_tool(handle)

    result = await execute_prepared_tool(
        tool,
        fixture["arguments"],
        ToolContext(
            run_id="r",
            session_id=fixture["session_id"],
            turn=1,
            call_id="absolute-temp-call",
        ),
    )

    assert result.ok is True
    assert result.data is not None
    assert result.data["completed"] == expected["completed"]
    assert len(subagents.tasks) == expected["report_count"]
    assert (
        result.data["map_reduce"]["reducer_output"]["output_contract"]
        == expected["output_contract"]
    )
    assert (
        result.data["map_reduce"]["reducer_output"]["completed_shards"] == expected["report_count"]
    )
    summaries = [report["summary"] for report in result.data["reports"]]
    assert summaries == [
        "数字: 11 宣言: 我抽到了 11",
        "数字: 22 宣言: 我抽到了 22",
        "数字: 33 宣言: 我抽到了 33",
        "数字: 44 宣言: 我抽到了 44",
        "数字: 55 宣言: 我抽到了 55",
    ]
    assert all("run_shell" not in task.tool_names for task in subagents.tasks)
    assert all(task.metadata["map_reduce_files"] for task in subagents.tasks)
    assert all(task.permission.mode == "scoped_workdir" for task in subagents.tasks)


@pytest.mark.asyncio
async def test_run_agent_workflow_tool_failure_content_guides_model_in_chinese() -> None:
    """验证 workflow 失败时返回中文提示，输入为 manager 异常，输出为禁止编造的 content。"""

    class _Manager:
        """测试用 manager，固定抛出 payload 错误。"""

        async def run_workflow_payload(
            self,
            *,
            mode: str,
            parent_session_id: str,
            payload: dict[str, object],
            parent_agent: dict[str, object] | None = None,
        ) -> Any:
            """抛出模拟校验错误，输入为 workflow 调用，输出为异常。"""
            del mode, parent_session_id, payload, parent_agent
            raise ValueError("$.input_source: expected object")

    handle = AgentWorkflowHandle()
    handle.bind(_Manager())
    tool = build_run_agent_workflow_tool(handle)

    result = await execute_prepared_tool(
        tool,
        {"mode": "map_reduce", "payload": _payload()},
        ToolContext(run_id="r", session_id="parent-session", turn=1, call_id="c"),
    )

    assert result.ok is False
    assert "$.input_source" in (result.error_message or "")
    assert "工具执行失败：run_agent_workflow" in result.content
    assert "禁止声称工具已经成功执行" in result.content
    assert "run_agent_workflow 参数修正提示" in result.content
    assert "禁止把 payload 写成" in result.content
