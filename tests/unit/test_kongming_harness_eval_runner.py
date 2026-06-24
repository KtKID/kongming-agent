"""Kongming runtime harness eval runner 单元测试。"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "scripts" / "run_kongming_harness_eval.py"


def _load_runner_module():
    """加载 runtime eval 脚本模块，返回可直接调用的 module。"""

    spec = importlib.util.spec_from_file_location("run_kongming_harness_eval", RUNNER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_kongming_harness_eval"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fixture_runtime_suite_runs_real_tool_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fixture 模式必须通过真实 Runner 产生 tool_call / tool_result 闭环。"""

    monkeypatch.delenv("KONGMING_HOME", raising=False)
    runner = _load_runner_module()

    await runner.run_harness_environment(
        "fixture-full",
        runner.EvalEnvironmentOverrides(run_id="unit-runtime", output_dir=str(tmp_path)),
    )

    run_dir = tmp_path / "unit-runtime"
    assert os.environ.get("KONGMING_HOME") is None
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["total"] == 9
    assert summary["passed"] == 9
    assert summary["score"] == 1.0
    assert summary["environment_id"] == "fixture-full"
    assert summary["profile"] == "full"
    assert summary["approval_mode"] == "auto_allow"
    assert summary["session_backend"] == "file"
    assert summary["fixture_semantics"] == {
        "uses_real_runner": True,
        "uses_real_llm_provider": False,
        "tool_execution_checks_tool_loop": True,
        "non_tool_tasks_check": [
            "NativeRuntime.run request/response path",
            "session persistence",
            "deterministic scorer behavior",
        ],
    }
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "工具执行" in report
    assert "通过数：`9 / 9`" in report
    assert "环境预设：`fixture-full`" in report
    assert "Fixture 验证边界" in report
    assert "Environment config hash：`sha256:" in report
    assert "API keys present：`{}" in report

    trajectory = json.loads(
        (run_dir / "tasks" / "tool_execution_runner_001" / "trajectory.json").read_text(
            encoding="utf-8"
        )
    )
    assert trajectory["runtime"]["status"] == "completed"
    assert trajectory["runtime"]["turn_count"] == 2
    assert trajectory["runtime"]["metadata"]["environment_id"] == "fixture-full"
    assert trajectory["runtime"]["metadata"]["resolved_profile"] == "full"
    assert trajectory["runtime"]["metadata"]["effective_approval_mode"] == "auto_allow"
    event_kinds = [event["kind"] for event in trajectory["events"]]
    assert event_kinds.count("llm.request") == 2
    assert "approval.request" in event_kinds
    assert "approval.decision" in event_kinds
    assert "tool.call.end" in event_kinds
    details = trajectory["score"]["details"]
    assert details["tool_calls"] == [
        {
            "call_id": "fixture-call-1",
            "name": "search_code",
            "arguments": {"query": "Runner.run"},
            "turn": 1,
        },
        {
            "call_id": "fixture-call-2",
            "name": "read_file",
            "arguments": {"path": "src/core/runner.py"},
            "turn": 1,
        },
    ]
    assert all(result["ok"] for result in details["tool_results"])

    session_id = "unit-runtime-tool_execution_runner_001"
    session_dir = run_dir / "sessions" / session_id
    session_jsonl = session_dir / f"{session_id}.jsonl"
    assert (session_dir / "manifest.json").is_file()
    assert (session_dir / "system_prompt.json").is_file()
    assert session_jsonl.is_file()
    records = [
        json.loads(line)
        for line in session_jsonl.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    roles = [record["message"]["role"] for record in records]
    assert roles == ["user", "assistant", "tool", "tool", "assistant"]
    assert any(record["message"].get("tool_call_id") for record in records)


@pytest.mark.unit
def test_environment_resolver_loads_yaml_and_cli_overrides(tmp_path: Path) -> None:
    """environment resolver 必须读取 YAML，并让 CLI 覆盖优先。"""

    runner = _load_runner_module()

    resolved = runner.resolve_eval_environment(
        "fixture-baseline",
        runner.EvalEnvironmentOverrides(max_turns=20, output_dir=str(tmp_path)),
    )

    assert resolved.environment_id == "fixture-baseline"
    assert resolved.profile == "baseline-min"
    assert resolved.instructions_mode == "empty"
    assert resolved.session_backend == "memory"
    assert resolved.compactor_mode == "noop-script"
    assert resolved.runner_max_turns == 20
    assert resolved.output_dir == tmp_path.resolve()
    assert resolved.override_sources["runner.max_turns"] == "cli"
    assert resolved.environment_config_hash.startswith("sha256:")


@pytest.mark.unit
def test_environment_resolver_rejects_missing_required_field(tmp_path: Path) -> None:
    """environment YAML 缺必填字段时必须报错。"""

    runner = _load_runner_module()
    env_path = tmp_path / "environments.yaml"
    env_path.write_text(
        """
environments:
  incomplete:
    suite: evals/harness-runtime-v0.1
    mode: fixture
    profile: full
    runner:
      max_turns: 50
    artifacts:
      output_dir: evals/harness-runtime-v0.1/runs
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing required field 'approval_mode'"):
        runner.resolve_eval_environment(
            "incomplete",
            runner.EvalEnvironmentOverrides(environment_config=str(env_path)),
        )


@pytest.mark.unit
def test_environment_resolver_reports_unknown_environment() -> None:
    """未知 environment id 必须报告可用 id。"""

    runner = _load_runner_module()

    with pytest.raises(ValueError, match="unknown environment 'missing'"):
        runner.resolve_eval_environment("missing")


@pytest.mark.unit
def test_environment_resolver_rejects_invalid_profile_and_approval(tmp_path: Path) -> None:
    """非法 profile / approval_mode 必须被 resolver 拦截。"""

    runner = _load_runner_module()
    env_path = tmp_path / "environments.yaml"
    env_path.write_text(
        """
environments:
  invalid-profile:
    suite: evals/harness-runtime-v0.1
    mode: fixture
    profile: tiny
    approval_mode: auto_allow
    runner:
      max_turns: 50
    artifacts:
      output_dir: evals/harness-runtime-v0.1/runs
  invalid-approval:
    suite: evals/harness-runtime-v0.1
    mode: fixture
    profile: full
    approval_mode: maybe
    runner:
      max_turns: 50
    artifacts:
      output_dir: evals/harness-runtime-v0.1/runs
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="profile must be one of"):
        runner.resolve_eval_environment(
            "invalid-profile",
            runner.EvalEnvironmentOverrides(environment_config=str(env_path)),
        )
    with pytest.raises(ValueError, match="approval_mode must be one of"):
        runner.resolve_eval_environment(
            "invalid-approval",
            runner.EvalEnvironmentOverrides(environment_config=str(env_path)),
        )


@pytest.mark.unit
def test_environment_resolver_records_preset_key_presence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resolver 必须记录 preset 对应密钥是否存在。"""

    runner = _load_runner_module()
    monkeypatch.setenv("MINIMAX_API_KEY", "dummy-key")

    resolved = runner.resolve_eval_environment("minimax-full-ci")

    assert resolved.preset == "minimax-m3"
    assert resolved.api_keys_present == {"MINIMAX_API_KEY": True}


@pytest.mark.unit
def test_environment_resolver_rejects_mode_preset_conflict() -> None:
    """CLI 同时指定 fixture mode 和 preset 时必须显式报错。"""

    runner = _load_runner_module()

    with pytest.raises(ValueError, match="--preset requires --mode preset"):
        runner.resolve_eval_environment(
            "fixture-full",
            runner.EvalEnvironmentOverrides(mode="fixture", preset="minimax-m3"),
        )


@pytest.mark.unit
def test_environment_resolver_rejects_fixture_environment_with_preset(tmp_path: Path) -> None:
    """environment YAML 中 fixture mode 也不能携带 preset。"""

    runner = _load_runner_module()
    env_path = tmp_path / "environments.yaml"
    env_path.write_text(
        """
environments:
  fixture-with-preset:
    suite: evals/harness-runtime-v0.1
    mode: fixture
    preset: minimax-m3
    profile: full
    approval_mode: auto_allow
    runner:
      max_turns: 50
    artifacts:
      output_dir: evals/harness-runtime-v0.1/runs
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="fixture mode cannot be combined with preset"):
        runner.resolve_eval_environment(
            "fixture-with-preset",
            runner.EvalEnvironmentOverrides(environment_config=str(env_path)),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_python_api_runs_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Python API 必须能按 environment id 直接运行 suite。"""

    monkeypatch.delenv("KONGMING_HOME", raising=False)
    runner = _load_runner_module()

    summary = await runner.run_harness_environment(
        "fixture-full",
        runner.EvalEnvironmentOverrides(run_id="unit-python-api", output_dir=str(tmp_path)),
    )
    rerun_summary = await runner.run_harness_environment(
        "fixture-full",
        runner.EvalEnvironmentOverrides(run_id="unit-python-api", output_dir=str(tmp_path)),
    )

    assert summary["passed"] == summary["total"] == 9
    assert summary["environment_id"] == "fixture-full"
    assert summary["run_dir"] == str(tmp_path / "unit-python-api")
    assert rerun_summary["passed"] == rerun_summary["total"] == 9
    assert rerun_summary["run_dir"] == str(tmp_path / "unit-python-api")


@pytest.mark.unit
@pytest.mark.parametrize("bad_run_id", ["../escape", ".", "..", "...", ""])
@pytest.mark.asyncio
async def test_run_id_rejects_path_segments(bad_run_id: str) -> None:
    """run_id 必须是单段路径名，避免输出目录逃逸。"""

    runner = _load_runner_module()

    with pytest.raises(ValueError, match="run_id must contain"):
        await runner.run_harness_environment(
            "fixture-full",
            runner.EvalEnvironmentOverrides(run_id=bad_run_id),
        )


@pytest.mark.unit
def test_run_id_allows_dotted_names() -> None:
    """合法 dotted run id 必须保留。"""

    runner = _load_runner_module()

    assert runner._validate_run_id("run.v1") == "run.v1"


@pytest.mark.unit
def test_tool_execution_scorer_reports_missing_call(tmp_path: Path) -> None:
    """tool_execution scorer 必须报告缺失工具调用。"""

    runner = _load_runner_module()
    task = runner.Task(
        id="tool-execution-test",
        category="tool_execution",
        source="unit",
        prompt="use tools",
        scoring={
            "type": "tool_execution",
            "expected_calls": [
                {"name": "search_code", "arguments_contains": {"query": "Runner.run"}},
                {"name": "read_file", "arguments_contains": {"path": "src/core/runner.py"}},
            ],
            "final_contains": ["tool_result"],
            "min_turns": 2,
        },
        fixture_response=None,
        runtime={},
        path=tmp_path / "task.yaml",
    )
    events = [
        {
            "kind": "llm.response",
            "turn": 1,
            "payload": {
                "response": {
                    "message": {
                        "tool_calls": [
                            {
                                "call_id": "call-1",
                                "tool_name": "search_code",
                                "arguments": {"query": "Runner.run"},
                            }
                        ]
                    }
                }
            },
        },
        {"kind": "llm.request", "turn": 1, "payload": {}},
        {"kind": "llm.request", "turn": 2, "payload": {}},
        {
            "kind": "tool.call.end",
            "turn": 1,
            "payload": {
                "call_id": "call-1",
                "tool_name": "search_code",
                "ok": True,
                "content": "found",
                "data": None,
                "error_message": None,
            },
        },
    ]

    score = runner.score_tool_execution(task, "final mentions tool_result", events)

    assert score.passed is False
    assert score.score == 0.0
    assert "missing call read_file" in score.details["failures"]


@pytest.mark.unit
def test_arguments_contain_supports_nested_subset() -> None:
    """tool 参数子集匹配必须支持嵌套 dict/list。"""

    runner = _load_runner_module()

    assert runner._arguments_contain(
        {
            "query": "Runner.run implementation",
            "filters": {"paths": ["src/core/runner.py", "tests/unit/test_runner.py"]},
            "tags": [{"name": "runtime", "score": 1}, {"name": "tooling", "score": 2}],
        },
        {
            "query": "runner.run",
            "filters": {"paths": ["src/core/runner.py"]},
            "tags": [{"name": "runtime"}],
        },
    )


@pytest.mark.unit
def test_swebench_diff_requires_pass_to_pass(tmp_path: Path) -> None:
    """swebench_diff 必须声明至少一个 pass_to_pass 回归保护测试。"""

    runner = _load_runner_module()
    task = runner.Task(
        id="repo-fix-no-regression",
        category="repo_fix",
        source="unit",
        prompt="fix",
        scoring={
            "type": "swebench_diff",
            "base_files": {"src/app.py": "def value():\n    return 1\n"},
            "test_files": {
                "tests/test_app.py": (
                    "from src.app import value\n\ndef test_value():\n    assert value() == 2\n"
                )
            },
            "fail_to_pass": ["tests/test_app.py::test_value"],
            "pass_to_pass": [],
        },
        fixture_response="",
        runtime={},
        path=tmp_path / "task.yaml",
    )

    score = runner.score_response(task, "", tmp_path / "run")

    assert score.passed is False
    assert score.details["error"] == "scoring.pass_to_pass is required"


@pytest.mark.unit
def test_swebench_diff_stops_when_baseline_invalid(tmp_path: Path) -> None:
    """baseline 非法时必须短路，避免继续应用模型 diff。"""

    runner = _load_runner_module()
    task = runner.Task(
        id="repo-fix-baseline-invalid",
        category="repo_fix",
        source="unit",
        prompt="fix",
        scoring={
            "type": "swebench_diff",
            "base_files": {"src/app.py": "def value():\n    return 1\n"},
            "test_files": {
                "tests/test_app.py": (
                    "from src.app import value\n\ndef test_value():\n    assert value() == 1\n"
                )
            },
            "fail_to_pass": ["tests/test_app.py::test_value"],
            "pass_to_pass": ["tests/test_app.py::test_value"],
        },
        fixture_response="""diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
 def value():
-    return 1
+    return 2
""",
        runtime={},
        path=tmp_path / "task.yaml",
    )

    score = runner.score_response(task, task.fixture_response or "", tmp_path / "run")

    assert score.passed is False
    assert score.details["phase"] == "baseline-invalid"
    assert "apply" not in score.details


@pytest.mark.unit
def test_apply_model_diff_rejects_fuzzy_only_patch(tmp_path: Path) -> None:
    """diff 只能靠 fuzzy patch 应用时必须失败。"""

    runner = _load_runner_module()
    repo_dir = tmp_path / "repo"
    runner._init_repo_with_base(repo_dir, {"src/app.py": "line1\nline2\nline3\nline4\n"})
    diff_text = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,4 +1,4 @@
-lineA
+lineA changed
 lineB
 lineC
 lineD
"""

    result = runner._apply_model_diff(repo_dir, diff_text)

    assert result["applied"] is False
    assert result["strategy"] is None
    assert "patch-fuzz" not in result["output"]


@pytest.mark.unit
def test_apply_model_diff_rejects_paths_outside_sandbox(tmp_path: Path) -> None:
    """模型 diff 指向 sandbox 外路径时必须在 git apply 前拒绝。"""

    runner = _load_runner_module()
    repo_dir = tmp_path / "repo"
    runner._init_repo_with_base(repo_dir, {"src/app.py": "value = 1\n"})
    diff_text = """diff --git a/../../outside.txt b/../../outside.txt
--- a/../../outside.txt
+++ b/../../outside.txt
@@ -0,0 +1 @@
+owned
"""

    result = runner._apply_model_diff(repo_dir, diff_text)

    assert result["applied"] is False
    assert result["strategy"] == "rejected-outside-sandbox"
    assert "path escapes sandbox" in result["output"]


@pytest.mark.unit
def test_pytest_env_uses_sandbox_only_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pytest 子进程环境必须避免继承宿主 PYTHONPATH。"""

    runner = _load_runner_module()
    monkeypatch.setenv("PYTHONPATH", "/tmp/host-path")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")

    env = runner._pytest_env(tmp_path)

    assert env["PYTHONPATH"] == str(tmp_path)
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "OPENAI_API_KEY" not in env
