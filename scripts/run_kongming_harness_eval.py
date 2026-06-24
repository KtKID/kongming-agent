"""Kongming Harness Eval 执行器（唯一入口）。

本脚本用于运行 Kongming runtime 级 Harness Eval：它复用项目内真实
``NativeRuntime.build(...)`` 和 ``Runner.run()``，验证模型、工具、审批、
Session 和多轮 tool_result 回填组成的完整 agent harness 闭环。

支持两种运行模式：

- ``--mode fixture``（默认）：用题目自带 ``fixture_response`` / 期望 tool_calls
  驱动一个内置的伪 LLM provider，用于验证 harness 闭环本身和 session 落盘；
- ``--preset <id>``：按 ``config/setting.yaml`` 中 ``web.llm_presets`` 的 preset
  连真实模型，跑完整 agent 闭环。

关键执行流程：

1. 读取 ``--suite`` 下的 ``tasks/*.yaml``；
2. 为每道题创建隔离的 run 目录、``KONGMING_HOME`` 和 session id；
3. 按 scoring 类型决定是否注册 eval fake tools（``tool_execution``）；
4. 通过 ``NativeRuntime.run()`` 真实执行多轮（``--max-turns`` 控制上限）；
5. 根据 runner 事件和最终回答执行确定性评分，落盘 ``trajectory.json``、
   ``summary.json`` 和中文 ``report.md``。

关键函数：

- ``load_tasks``：加载并校验题集。
- ``score_response``：按 scoring 类型分发打分（json / exact_text / python_code /
  swebench_diff）。
- ``score_tool_execution``：检查真实工具调用轨迹、tool_result 和最终答案。
- ``run_suite_async``：执行整套评测并落盘 trajectory / summary / report。
- ``render_report``：生成中文 Markdown 报告。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

# 仓库根 .env 注入：load_config 只读 KONGMING_HOME/.env，而 eval 把 KONGMING_HOME
# 隔离到 task 子目录，必须主动把仓库根 .env 里的 *_API_KEY 注入到 os.environ。
try:
    from dotenv import load_dotenv as _load_dotenv
except ImportError:  # pragma: no cover - dotenv 是 runtime dep
    _load_dotenv = None  # type: ignore[assignment]
if _load_dotenv is not None:
    _REPO_DOTENV = _REPO_ROOT / ".env"
    if _REPO_DOTENV.exists():
        _load_dotenv(_REPO_DOTENV, override=False)

from core.agent_spec import AgentSpec  # noqa: E402
from core.contracts import (  # noqa: E402
    ApprovalAction,
    ApprovalRequest,
    Event,
    LLMRequest,
    LLMResponse,
    ToolContext,
)
from core.message import Message, ToolCall  # noqa: E402
from infrastructure.config import load_config  # noqa: E402
from infrastructure.config.models import Config  # noqa: E402
from infrastructure.llm_providers.provider_factory import apply_preset  # noqa: E402
from runtime_assembly.native_runtime import NativeRuntime  # noqa: E402
from sessions import SessionBootstrap, build_session  # noqa: E402
from tools.runtime.approval import AutoAllowApproval, InteractiveApproval  # noqa: E402
from tools.runtime.base import BaseBuiltinTool  # noqa: E402
from tools.runtime.registry import ToolRegistry  # noqa: E402

_DEFAULT_SUITE = Path("evals/harness-runtime-v0.1")
_DEFAULT_ENVIRONMENT_CONFIG = _DEFAULT_SUITE / "environments.yaml"
_DEFAULT_MAX_TURNS = 50
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_EVAL_INSTRUCTIONS = (
    "你是 Kongming Harness Eval 的被测 agent。"
    "需要使用工具时必须通过真实 tool_call 调用，不要伪造工具结果。"
)

# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    """表示单道评测题，输入为 YAML 字段，输出为运行期结构。"""

    id: str
    category: str
    source: str
    prompt: str
    scoring: dict[str, Any]
    fixture_response: str | None
    runtime: dict[str, Any]
    path: Path


@dataclass(frozen=True)
class ScoreResult:
    """表示单题评分结果，输入为打分细节，输出给汇总报告。"""

    passed: bool
    score: float
    details: dict[str, Any]


@dataclass(frozen=True)
class RuntimeTaskResult:
    """表示 runtime 单题结果，输入为执行产物，输出给 suite 汇总。"""

    final_content: str
    events: list[dict[str, Any]]
    score: ScoreResult
    duration_ms: int
    error: str | None
    result_status: str
    turn_count: int
    metadata: dict[str, Any]


@dataclass(frozen=True)
class EvalEnvironmentOverrides:
    """表示 CLI / Python 调用方的临时覆盖，输入为可选字段，输出给 resolver。"""

    suite: str | None = None
    mode: str | None = None
    preset: str | None = None
    config: str | None = None
    environment_config: str | None = None
    output_dir: str | None = None
    run_id: str | None = None
    max_turns: int | None = None
    profile: str | None = None
    approval_mode: str | None = None


@dataclass(frozen=True)
class ResolvedEvalEnvironment:
    """表示已解析的 eval 运行环境，输入给 suite runner，输出给 artifacts metadata。"""

    environment_id: str
    environment_config_path: Path | None
    environment_config_hash: str | None
    kongming_config_path: Path
    kongming_config_hash: str
    suite: Path
    mode: str
    preset: str | None
    profile: str
    approval_mode: str
    instructions_mode: str
    session_backend: str
    compactor_mode: str
    runner_max_turns: int
    output_dir: Path
    api_keys_present: dict[str, bool]
    override_sources: dict[str, str]

    def as_metadata(self) -> dict[str, Any]:
        """输出 JSON 友好的环境元数据，输入为空，输出 dict。"""

        return {
            "environment_id": self.environment_id,
            "environment_config_path": (
                str(self.environment_config_path) if self.environment_config_path else None
            ),
            "environment_config_hash": self.environment_config_hash,
            "kongming_config_path": str(self.kongming_config_path),
            "kongming_config_hash": self.kongming_config_hash,
            "suite": str(self.suite),
            "mode": self.mode,
            "model_preset": self.preset,
            "resolved_profile": self.profile,
            "approval_mode": self.approval_mode,
            "instructions_mode": self.instructions_mode,
            "session_backend": self.session_backend,
            "compactor_mode": self.compactor_mode,
            "runner_max_turns": self.runner_max_turns,
            "output_dir": str(self.output_dir),
            "api_keys_present": self.api_keys_present,
            "override_sources": self.override_sources,
        }


# ---------------------------------------------------------------------------
# YAML 加载与校验
# ---------------------------------------------------------------------------


def _utc_run_id() -> str:
    """生成 UTC run id，输入为空，输出适合路径使用的时间戳。"""

    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _validate_run_id(run_id: str) -> str:
    """校验 run id 为单段路径名，输入 run id，输出原值。"""

    if not run_id or not _RUN_ID_RE.fullmatch(run_id) or not run_id.strip("."):
        raise ValueError("run_id must contain only letters, digits, underscore, dash and dot")
    return run_id


def _read_yaml(path: Path) -> dict[str, Any]:
    """读取单个 YAML 文件，输入路径，输出字典。"""

    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: YAML root must be an object")
    return payload


def _require_str(payload: dict[str, Any], field: str, path: Path) -> str:
    """校验必填字符串字段，输入 YAML 字段名，输出字符串值。"""

    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: missing non-empty string field '{field}'")
    return value


def _validate_task_payload(payload: dict[str, Any], path: Path) -> Task:
    """校验题目 schema，输入 YAML 字典，输出 Task。"""

    task_id = _require_str(payload, "id", path)
    category = _require_str(payload, "category", path)
    prompt = _require_str(payload, "prompt", path)
    scoring = payload.get("scoring")
    if not isinstance(scoring, dict):
        raise ValueError(f"{path}: missing object field 'scoring'")
    scoring_type = scoring.get("type")
    if not isinstance(scoring_type, str) or not scoring_type:
        raise ValueError(f"{path}: missing scoring.type")
    fixture_response = payload.get("fixture_response")
    if fixture_response is not None and not isinstance(fixture_response, str):
        raise ValueError(f"{path}: fixture_response must be a string")
    source = payload.get("source", "self_built")
    if not isinstance(source, str):
        raise ValueError(f"{path}: source must be a string")
    runtime = payload.get("runtime", {})
    if runtime is None:
        runtime = {}
    if not isinstance(runtime, dict):
        raise ValueError(f"{path}: runtime must be an object when present")
    task_approval = runtime.get("approval_mode")
    if task_approval is not None and task_approval not in {"auto_allow", "interactive"}:
        raise ValueError(f"{path}: runtime.approval_mode must be auto_allow or interactive")
    return Task(
        id=task_id,
        category=category,
        source=source,
        prompt=prompt,
        scoring=scoring,
        fixture_response=fixture_response,
        runtime=dict(runtime),
        path=path,
    )


def load_tasks(suite_dir: Path) -> list[Task]:
    """加载 suite 中所有任务，输入 suite 路径，输出按 id 排序的 Task 列表。"""

    task_dir = suite_dir / "tasks"
    if not task_dir.is_dir():
        raise ValueError(f"task dir not found: {task_dir}")
    tasks = [_validate_task_payload(_read_yaml(path), path) for path in task_dir.glob("*.yaml")]
    if not tasks:
        raise ValueError(f"no tasks found in {task_dir}")
    seen: set[str] = set()
    duplicates: list[str] = []
    for task in tasks:
        if task.id in seen:
            duplicates.append(task.id)
        seen.add(task.id)
    if duplicates:
        raise ValueError(f"duplicate task ids: {', '.join(sorted(duplicates))}")
    return sorted(tasks, key=lambda item: item.id)


# ---------------------------------------------------------------------------
# 输出文本提取（JSON / 代码 / diff 等）
# ---------------------------------------------------------------------------


def _strip_code_fence(text: str) -> str:
    """去除单层 Markdown code fence，输入模型文本，输出内部正文。"""

    stripped = text.strip()
    match = re.fullmatch(r"```(?:[a-zA-Z0-9_-]+)?\s*(.*?)\s*```", stripped, re.DOTALL)
    return match.group(1).strip() if match else stripped


def _extract_json(text: str) -> Any:
    """从模型输出中提取 JSON，输入文本，输出解析后的 JSON 值。"""

    cleaned = _strip_code_fence(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


def _extract_python_code(text: str) -> str:
    """从模型输出中提取 Python 代码，输入文本，输出代码字符串。"""

    cleaned = text.strip()
    match = re.search(r"```python\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip() + "\n"
    match = re.search(r"```\s*(.*?)\s*```", cleaned, re.DOTALL)
    if match:
        return match.group(1).strip() + "\n"
    return cleaned + ("\n" if cleaned else "")


def _extract_diff(text: str) -> str:
    """从模型输出中提取 unified diff，输入文本，输出适合 git apply 的 patch 字符串。

    支持三种来源：```diff fenced block、裸 code fence、整段裸文本；
    并把首个 `diff --git` / `--- ` 之前的自然语言前缀剥掉，保证 git apply 不被污染。
    """

    match = re.search(r"```(?:diff|patch)\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    patch = match.group(1) if match else _strip_code_fence(text)
    lines = patch.splitlines()
    start = 0
    for index, line in enumerate(lines):
        if line.startswith("diff --git ") or line.startswith("--- "):
            start = index
            break
    patch_body = "\n".join(lines[start:]).strip("\n")
    return patch_body + "\n" if patch_body else ""


# ---------------------------------------------------------------------------
# sandbox / 文件 / pytest / git 辅助
# ---------------------------------------------------------------------------


def _write_file(root: Path, relative_path: str, content: str) -> Path:
    """在 sandbox 内写文件，输入根目录、相对路径和内容，输出绝对路径。"""

    target = (root / relative_path).resolve()
    root_resolved = root.resolve()
    if root_resolved not in target.parents and target != root_resolved:
        raise ValueError(f"path escapes sandbox: {relative_path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _pytest_env(sandbox_dir: Path) -> dict[str, str]:
    """构造 pytest 子进程环境，输入 sandbox 目录，输出收窄后的环境变量。"""

    keep_keys = {
        "PATH",
        "LANG",
        "LC_ALL",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
    }
    env = {
        key: value for key, value in os.environ.items() if key in keep_keys or key.startswith("LC_")
    }
    home_dir = sandbox_dir / ".home"
    temp_dir = sandbox_dir / ".tmp"
    home_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home_dir)
    env["TMPDIR"] = str(temp_dir)
    env["TEMP"] = str(temp_dir)
    env["TMP"] = str(temp_dir)
    env["PYTHONPATH"] = str(sandbox_dir)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_pytest(sandbox_dir: Path, timeout_seconds: int) -> dict[str, Any]:
    """在 sandbox 内运行 pytest，输入目录和超时，输出进程结果。"""

    started = time.monotonic()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--rootdir",
            str(sandbox_dir),
            "--noconftest",
            "-p",
            "no:cacheprovider",
        ],
        cwd=sandbox_dir,
        env=_pytest_env(sandbox_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
        check=False,
    )
    return {
        "exit_code": proc.returncode,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "output": proc.stdout,
    }


def _run_pytest_nodes(
    sandbox_dir: Path, node_ids: list[str], timeout_seconds: int
) -> dict[str, Any]:
    """在 sandbox 内按 pytest node id 选择性运行测试，输入目录/节点列表/超时，输出进程结果。

    node id 形如 `tests/test_x.py::test_name`；调用方负责保证列表非空，
    避免把“未运行任何测试”误判为通过。
    """

    if not node_ids:
        return {
            "exit_code": 2,
            "duration_ms": 0,
            "output": "no pytest nodes configured",
            "skipped": True,
        }
    started = time.monotonic()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--rootdir",
            str(sandbox_dir),
            "--noconftest",
            "-p",
            "no:cacheprovider",
            *node_ids,
        ],
        cwd=sandbox_dir,
        env=_pytest_env(sandbox_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
        check=False,
    )
    return {
        "exit_code": proc.returncode,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "output": proc.stdout,
    }


def _run_git(
    args: list[str], cwd: Path, *, stdin: str | None = None, timeout_seconds: int = 30
) -> subprocess.CompletedProcess[str]:
    """在指定目录执行一次 git 命令，输入参数列表/工作目录/可选 stdin，输出进程结果。

    通过命令级 `-c user.*` 提供提交身份，绝不写入任何持久 git config。
    """

    base = [
        "git",
        "-c",
        "user.email=eval@kongming.local",
        "-c",
        "user.name=kongming-eval",
        "-c",
        "commit.gpgsign=false",
    ]
    command = base + args
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            text=True,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            command,
            124,
            stdout=f"git command timed out after {timeout_seconds}s: {exc}",
        )


def _init_repo_with_base(repo_dir: Path, base_files: dict[str, Any]) -> None:
    """初始化 SWE-bench 风格 base 仓库，输入仓库目录和 base 文件映射，无返回值。"""

    repo_dir.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-q"], repo_dir)
    for relative_path, content in base_files.items():
        _write_file(repo_dir, str(relative_path), str(content))
    _run_git(["add", "-A"], repo_dir)
    _run_git(["commit", "-q", "-m", "base commit"], repo_dir)


def _diff_path_token(line: str) -> list[str]:
    """从 diff header 行提取路径 token，输入一行 patch，输出路径列表。"""

    try:
        parts = shlex.split(line)
    except ValueError:
        return []
    if len(parts) >= 4 and parts[0] == "diff" and parts[1] == "--git":
        return [parts[2], parts[3]]
    if len(parts) >= 2 and parts[0] in {"---", "+++"}:
        return [parts[1]]
    return []


def _normalize_diff_path_token(token: str) -> str | None:
    """规范化 diff path token，输入原始 token，输出仓库相对路径或 None。"""

    if token == "/dev/null":
        return None
    if token.startswith(("a/", "b/")):
        token = token[2:]
    return token


def _validate_diff_paths(repo_dir: Path, diff_text: str) -> str | None:
    """校验 diff 路径是否留在 repo 内，输入 repo 和 patch，输出错误字符串或 None。"""

    root = repo_dir.resolve()
    for line in diff_text.splitlines():
        if line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            return f"unsupported diff metadata: {line}"
        if line.startswith(("GIT binary patch", "Binary files ")):
            return f"unsupported binary diff: {line}"
        if not (
            line.startswith("diff --git ") or line.startswith("--- ") or line.startswith("+++ ")
        ):
            continue
        tokens = _diff_path_token(line)
        if not tokens:
            return f"invalid diff header: {line}"
        for token in tokens:
            normalized = _normalize_diff_path_token(token)
            if normalized is None:
                continue
            path = PurePosixPath(normalized)
            if path.is_absolute() or ".." in path.parts or not normalized:
                return f"path escapes sandbox: {token}"
            if path.parts and path.parts[0] == ".git":
                return f"git metadata path is unsupported: {token}"
            target = (root / Path(*path.parts)).resolve()
            if root not in target.parents and target != root:
                return f"path escapes sandbox: {token}"
    return None


def _validate_repo_symlinks(repo_dir: Path) -> str | None:
    """校验 repo 内符号链接不逃出 sandbox，输入 repo，输出错误字符串或 None。"""

    root = repo_dir.resolve()
    for path in repo_dir.rglob("*"):
        if not path.is_symlink():
            continue
        target = path.resolve(strict=False)
        if root not in target.parents and target != root:
            relative_path = path.relative_to(repo_dir)
            return f"symlink escapes sandbox: {relative_path} -> {target}"
    return None


def _apply_model_diff(repo_dir: Path, diff_text: str) -> dict[str, Any]:
    """把模型产出的 diff 应用到 base 仓库，输入仓库目录和 patch 文本，输出应用结果。

    应用策略限定为普通 ``git apply``。不启用 3way / fuzzy patch，
    避免合并回退或上下文错位时把错误补丁误判为可接受结果。
    """

    if not diff_text.strip():
        return {"applied": False, "strategy": None, "output": "empty diff"}
    path_error = _validate_diff_paths(repo_dir, diff_text)
    if path_error:
        return {
            "applied": False,
            "strategy": "rejected-outside-sandbox",
            "output": path_error,
        }
    primary = _run_git(["apply", "--whitespace=nowarn", "-"], repo_dir, stdin=diff_text)
    if primary.returncode == 0:
        symlink_error = _validate_repo_symlinks(repo_dir)
        if symlink_error:
            return {
                "applied": False,
                "strategy": "rejected-outside-sandbox",
                "output": symlink_error,
            }
        return {"applied": True, "strategy": "git-apply", "output": primary.stdout}
    return {"applied": False, "strategy": None, "output": f"git-apply:\n{primary.stdout}"}


# ---------------------------------------------------------------------------
# 评分函数
# ---------------------------------------------------------------------------


def _score_exact_text(task: Task, response: str) -> ScoreResult:
    """执行短答案精确匹配，输入任务和响应，输出评分。"""

    expected = str(task.scoring.get("expected", ""))
    actual = _strip_code_fence(response).strip()
    if not task.scoring.get("case_sensitive", True):
        passed = actual.lower() == expected.lower()
    else:
        passed = actual == expected
    return ScoreResult(
        passed=passed,
        score=1.0 if passed else 0.0,
        details={"expected": expected, "actual": actual},
    )


def _contains_value(actual: Any, expected: Any) -> bool:
    """判断实际值是否包含期望值，输入任意 JSON 值，输出布尔结果。"""

    if isinstance(actual, str) and isinstance(expected, str):
        return expected.lower() in actual.lower()
    if isinstance(actual, list):
        return expected in actual
    return actual == expected


def _score_json(task: Task, response: str) -> ScoreResult:
    """执行 JSON 字段和值检查，输入任务和响应，输出评分。"""

    try:
        actual = _extract_json(response)
    except (json.JSONDecodeError, ValueError) as exc:
        return ScoreResult(False, 0.0, {"error": f"json parse failed: {exc}", "actual": response})
    if not isinstance(actual, dict):
        return ScoreResult(False, 0.0, {"error": "json root is not object", "actual": actual})
    failures: list[str] = []
    for field, expected in dict(task.scoring.get("equals", {})).items():
        if actual.get(field) != expected:
            failures.append(f"{field}: expected {expected!r}, got {actual.get(field)!r}")
    for field, expected_values in dict(task.scoring.get("list_contains", {})).items():
        actual_values = actual.get(field)
        if not isinstance(actual_values, list):
            failures.append(f"{field}: expected list")
            continue
        for expected in expected_values:
            if expected not in actual_values:
                failures.append(f"{field}: missing {expected!r}")
    for field, expected_values in dict(task.scoring.get("contains", {})).items():
        actual_value = actual.get(field)
        for expected in expected_values:
            if not _contains_value(actual_value, expected):
                failures.append(f"{field}: missing text {expected!r}")
    passed = not failures
    return ScoreResult(passed, 1.0 if passed else 0.0, {"actual": actual, "failures": failures})


def _score_python_code(task: Task, response: str, sandbox_dir: Path) -> ScoreResult:
    """执行 Python 代码题打分，输入任务、响应和 sandbox，输出评分。"""

    solution_file = str(task.scoring.get("solution_file", "solution.py"))
    _write_file(sandbox_dir, solution_file, _extract_python_code(response))
    for test_spec in task.scoring.get("tests", []):
        _write_file(sandbox_dir, str(test_spec["path"]), str(test_spec["content"]))
    result = _run_pytest(sandbox_dir, int(task.scoring.get("timeout_seconds", 15)))
    passed = result["exit_code"] == 0
    return ScoreResult(passed, 1.0 if passed else 0.0, {"pytest": result})


def _score_swebench_diff(task: Task, response: str, sandbox_dir: Path) -> ScoreResult:
    """执行 SWE-bench 风格 diff 评分，输入任务/模型响应/sandbox，输出评分结果。

    1. 用 base_files 建 base 仓库并 commit；写入评测方持有的 test_files；
    2. 基线校验：未打补丁时 FAIL_TO_PASS 必须失败、PASS_TO_PASS 必须通过；
    3. git apply 模型 diff；
    4. 复跑两组测试，FAIL_TO_PASS 全转通过且 PASS_TO_PASS 不退化才判通过。
    """

    scoring = task.scoring
    base_files = dict(scoring.get("base_files", {}))
    test_files = dict(scoring.get("test_files", {}))
    fail_to_pass = [str(node) for node in scoring.get("fail_to_pass", [])]
    pass_to_pass = [str(node) for node in scoring.get("pass_to_pass", [])]
    timeout_seconds = int(scoring.get("timeout_seconds", 30))
    if not base_files:
        return ScoreResult(False, 0.0, {"error": "scoring.base_files is required"})
    if not fail_to_pass:
        return ScoreResult(False, 0.0, {"error": "scoring.fail_to_pass is required"})
    if not pass_to_pass:
        return ScoreResult(False, 0.0, {"error": "scoring.pass_to_pass is required"})

    repo_dir = sandbox_dir / "repo"
    _init_repo_with_base(repo_dir, base_files)
    for relative_path, content in test_files.items():
        _write_file(repo_dir, str(relative_path), str(content))

    baseline_fail = _run_pytest_nodes(repo_dir, fail_to_pass, timeout_seconds)
    baseline_pass = _run_pytest_nodes(repo_dir, pass_to_pass, timeout_seconds)
    baseline_valid = baseline_fail["exit_code"] != 0 and baseline_pass["exit_code"] == 0
    if not baseline_valid:
        return ScoreResult(
            False,
            0.0,
            {
                "phase": "baseline-invalid",
                "baseline_valid": False,
                "fail_to_pass": {"before": baseline_fail},
                "pass_to_pass": {"before": baseline_pass},
            },
        )

    diff_text = _extract_diff(response)
    apply_result = _apply_model_diff(repo_dir, diff_text)
    if not apply_result["applied"]:
        return ScoreResult(
            False,
            0.0,
            {
                "phase": "apply",
                "baseline_valid": baseline_valid,
                "apply": apply_result,
                "extracted_diff": diff_text,
            },
        )

    post_fail = _run_pytest_nodes(repo_dir, fail_to_pass, timeout_seconds)
    post_pass = _run_pytest_nodes(repo_dir, pass_to_pass, timeout_seconds)
    fail_to_pass_resolved = post_fail["exit_code"] == 0
    pass_to_pass_kept = post_pass["exit_code"] == 0
    passed = baseline_valid and fail_to_pass_resolved and pass_to_pass_kept
    return ScoreResult(
        passed,
        1.0 if passed else 0.0,
        {
            "phase": "evaluate",
            "baseline_valid": baseline_valid,
            "fail_to_pass_resolved": fail_to_pass_resolved,
            "pass_to_pass_kept": pass_to_pass_kept,
            "apply": apply_result,
            "fail_to_pass": {"before": baseline_fail, "after": post_fail},
            "pass_to_pass": {"before": baseline_pass, "after": post_pass},
        },
    )


def _arguments_contain(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    """检查工具参数是否包含期望字段，输入实际和期望参数，输出布尔结果。"""

    for key, expected_value in expected.items():
        if key not in actual:
            return False
        if not _expected_value_matches(actual[key], expected_value):
            return False
    return True


def _expected_value_matches(actual_value: Any, expected_value: Any) -> bool:
    """递归执行参数子集匹配，输入实际值和期望值，输出布尔结果。"""

    if isinstance(expected_value, str) and isinstance(actual_value, str):
        return expected_value.lower() in actual_value.lower()
    if isinstance(expected_value, dict):
        if not isinstance(actual_value, dict):
            return False
        return all(
            key in actual_value and _expected_value_matches(actual_value[key], nested_expected)
            for key, nested_expected in expected_value.items()
        )
    if isinstance(expected_value, list):
        if not isinstance(actual_value, list):
            return False
        return all(
            any(_expected_value_matches(actual_item, expected_item) for actual_item in actual_value)
            for expected_item in expected_value
        )
    return actual_value == expected_value


def score_response(task: Task, response: str, task_run_dir: Path) -> ScoreResult:
    """按 scoring 类型分发打分，输入任务、响应和运行目录，输出评分结果。"""

    scoring_type = task.scoring["type"]
    sandbox_dir = task_run_dir / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    try:
        if scoring_type == "exact_text":
            return _score_exact_text(task, response)
        if scoring_type == "json":
            return _score_json(task, response)
        if scoring_type == "python_code":
            return _score_python_code(task, response, sandbox_dir)
        if scoring_type == "swebench_diff":
            return _score_swebench_diff(task, response, sandbox_dir)
        raise ValueError(f"unsupported scoring type: {scoring_type}")
    finally:
        shutil.rmtree(sandbox_dir, ignore_errors=True)


def _tool_calls_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从 llm.response 事件提取工具调用，输入事件列表，输出调用轨迹。"""

    calls: list[dict[str, Any]] = []
    for event in events:
        if event.get("kind") != "llm.response":
            continue
        message = event.get("payload", {}).get("response", {}).get("message", {})
        for call in message.get("tool_calls") or []:
            calls.append(
                {
                    "call_id": call.get("call_id"),
                    "name": call.get("tool_name"),
                    "arguments": call.get("arguments") or {},
                    "turn": event.get("turn"),
                }
            )
    return calls


def _tool_results_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从 tool.call.end 事件提取执行结果，输入事件列表，输出结果轨迹。"""

    results: list[dict[str, Any]] = []
    for event in events:
        if event.get("kind") != "tool.call.end":
            continue
        payload = event.get("payload", {})
        results.append(
            {
                "call_id": payload.get("call_id"),
                "name": payload.get("tool_name"),
                "ok": payload.get("ok"),
                "content": payload.get("content"),
                "data": payload.get("data"),
                "error_message": payload.get("error_message"),
                "turn": event.get("turn"),
            }
        )
    return results


def score_tool_execution(
    task: Task, final_content: str, events: list[dict[str, Any]]
) -> ScoreResult:
    """执行 tool execution 评分，输入任务、最终回答和事件，输出评分。"""

    calls = _tool_calls_from_events(events)
    results = _tool_results_from_events(events)
    failures: list[str] = []
    cursor = 0
    for expected in task.scoring.get("expected_calls", []):
        expected_name = expected.get("name")
        expected_arguments = expected.get("arguments_contains", {})
        matched_index = None
        for index in range(cursor, len(calls)):
            call = calls[index]
            if call.get("name") != expected_name:
                continue
            arguments = call.get("arguments", {})
            if isinstance(arguments, dict) and _arguments_contain(arguments, expected_arguments):
                matched_index = index
                break
        if matched_index is None:
            failures.append(f"missing call {expected_name}")
        else:
            cursor = matched_index + 1

    failed_results = [result for result in results if not result.get("ok")]
    if failed_results:
        failures.append(f"tool execution failed: {failed_results}")

    lowered_final = final_content.lower()
    for expected_text in task.scoring.get("final_contains", []):
        if str(expected_text).lower() not in lowered_final:
            failures.append(f"final missing {expected_text!r}")

    min_turns = int(task.scoring.get("min_turns", 2))
    llm_turns = [event for event in events if event.get("kind") == "llm.request"]
    if len(llm_turns) < min_turns:
        failures.append(f"expected at least {min_turns} llm turns, got {len(llm_turns)}")

    passed = not failures
    return ScoreResult(
        passed=passed,
        score=1.0 if passed else 0.0,
        details={
            "tool_calls": calls,
            "tool_results": results,
            "final": final_content,
            "failures": failures,
        },
    )


# ---------------------------------------------------------------------------
# 运行时装配（fixture LLM、eval fake tools、session factory、隔离 home）
# ---------------------------------------------------------------------------


class RecordingEventSink:
    """记录 Runner 事件，输入为 Event，输出为内存中的 JSON 友好列表。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, event: Event) -> None:
        """接收单条事件，输入 Event，无返回值。"""

        self.events.append(
            {
                "kind": event.kind,
                "run_id": event.run_id,
                "turn": event.turn,
                "timestamp_ms": event.timestamp_ms,
                "payload": event.payload,
            }
        )


class FixtureRuntimeLLM:
    """fixture 模式 provider，输入 Task，输出可驱动真实 Runner 的 LLMResponse。"""

    def __init__(self, task: Task) -> None:
        self._task = task
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """按 task fixture 生成响应，输入 LLMRequest，输出 LLMResponse。"""

        self.requests.append(request)
        if self._task.scoring.get("type") == "tool_execution" and not _has_tool_result(
            request.messages
        ):
            return LLMResponse(
                message=Message.assistant(tool_calls=_fixture_tool_calls(self._task)),
                finish_reason="tool_calls",
            )
        return LLMResponse(
            message=Message.assistant(self._task.fixture_response or ""),
            finish_reason="stop",
        )


class EvalSearchCodeTool(BaseBuiltinTool):
    """评测用代码搜索工具，输入 query，输出固定代码位置。"""

    name = "search_code"
    description = "Search indexed source code and return matching files and symbols."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """执行固定搜索，输入 query，输出 Runner.run 定义位置。"""

        query = str(args.get("query", ""))
        if "Runner.run" not in query:
            return (
                f"未找到精确匹配：{query}",
                {"matches": []},
            )
        return (
            "找到 1 个匹配：src/core/runner.py:135 async def run(...)",
            {
                "matches": [
                    {
                        "path": "src/core/runner.py",
                        "line": 135,
                        "symbol": "Runner.run",
                    }
                ]
            },
        )


class EvalReadFileTool(BaseBuiltinTool):
    """评测用文件读取工具，输入 path，输出固定源码片段。"""

    name = "read_file"
    description = "Read a repository file by path."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """执行固定文件读取，输入 path，输出 Runner.run 工具闭环片段。"""

        path = str(args.get("path", ""))
        if path != "src/core/runner.py":
            raise ValueError(f"unexpected path: {path}")
        return (
            "src/core/runner.py 摘要：Runner.run 调用 _drive_turns；"
            "_drive_turns 发现 assistant tool_calls 后执行工具；"
            "工具结果以 role='tool' 的 tool_result 写回 session，然后进入下一轮 LLM 请求。",
            {
                "path": path,
                "contains": ["Runner.run", "tool_calls", "tool_result", "session"],
            },
        )


class EvalListMcpServersTool(BaseBuiltinTool):
    """评测用 MCP server 列表工具，输入为空，输出 xcodeatlas server。"""

    name = "list_mcp_servers"
    description = "List connected MCP servers."
    input_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}, "required": []}

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """返回固定 MCP server 列表，输入为空，输出 xcodeatlas。"""

        return (
            "可用 MCP server：xcodeatlas",
            {"servers": [{"id": "xcodeatlas", "description": "Code graph and dependency atlas"}]},
        )


class EvalListMcpToolsTool(BaseBuiltinTool):
    """评测用 MCP tool 列表工具，输入 server_id，输出 graph 工具。"""

    name = "list_mcp_tools"
    description = "List tools exposed by an MCP server."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"server_id": {"type": "string"}},
        "required": ["server_id"],
    }

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """返回固定 MCP tool 列表，输入 server_id，输出 graph 工具。"""

        server_id = str(args.get("server_id", ""))
        if server_id != "xcodeatlas":
            raise ValueError(f"unknown server_id: {server_id}")
        return (
            "xcodeatlas 可用工具：graph(format: summary|json), find(query), read(path)",
            {"tools": [{"name": "graph", "args": {"format": "summary"}}]},
        )


class EvalCallMcpTool(BaseBuiltinTool):
    """评测用 MCP tool 调用工具，输入 server/tool/args，输出依赖图摘要。"""

    name = "call_mcp_tool"
    description = "Call a tool on an MCP server."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "server_id": {"type": "string"},
            "tool_name": {"type": "string"},
            "args": {"type": "object"},
        },
        "required": ["server_id", "tool_name", "args"],
    }

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """执行固定 MCP graph 调用，输入调用参数，输出依赖图摘要。"""

        server_id = str(args.get("server_id", ""))
        tool_name = str(args.get("tool_name", ""))
        if server_id != "xcodeatlas" or tool_name != "graph":
            raise ValueError(f"unexpected MCP call: {server_id}.{tool_name}")
        return (
            "xcodeatlas dependency graph summary: core -> tools -> runtime_assembly; "
            "runtime_assembly -> infrastructure; hosts -> runtime_assembly。",
            {
                "server_id": server_id,
                "tool_name": tool_name,
                "modules": ["core", "tools", "runtime_assembly", "infrastructure", "hosts"],
            },
        )


def build_eval_tools() -> ToolRegistry:
    """构造评测 fake tools，输入为空，输出独立 ToolRegistry。

    fixture 模式直接把该 registry 传入 NativeRuntime.build，不与生产 builtin tools
    合并，确保评测工具名由确定性 fake tools 接管。
    """

    return ToolRegistry(
        [
            EvalSearchCodeTool(),
            EvalReadFileTool(),
            EvalListMcpServersTool(),
            EvalListMcpToolsTool(),
            EvalCallMcpTool(),
        ]
    )


def _has_tool_result(messages: tuple[Message, ...]) -> bool:
    """判断请求历史里是否已有 tool result，输入消息元组，输出布尔值。"""

    return any(message.role == "tool" for message in messages)


def _fixture_tool_calls(task: Task) -> list[ToolCall]:
    """按 scoring.expected_calls 构造 fixture tool calls，输入 Task，输出调用列表。"""

    calls: list[ToolCall] = []
    for index, expected in enumerate(task.scoring.get("expected_calls", []), start=1):
        tool_name = str(expected["name"])
        arguments = dict(_fixture_default_arguments(tool_name))
        arguments.update(dict(expected.get("arguments") or {}))
        if expected.get("name") == "call_mcp_tool":
            arguments.setdefault("args", {"format": "summary"})
        calls.append(
            ToolCall(
                call_id=f"fixture-call-{index}",
                tool_name=tool_name,
                arguments=arguments,
            )
        )
    return calls


def _fixture_default_arguments(tool_name: str) -> dict[str, Any]:
    """返回 fixture fake tool 的最小可运行参数，输入工具名，输出参数字典。"""

    if tool_name == "search_code":
        return {"query": "Runner.run"}
    if tool_name == "read_file":
        return {"path": "src/core/runner.py"}
    if tool_name == "list_mcp_tools":
        return {"server_id": "xcodeatlas"}
    if tool_name == "call_mcp_tool":
        return {"server_id": "xcodeatlas", "tool_name": "graph", "args": {"format": "summary"}}
    return {}


class EvalNoopCompactor:
    """评测 profile 用 Noop compactor，输入消息历史，输出原样副本。"""

    async def compact(self, history: Sequence[Message]) -> list[Message]:
        """返回原样消息历史，输入消息序列，输出 list 副本。"""

        return list(history)


def _sha256_file(path: Path) -> str:
    """计算文件 sha256，输入路径，输出 sha256:<hex> 字符串。"""

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_repo_path(value: str | Path) -> Path:
    """把相对仓库路径解析为绝对路径，输入字符串或 Path，输出绝对 Path。"""

    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (_REPO_ROOT / path).resolve()


def _as_mapping(value: Any, *, field: str, path: Path) -> dict[str, Any]:
    """校验 YAML 子对象，输入任意值，输出 dict。"""

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{path}: {field} must be an object")
    return dict(value)


def _load_environment_entry(
    environment_id: str,
    environment_config_path: Path,
) -> dict[str, Any]:
    """从 environments.yaml 加载单个 environment，输入 id 和路径，输出配置字典。"""

    if not environment_config_path.exists():
        raise ValueError(f"environment config not found: {environment_config_path}")
    payload = _read_yaml(environment_config_path)
    environments = payload.get("environments")
    if not isinstance(environments, dict):
        raise ValueError(f"{environment_config_path}: missing object field 'environments'")
    entry = environments.get(environment_id)
    if not isinstance(entry, dict):
        available = ", ".join(sorted(str(key) for key in environments)) or "<none>"
        raise ValueError(
            f"unknown environment {environment_id!r}; available environments: {available}"
        )
    return dict(entry)


def _require_environment_field(
    entry: dict[str, Any],
    field: str,
    *,
    path: Path,
    environment_id: str,
) -> None:
    """校验 environment 必填字段存在，输入 entry 和字段名，无返回值。"""

    cursor: Any = entry
    for part in field.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            raise ValueError(
                f"{path}: environment {environment_id!r} missing required field {field!r}"
            )
        cursor = cursor[part]


def _reject_unknown_environment_fields(
    entry: dict[str, Any],
    *,
    path: Path,
    environment_id: str,
) -> None:
    """拒绝 environment 未知字段，输入 entry，无返回值。"""

    allowed_top_level = {
        "suite",
        "mode",
        "preset",
        "profile",
        "approval_mode",
        "runner",
        "artifacts",
    }
    allowed_nested = {
        "runner": {"max_turns"},
        "artifacts": {"output_dir"},
    }
    extras = sorted(set(entry) - allowed_top_level)
    if extras:
        joined = ", ".join(extras)
        raise ValueError(f"{path}: environment {environment_id!r} has unknown fields: {joined}")
    for field, allowed in allowed_nested.items():
        nested = entry.get(field)
        if nested is None:
            continue
        if not isinstance(nested, dict):
            raise ValueError(
                f"{path}: environment {environment_id!r} field {field!r} must be object"
            )
        nested_extras = sorted(set(nested) - allowed)
        if nested_extras:
            joined = ", ".join(f"{field}.{key}" for key in nested_extras)
            raise ValueError(f"{path}: environment {environment_id!r} has unknown fields: {joined}")


def _validate_environment_entry(
    entry: dict[str, Any],
    *,
    path: Path,
    environment_id: str,
) -> None:
    """校验单个 environment schema，输入配置字典，无返回值。"""

    _reject_unknown_environment_fields(entry, path=path, environment_id=environment_id)
    required_fields = (
        "suite",
        "mode",
        "profile",
        "approval_mode",
        "runner.max_turns",
        "artifacts.output_dir",
    )
    for field in required_fields:
        _require_environment_field(entry, field, path=path, environment_id=environment_id)
    if entry.get("mode") == "preset":
        _require_environment_field(entry, "preset", path=path, environment_id=environment_id)


def _choose_value(
    field: str,
    *,
    environment_value: Any,
    override_value: Any,
    default_value: Any,
    override_sources: dict[str, str],
) -> Any:
    """按 CLI 覆盖、environment、默认值顺序取值，输入三层值，输出最终值。"""

    if override_value is not None:
        override_sources[field] = "cli"
        return override_value
    if environment_value is not None:
        override_sources[field] = "environment"
        return environment_value
    override_sources[field] = "default"
    return default_value


def _validate_choice(value: Any, *, field: str, choices: set[str]) -> str:
    """校验枚举字段，输入任意值，输出字符串。"""

    if not isinstance(value, str) or value not in choices:
        allowed = ", ".join(sorted(choices))
        raise ValueError(f"{field} must be one of: {allowed}")
    return value


def _validate_positive_int(value: Any, *, field: str) -> int:
    """校验正整数，输入任意值，输出 int。"""

    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if result <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return result


def _profile_modes(profile: str) -> tuple[str, str, str]:
    """解析 profile 的指令、session、compactor 模式，输入 profile，输出三元组。"""

    if profile == "baseline-min":
        return "empty", "memory", "noop-script"
    if profile == "full":
        return "eval_default", "file", "config"
    raise ValueError("profile must be one of: baseline-min, full")


def _overrides_from_args(args: argparse.Namespace) -> EvalEnvironmentOverrides:
    """从 argparse Namespace 提取覆盖项，输入 args，输出 overrides。"""

    return EvalEnvironmentOverrides(
        suite=args.suite,
        mode=args.mode,
        preset=args.preset,
        config=args.config,
        environment_config=args.environment_config,
        output_dir=args.output_dir,
        run_id=args.run_id,
        max_turns=args.max_turns,
        profile=args.profile,
        approval_mode=args.approval_mode,
    )


def _api_keys_present(config: Config, preset_id: str | None) -> dict[str, bool]:
    """记录 preset 关联密钥是否存在，输入 Config 和 preset id，输出 env 名到布尔值。"""

    if not preset_id:
        return {}
    preset = _find_preset(config, preset_id)
    if not preset.api_key_env:
        return {}
    return {preset.api_key_env: bool(os.environ.get(preset.api_key_env))}


def resolve_eval_environment(
    environment_id: str | None,
    overrides: EvalEnvironmentOverrides | None = None,
) -> ResolvedEvalEnvironment:
    """解析 eval 环境预设，输入 environment id 和覆盖项，输出 resolved environment。"""

    resolved_overrides = overrides or EvalEnvironmentOverrides()
    override_sources: dict[str, str] = {}
    environment_config_path = (
        _resolve_repo_path(resolved_overrides.environment_config)
        if resolved_overrides.environment_config
        else _resolve_repo_path(_DEFAULT_ENVIRONMENT_CONFIG)
    )
    entry: dict[str, Any] = {}
    environment_hash: str | None = None
    if environment_id:
        entry = _load_environment_entry(environment_id, environment_config_path)
        _validate_environment_entry(
            entry,
            path=environment_config_path,
            environment_id=environment_id,
        )
        environment_hash = _sha256_file(environment_config_path)

    runner_entry = _as_mapping(entry.get("runner"), field="runner", path=environment_config_path)
    artifacts_entry = _as_mapping(
        entry.get("artifacts"), field="artifacts", path=environment_config_path
    )

    suite_raw = _choose_value(
        "suite",
        environment_value=entry.get("suite"),
        override_value=resolved_overrides.suite,
        default_value=str(_DEFAULT_SUITE),
        override_sources=override_sources,
    )
    suite = _resolve_repo_path(str(suite_raw))

    mode_raw = _choose_value(
        "mode",
        environment_value=entry.get("mode"),
        override_value=resolved_overrides.mode,
        default_value="fixture",
        override_sources=override_sources,
    )
    preset_raw = _choose_value(
        "preset",
        environment_value=entry.get("preset"),
        override_value=resolved_overrides.preset,
        default_value=None,
        override_sources=override_sources,
    )
    if resolved_overrides.preset is not None:
        if resolved_overrides.mode is not None and resolved_overrides.mode != "preset":
            raise ValueError("--preset requires --mode preset or no --mode")
        if resolved_overrides.mode is None:
            mode_raw = "preset"
            override_sources["mode"] = "cli-derived"
    mode = _validate_choice(str(mode_raw), field="mode", choices={"fixture", "preset"})
    preset = str(preset_raw) if preset_raw is not None else None
    if mode == "preset" and not preset:
        raise ValueError("preset mode requires a preset id")
    if mode == "fixture" and preset:
        raise ValueError("fixture mode cannot be combined with preset")

    profile = _validate_choice(
        str(
            _choose_value(
                "profile",
                environment_value=entry.get("profile"),
                override_value=resolved_overrides.profile,
                default_value="full",
                override_sources=override_sources,
            )
        ),
        field="profile",
        choices={"baseline-min", "full"},
    )
    approval_mode = _validate_choice(
        str(
            _choose_value(
                "approval_mode",
                environment_value=entry.get("approval_mode"),
                override_value=resolved_overrides.approval_mode,
                default_value="auto_allow",
                override_sources=override_sources,
            )
        ),
        field="approval_mode",
        choices={"auto_allow", "interactive", "case"},
    )
    runner_max_turns = _validate_positive_int(
        _choose_value(
            "runner.max_turns",
            environment_value=runner_entry.get("max_turns"),
            override_value=resolved_overrides.max_turns,
            default_value=_DEFAULT_MAX_TURNS,
            override_sources=override_sources,
        ),
        field="runner.max_turns",
    )
    output_dir = _resolve_repo_path(
        str(
            _choose_value(
                "artifacts.output_dir",
                environment_value=artifacts_entry.get("output_dir"),
                override_value=resolved_overrides.output_dir,
                default_value=str(suite / "runs"),
                override_sources=override_sources,
            )
        )
    )
    config_path = _resolve_repo_path(
        str(
            _choose_value(
                "config",
                environment_value=entry.get("config"),
                override_value=resolved_overrides.config,
                default_value="config/setting.yaml",
                override_sources=override_sources,
            )
        )
    )
    if not config_path.exists():
        raise ValueError(f"config file not found: {config_path}")
    config = load_config(config_path)
    instructions_mode, session_backend, compactor_mode = _profile_modes(profile)
    return ResolvedEvalEnvironment(
        environment_id=environment_id or "cli-args",
        environment_config_path=environment_config_path if environment_id else None,
        environment_config_hash=environment_hash,
        kongming_config_path=config_path,
        kongming_config_hash=_sha256_file(config_path),
        suite=suite,
        mode=mode,
        preset=preset,
        profile=profile,
        approval_mode=approval_mode,
        instructions_mode=instructions_mode,
        session_backend=session_backend,
        compactor_mode=compactor_mode,
        runner_max_turns=runner_max_turns,
        output_dir=output_dir,
        api_keys_present=_api_keys_present(config, preset),
        override_sources=override_sources,
    )


def _find_preset(config: Config, preset_id: str):
    """按 id 查找 preset，输入 Config 和 preset id，输出 LLMPresetConfig。"""

    for preset in config.web.llm_presets:
        if preset.id == preset_id:
            return preset
    available = ", ".join(preset.id for preset in config.web.llm_presets) or "<none>"
    raise ValueError(f"unknown preset {preset_id!r}; available presets: {available}")


def _effective_task_approval_mode(environment: ResolvedEvalEnvironment, task: Task) -> str:
    """解析单题有效审批模式，输入环境和任务，输出 auto_allow 或 interactive。"""

    if environment.approval_mode != "case":
        return environment.approval_mode
    task_mode = task.runtime.get("approval_mode", "auto_allow")
    if task_mode not in {"auto_allow", "interactive"}:
        raise ValueError("task runtime.approval_mode must be auto_allow or interactive")
    return str(task_mode)


async def _prompt_eval_approval(request: ApprovalRequest) -> ApprovalAction:
    """评测脚本交互审批 prompt，输入审批请求，输出 ApprovalAction。"""

    if not sys.stdin.isatty():
        raise RuntimeError("interactive approval requires a TTY")
    answer = await asyncio.to_thread(
        input,
        f"Approve tool {request.tool_name}? [y/N] ",
    )
    if answer.strip().lower() in {"y", "yes"}:
        return ApprovalAction.ACCEPT_ONCE
    return ApprovalAction.REJECT


def _approval_provider_for(mode: str):
    """按有效审批模式构造底层 provider，输入模式，输出 ApprovalProvider。"""

    if mode == "auto_allow":
        return AutoAllowApproval()
    if mode == "interactive":
        if not sys.stdin.isatty():
            raise RuntimeError("interactive approval requires a TTY")
        return InteractiveApproval(_prompt_eval_approval)
    raise ValueError(f"unsupported effective approval mode: {mode}")


def _fixture_semantics(environment: ResolvedEvalEnvironment) -> dict[str, Any] | None:
    """描述 fixture 模式验证边界，输入环境，输出 summary 元数据。"""

    if environment.mode != "fixture":
        return None
    return {
        "uses_real_runner": True,
        "uses_real_llm_provider": False,
        "tool_execution_checks_tool_loop": True,
        "non_tool_tasks_check": [
            "NativeRuntime.run request/response path",
            "session persistence",
            "deterministic scorer behavior",
        ],
    }


def load_runtime_config(
    environment: ResolvedEvalEnvironment,
    run_dir: Path,
    *,
    effective_approval_mode: str,
) -> Config:
    """加载并隔离 runtime config，输入 resolved environment 和 run 目录，输出 Config。"""

    config = load_config(environment.kongming_config_path)
    if environment.preset:
        config = apply_preset(config, _find_preset(config, environment.preset))

    config = config.model_copy(
        update={
            "approval": config.approval.model_copy(update={"mode": effective_approval_mode}),
            "runner": config.runner.model_copy(update={"max_turns": environment.runner_max_turns}),
            "session": config.session.model_copy(
                update={
                    "backend": environment.session_backend,
                    "store_path": str(run_dir / "sessions.sqlite"),
                    "file_store_path": str(run_dir / "sessions"),
                }
            ),
            "trace": config.trace.model_copy(update={"output_path": str(run_dir / "trace.jsonl")}),
        }
    )
    return config


def build_session_factory(config: Config, instructions: str):
    """构造 session factory，输入 Config 和指令文本，输出 session factory。"""

    bootstrap = SessionBootstrap(
        agent_name="harness-runtime-eval",
        model_name=config.model.name,
        instruction_sources=["harness-runtime-eval"],
        instruction_text_hash="sha256:" + hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
        instruction_text=instructions,
        created_at=time.time(),
        cwd=str(_REPO_ROOT),
        app_version="harness-runtime-eval-v0.1",
    )

    def _factory(session_id: str):
        """构造单个 file session，输入 session_id，输出 Session 实现。"""

        return build_session(config, session_id, bootstrap=bootstrap)

    return _factory


@contextlib.contextmanager
def isolated_home(home: Path) -> Iterator[None]:
    """临时设置 KONGMING_HOME，输入 home 路径，退出时恢复环境。"""

    old_value = os.environ.get("KONGMING_HOME")
    os.environ["KONGMING_HOME"] = str(home)
    home.mkdir(parents=True, exist_ok=True)
    try:
        yield
    finally:
        if old_value is None:
            os.environ.pop("KONGMING_HOME", None)
        else:
            os.environ["KONGMING_HOME"] = old_value


# ---------------------------------------------------------------------------
# 单题 / 整套执行
# ---------------------------------------------------------------------------


async def run_task(
    task: Task,
    environment: ResolvedEvalEnvironment,
    run_id: str,
    run_dir: Path,
) -> RuntimeTaskResult:
    """执行单题 runtime eval，输入 Task、resolved environment 和 run 目录，输出结果。"""

    started = time.monotonic()
    sink = RecordingEventSink()
    final_content = ""
    error: str | None = None
    result_status = "failed"
    turn_count = 0
    effective_approval_mode = _effective_task_approval_mode(environment, task)
    environment_metadata = environment.as_metadata()
    metadata: dict[str, Any] = {
        **environment_metadata,
        "mode": environment.mode,
        "effective_approval_mode": effective_approval_mode,
        "task_runtime": task.runtime,
    }
    task_home = run_dir / "tasks" / task.id / "kongming_home"
    runtime: NativeRuntime | None = None
    try:
        with isolated_home(task_home):
            config = load_runtime_config(
                environment,
                run_dir,
                effective_approval_mode=effective_approval_mode,
            )
            registry = (
                build_eval_tools()
                if task.scoring.get("type") == "tool_execution"
                else ToolRegistry()
            )
            instructions = "" if environment.profile == "baseline-min" else _EVAL_INSTRUCTIONS
            agent_spec = None
            if environment.profile == "baseline-min":
                agent_spec = AgentSpec(
                    name="harness-baseline-min",
                    instructions="",
                    default_model=config.model.name,
                    tool_names=tuple(registry.names()),
                    max_turns=environment.runner_max_turns,
                    reasoning_effort=config.model.reasoning_effort,
                    metadata={"profile": "baseline-min"},
                )
            llm_provider = (
                FixtureRuntimeLLM(task)
                if environment.mode == "fixture" and not environment.preset
                else None
            )
            runtime = NativeRuntime.build(
                config,
                event_sinks=[sink],
                tools=registry,
                enabled_tool_names=registry.names(),
                approval=_approval_provider_for(effective_approval_mode),
                agent_spec=agent_spec,
                instructions=instructions,
                session_factory=build_session_factory(config, instructions),
                message_compactor=(
                    EvalNoopCompactor() if environment.compactor_mode == "noop-script" else None
                ),
                llm_provider=llm_provider,
            )
            result = await runtime.run(task.prompt, session_id=f"{run_id}-{task.id}")
            result_status = result.status
            turn_count = result.turn_count
            metadata.update(result.metadata)
            if result.final_message and result.final_message.content:
                final_content = result.final_message.content
            if result.status != "completed":
                error = str(result.error) if result.error else result.status
    except Exception as exc:
        error = str(exc)
    finally:
        if runtime is not None:
            with contextlib.suppress(Exception):
                await runtime.aclose()

    task_run_dir = run_dir / "tasks" / task.id
    if task.scoring.get("type") == "tool_execution":
        score = score_tool_execution(task, final_content, sink.events)
    else:
        score = score_response(task, final_content, task_run_dir)
    if error:
        score = ScoreResult(False, 0.0, {**score.details, "error": error})

    return RuntimeTaskResult(
        final_content=final_content,
        events=sink.events,
        score=score,
        duration_ms=int((time.monotonic() - started) * 1000),
        error=error,
        result_status=result_status,
        turn_count=turn_count,
        metadata=metadata,
    )


async def run_resolved_environment(
    environment: ResolvedEvalEnvironment,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """异步执行已解析环境，输入 environment 和可选 run id，输出 summary。"""

    suite_dir = environment.suite
    tasks = load_tasks(suite_dir)
    resolved_run_id = _validate_run_id(_utc_run_id() if run_id is None else run_id)
    output_root = environment.output_dir
    run_dir = output_root / resolved_run_id
    if run_dir.exists():
        shutil.rmtree(run_dir)
    task_records: list[dict[str, Any]] = []
    category_stats: dict[str, dict[str, float]] = defaultdict(
        lambda: {"total": 0.0, "passed": 0.0, "score": 0.0}
    )
    environment_metadata = environment.as_metadata()

    for task in tasks:
        task_run_dir = run_dir / "tasks" / task.id
        task_run_dir.mkdir(parents=True, exist_ok=True)
        task_result = await run_task(task, environment, resolved_run_id, run_dir)
        record = {
            "id": task.id,
            "category": task.category,
            "source": task.source,
            "passed": task_result.score.passed,
            "score": task_result.score.score,
            "duration_ms": task_result.duration_ms,
            "error": task_result.error,
            "details": task_result.score.details,
            "metadata": task_result.metadata,
        }
        _write_json(
            task_run_dir / "trajectory.json",
            {
                "task": {
                    "id": task.id,
                    "category": task.category,
                    "source": task.source,
                    "path": str(task.path),
                    "prompt": task.prompt,
                    "scoring": task.scoring,
                },
                "runtime": {
                    "status": task_result.result_status,
                    "turn_count": task_result.turn_count,
                    "metadata": task_result.metadata,
                },
                "response": {"content": task_result.final_content},
                "events": task_result.events,
                "score": record,
            },
        )
        task_records.append(record)
        stats = category_stats[task.category]
        stats["total"] += 1
        stats["passed"] += 1 if task_result.score.passed else 0
        stats["score"] += task_result.score.score

    categories = {
        category: {
            "total": int(stats["total"]),
            "passed": int(stats["passed"]),
            "score": stats["score"] / stats["total"] if stats["total"] else 0.0,
        }
        for category, stats in category_stats.items()
    }
    total = len(task_records)
    passed = sum(1 for record in task_records if record["passed"])
    score = sum(float(record["score"]) for record in task_records) / total if total else 0.0
    summary = {
        "run_id": resolved_run_id,
        "suite": str(suite_dir),
        "mode": environment.mode,
        "model": environment.preset or environment.mode,
        "environment_id": environment.environment_id,
        "profile": environment.profile,
        "approval_mode": environment.approval_mode,
        "session_backend": environment.session_backend,
        "compactor_mode": environment.compactor_mode,
        "runner_max_turns": environment.runner_max_turns,
        "environment": environment_metadata,
        "total": total,
        "passed": passed,
        "score": score,
        "categories": categories,
        "run_dir": str(run_dir),
    }
    fixture_semantics = _fixture_semantics(environment)
    if fixture_semantics is not None:
        summary["fixture_semantics"] = fixture_semantics
    _write_json(run_dir / "summary.json", summary)
    _write_json(run_dir / "tasks.json", {"tasks": task_records})
    (run_dir / "report.md").write_text(render_report(summary, task_records), encoding="utf-8")
    return summary


async def run_suite_async(args: argparse.Namespace) -> dict[str, Any]:
    """异步执行整套 runtime eval，输入 CLI 参数，输出 summary。"""

    environment = resolve_eval_environment(args.environment, _overrides_from_args(args))
    return await run_resolved_environment(environment, run_id=args.run_id)


async def run_harness_environment(
    environment_id: str,
    overrides: EvalEnvironmentOverrides | None = None,
) -> dict[str, Any]:
    """Python API 入口，输入 environment id 和覆盖项，输出 suite summary。"""

    environment = resolve_eval_environment(environment_id, overrides)
    return await run_resolved_environment(
        environment,
        run_id=overrides.run_id if overrides else None,
    )


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """写 JSON 文件，输入路径和 payload，无返回值。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


_CATEGORY_LABELS = {
    "coding": "代码生成",
    "instruction_following": "指令遵循",
    "long_context": "长上下文定位",
    "repo_fix": "仓库修复",
    "short_answer": "短答案推理",
    "tool_execution": "工具执行",
}


def _category_label(category: str) -> str:
    """把 category 翻译为中文显示名，输入英文类别，输出中文标签。"""

    return _CATEGORY_LABELS.get(category, category)


def _failure_summary(record: dict[str, Any]) -> str:
    """根据 scorer 细节生成人类可读失败摘要，输入任务记录，输出中文说明。"""

    details = record.get("details", {})
    if isinstance(details, dict) and record.get("category") == "short_answer":
        return f"期望 `{details.get('expected')}`，实际输出 `{details.get('actual')}`。"
    if isinstance(details, dict) and record.get("category") == "tool_execution":
        failures = details.get("failures", [])
        failure_text = (
            "；".join(str(item) for item in failures) if failures else "工具执行链路不符合预期"
        )
        calls = details.get("tool_calls", [])
        results = details.get("tool_results", [])
        return (
            f"{failure_text}。实际 tool_calls: `{json.dumps(calls, ensure_ascii=False)}`；"
            f"tool_results: `{json.dumps(results, ensure_ascii=False)}`。"
        )
    if isinstance(details, dict) and details.get("error"):
        return str(details["error"])
    return str(record.get("error") or "未通过 scorer。")


def _analysis_lines(summary: dict[str, Any], task_records: list[dict[str, Any]]) -> list[str]:
    """生成报告分析段，输入 summary 和任务记录，输出 Markdown 行。"""

    passed_records = [record for record in task_records if record["passed"]]
    failed_records = [record for record in task_records if not record["passed"]]
    strong_categories = [
        _category_label(category)
        for category, item in sorted(summary["categories"].items())
        if item["passed"] == item["total"]
    ]
    weak_categories = [
        _category_label(category)
        for category, item in sorted(summary["categories"].items())
        if item["passed"] < item["total"]
    ]
    lines = [
        "## 能力分析",
        "",
        f"- 本轮通过 `{summary['passed']} / {summary['total']}`，总分 `{summary['score']:.2f}`。",
    ]
    if strong_categories:
        lines.append(f"- 表现稳定的能力面：{'、'.join(strong_categories)}。")
    if weak_categories:
        lines.append(f"- 暴露短板的能力面：{'、'.join(weak_categories)}。")
    if passed_records and "tool_execution" in summary["categories"]:
        lines.append(
            "- 当前样本显示该模型能完成真实 tool_call 生成、工具执行结果读取和最终答案整合。"
        )
    if failed_records:
        lines.append("- 失败样例：")
        for record in failed_records:
            lines.append(
                f"  - `{record['id']}`（{_category_label(record['category'])}）："
                f"{_failure_summary(record)}"
            )
    return lines


def render_report(summary: dict[str, Any], task_records: list[dict[str, Any]]) -> str:
    """生成中文 Markdown 报告，输入汇总和题目记录，输出 Markdown 文本。"""

    environment = summary.get("environment") if isinstance(summary.get("environment"), dict) else {}
    fixture_semantics = summary.get("fixture_semantics")
    fixture_line = ""
    if isinstance(fixture_semantics, dict):
        fixture_line = (
            "- Fixture 验证边界：`真实 Runner + 确定性伪 LLM；tool_execution 覆盖工具闭环，"
            "非工具题覆盖 runtime 请求、session 落盘和 scorer`"
        )
    category_rows = []
    for category, item in sorted(summary["categories"].items()):
        category_rows.append(
            f"| {_category_label(category)} | `{category}` | {item['passed']} / {item['total']} | {item['score']:.2f} |"
        )
    task_rows = []
    for record in task_records:
        status = "通过" if record["passed"] else "失败"
        task_rows.append(
            f"| `{record['id']}` | {_category_label(record['category'])} | {status} | {record['score']:.2f} |"
        )
    return "\n".join(
        [
            "# Harness Eval 评测报告",
            "",
            "## 运行信息",
            "",
            f"- 运行 ID：`{summary['run_id']}`",
            f"- 题集路径：`{summary['suite']}`",
            f"- 环境预设：`{summary.get('environment_id') or ''}`",
            f"- 运行模式：`{summary['mode']}`",
            f"- 模型 / preset：`{summary.get('model') or ''}`",
            f"- Runtime profile：`{summary.get('profile') or ''}`",
            f"- Approval mode：`{summary.get('approval_mode') or ''}`",
            f"- Session backend：`{summary.get('session_backend') or ''}`",
            f"- Compactor mode：`{summary.get('compactor_mode') or ''}`",
            f"- Runner max turns：`{summary.get('runner_max_turns') or ''}`",
            f"- Environment config path：`{environment.get('environment_config_path') or ''}`",
            f"- Environment config hash：`{environment.get('environment_config_hash') or ''}`",
            f"- Kongming config path：`{environment.get('kongming_config_path') or ''}`",
            f"- Kongming config hash：`{environment.get('kongming_config_hash') or ''}`",
            f"- Output dir：`{environment.get('output_dir') or ''}`",
            f"- API keys present：`{json.dumps(environment.get('api_keys_present') or {}, ensure_ascii=False)}`",
            f"- Override sources：`{json.dumps(environment.get('override_sources') or {}, ensure_ascii=False)}`",
            fixture_line,
            f"- 通过数：`{summary['passed']} / {summary['total']}`",
            f"- 总分：`{summary['score']:.2f}`",
            "",
            *_analysis_lines(summary, task_records),
            "",
            "## 分类得分",
            "",
            "| 能力面 | category | 通过数 | 分数 |",
            "|---|---|---:|---:|",
            *category_rows,
            "",
            "## 任务明细",
            "",
            "| 任务 | 能力面 | 状态 | 分数 |",
            "|---|---|---|---:|",
            *task_rows,
            "",
        ]
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """构造 CLI parser，输入为空，输出 ArgumentParser。"""

    parser = argparse.ArgumentParser(
        description="Run Kongming harness eval suite via NativeRuntime + Runner"
    )
    parser.add_argument(
        "--environment",
        help="Eval environment preset id from evals/harness-runtime-v0.1/environments.yaml",
    )
    parser.add_argument(
        "--environment-config",
        help="Environment preset YAML path; default evals/harness-runtime-v0.1/environments.yaml",
    )
    parser.add_argument("--suite", help="Migration override for suite path")
    parser.add_argument(
        "--mode",
        choices=("fixture",),
        default=None,
        help="无 --preset 时的运行模式；fixture 走内置伪 LLM 验证 harness 闭环",
    )
    parser.add_argument("--preset", "--llm", dest="preset", help="Kongming web.llm_presets id")
    parser.add_argument("--config", help="Kongming config path; default config/setting.yaml")
    parser.add_argument("--output-dir")
    parser.add_argument("--run-id")
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--profile", choices=("baseline-min", "full"))
    parser.add_argument("--approval-mode", choices=("auto_allow", "interactive", "case"))
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口，输入 argv，输出进程退出码。"""

    args = build_parser().parse_args(argv)
    summary = asyncio.run(run_suite_async(args))
    print(f"run_dir: {summary['run_dir']}")
    print(f"passed: {summary['passed']} / {summary['total']}")
    print(f"score: {summary['score']:.2f}")
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
