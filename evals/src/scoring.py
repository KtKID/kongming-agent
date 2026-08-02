"""Harness Eval 评分函数。

# 按 scoring type 分发打分：exact_text / json / python_code / swebench_diff /
# tool_execution / tool_state。以及 pass^k 可靠性指标计算。
# 关键函数：score_response、score_tool_execution、score_tool_state、pass_hat_k。
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path
from typing import Any

from .extractors import extract_diff, extract_json, extract_python_code, strip_code_fence
from .fake_tools import StatefulToolStore
from .models import ScoreResult, Task
from .sandbox import (
    apply_model_diff,
    init_repo_with_base,
    run_pytest,
    run_pytest_nodes,
    write_file,
)

# ---------------------------------------------------------------------------
# 基础评分
# ---------------------------------------------------------------------------


def _score_exact_text(task: Task, response: str) -> ScoreResult:
    """执行短答案精确匹配，输入任务和响应，输出评分。"""

    expected = str(task.scoring.get("expected", ""))
    actual = strip_code_fence(response).strip()
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
        actual = extract_json(response)
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
    write_file(sandbox_dir, solution_file, extract_python_code(response))
    for test_spec in task.scoring.get("tests", []):
        write_file(sandbox_dir, str(test_spec["path"]), str(test_spec["content"]))
    result = run_pytest(sandbox_dir, int(task.scoring.get("timeout_seconds", 15)))
    passed = result["exit_code"] == 0
    return ScoreResult(passed, 1.0 if passed else 0.0, {"pytest": result})


def _score_swebench_diff(task: Task, response: str, sandbox_dir: Path) -> ScoreResult:
    """执行 SWE-bench 风格 diff 评分，输入任务/模型响应/sandbox，输出评分结果。"""

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
    init_repo_with_base(repo_dir, base_files)
    for relative_path, content in test_files.items():
        write_file(repo_dir, str(relative_path), str(content))

    baseline_fail = run_pytest_nodes(repo_dir, fail_to_pass, timeout_seconds)
    baseline_pass = run_pytest_nodes(repo_dir, pass_to_pass, timeout_seconds)
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

    diff_text = extract_diff(response)
    apply_result = apply_model_diff(repo_dir, diff_text)
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

    post_fail = run_pytest_nodes(repo_dir, fail_to_pass, timeout_seconds)
    post_pass = run_pytest_nodes(repo_dir, pass_to_pass, timeout_seconds)
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


# ---------------------------------------------------------------------------
# 工具调用 / 状态化评分
# ---------------------------------------------------------------------------


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


def _dotted_get(state: dict[str, Any], dotted_path: str) -> Any:
    """按点路径读取嵌套 state，输入 state 和 'orders.ORD-1002' 形态路径，输出叶子值或 None。"""

    cursor: Any = state
    for segment in dotted_path.split("."):
        if not isinstance(cursor, dict) or segment not in cursor:
            return None
        cursor = cursor[segment]
    return cursor


def _canonical_state_json(state: dict[str, Any]) -> str:
    """把世界状态规范化为可 hash 的 JSON 字符串，输入 state dict，输出 canonical JSON。"""

    return json.dumps(state, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _state_sha256(state: dict[str, Any]) -> str:
    """对世界状态求 SHA-256，输入 state dict，输出小写 hex digest。"""

    return hashlib.sha256(_canonical_state_json(state).encode("utf-8")).hexdigest()


def score_tool_state(
    task: Task,
    final_content: str,
    events: list[dict[str, Any]],
    store: StatefulToolStore,
) -> ScoreResult:
    """τ-bench 风格状态化裁决，输入任务、最终回答、事件和 per-task store，输出评分。"""

    scoring = task.scoring
    failures: list[str] = []
    final_state = store.snapshot()
    initial_state = store.initial_snapshot()
    actual_state_hash = _state_sha256(final_state)
    initial_state_hash = _state_sha256(initial_state)
    calls = _tool_calls_from_events(events)
    tool_call_count = len(calls)

    expected_state_hash = scoring.get("expected_state_hash")
    if expected_state_hash is None:
        failures.append(
            "expected_state_hash missing: tau_tool_state tasks must declare scoring.expected_state_hash"
        )
    elif not isinstance(expected_state_hash, str):
        failures.append("expected_state_hash must be a lower-case hex string")
    else:
        expected_lower = expected_state_hash.strip().lower()
        if expected_lower != initial_state_hash and tool_call_count == 0:
            failures.append(
                "state was expected to change but no tool was called; "
                "likely hallucinated tool result"
            )
        if expected_lower != actual_state_hash:
            failures.append(
                f"state hash mismatch: expected={expected_lower} actual={actual_state_hash}"
            )

    for dotted_path in scoring.get("state_unchanged", []):
        if _dotted_get(final_state, dotted_path) != _dotted_get(initial_state, dotted_path):
            failures.append(f"state changed at protected path {dotted_path}")

    for forbidden in scoring.get("forbidden_calls", []):
        forbidden_name = forbidden.get("name")
        forbidden_args = forbidden.get("arguments_contains", {})
        for call in calls:
            if call.get("name") != forbidden_name:
                continue
            arguments = call.get("arguments", {})
            if isinstance(arguments, dict) and _arguments_contain(arguments, forbidden_args):
                failures.append(f"forbidden call invoked: {forbidden_name} {forbidden_args}")
                break

    lowered_final = final_content.lower()
    for expected_text in scoring.get("final_contains", []):
        if str(expected_text).lower() not in lowered_final:
            failures.append(f"final missing {expected_text!r}")

    passed = not failures
    return ScoreResult(
        passed=passed,
        score=1.0 if passed else 0.0,
        details={
            "tool_calls": calls,
            "tool_call_count": tool_call_count,
            "final_state": final_state,
            "initial_state": initial_state,
            "final": final_content,
            "actual_state_hash": actual_state_hash,
            "initial_state_hash": initial_state_hash,
            "expected_state_hash": expected_state_hash,
            "expected_state_debug": scoring.get("expected_state"),
            "failures": failures,
        },
    )


# ---------------------------------------------------------------------------
# pass^k 可靠性指标（τ-bench 公式）
# ---------------------------------------------------------------------------


def pass_hat_k(n: int, c: int, k: int) -> float:
    """τ-bench 单 task pass^k，输入总跑次数 n、成功次数 c 和抽样 k，输出 pass^k 值。"""

    if k > n or n == 0:
        return 0.0
    denominator = math.comb(n, k)
    if denominator == 0:
        return 0.0
    return math.comb(c, k) / denominator


def aggregate_pass_hat_k(per_task: list[tuple[int, int]], ks: list[int]) -> dict[int, float]:
    """跨 task 聚合 pass^k（算术平均），输入每 task 的 (n, c) 和 k 列表，输出 {k: 平均值}。"""

    if not per_task:
        return {k: 0.0 for k in ks}
    return {k: sum(pass_hat_k(n, c, k) for n, c in per_task) / len(per_task) for k in ks}
