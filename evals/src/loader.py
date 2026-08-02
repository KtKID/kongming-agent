"""Harness Eval 题目加载与校验。

# 从 suite 目录加载 YAML 题目文件，校验 schema 后输出 Task 列表。
# 关键函数：load_tasks、_validate_task_payload。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from .models import Task


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
    initial_state = payload.get("initial_state", {})
    if initial_state is None:
        initial_state = {}
    if not isinstance(initial_state, dict):
        raise ValueError(f"{path}: initial_state must be an object when present")
    fixture_calls = payload.get("fixture_calls", [])
    if fixture_calls is None:
        fixture_calls = []
    if not isinstance(fixture_calls, list):
        raise ValueError(f"{path}: fixture_calls must be a list when present")
    for entry in fixture_calls:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ValueError(f"{path}: each fixture_calls entry needs a string 'name'")
    return Task(
        id=task_id,
        category=category,
        source=source,
        prompt=prompt,
        scoring=scoring,
        fixture_response=fixture_response,
        runtime=dict(runtime),
        path=path,
        initial_state=copy.deepcopy(initial_state),
        fixture_calls=[dict(entry) for entry in fixture_calls],
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
