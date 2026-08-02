#!/usr/bin/env python3
"""一次性生成并回填 tau_tool_state 题目的 ``scoring.expected_state_hash``。

本脚本的作用：
1. 扫描 ``evals/harness-runtime-v0.1/tasks/*.yaml``，筛出 ``scoring.type == tool_state`` 的题；
2. 用 PyYAML 加载 ``initial_state`` + ``fixture_calls``，按 mini_retail 工具语义在
   ``StatefulToolStore`` 上推演一遍，得到"基线 fixture 跑完的最终态"；
3. 把最终态规范化为 canonical JSON（``sort_keys=True`` / ``ensure_ascii=False`` /
   固定 ``separators``），做 SHA-256，与 scorer ``_state_sha256`` 完全一致；
4. 用 ruamel.yaml round-trip 写回 ``scoring.expected_state_hash``，保留原文件的注释和顺序。

关键流程：
- ``main``：解析参数、遍历 task 文件、决定是否 ``--dry-run``。
- ``compute_task_hash``：按 task 推演 store + 计算 hash，输出最终态和 hash。
- ``apply_fixture_calls``：按 fixture_calls 把 cancel_order/process_return 映射成 store 写操作。
- ``upsert_expected_state_hash``：ruamel round-trip 写回 yaml。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER_PATH = REPO_ROOT / "scripts" / "run_kongming_harness_eval.py"
TASKS_DIR = REPO_ROOT / "evals" / "harness-runtime-v0.1" / "tasks"


def _load_runner_module() -> Any:
    """加载 harness eval 主脚本作为模块，复用 StatefulToolStore，避免重复实现状态语义。"""

    spec = importlib.util.spec_from_file_location("run_kongming_harness_eval", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_kongming_harness_eval"] = module
    spec.loader.exec_module(module)
    return module


def _canonical_state_json(state: dict[str, Any]) -> str:
    """与 scorer 同步的 canonical JSON：sort_keys / ensure_ascii=False / 固定 separators。"""

    return json.dumps(state, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _state_sha256(state: dict[str, Any]) -> str:
    """对世界状态求 SHA-256，输入 state dict，输出小写 hex digest。"""

    return hashlib.sha256(_canonical_state_json(state).encode("utf-8")).hexdigest()


def apply_fixture_calls(store: Any, fixture_calls: list[dict[str, Any]]) -> None:
    """按 fixture_calls 在 store 上推演 mini_retail 写操作，输入 store + 调用序列，无返回值。

    支持的 tool name 与生产工具 EvalCancelOrderTool / EvalProcessReturnTool / EvalGetOrderTool 一致：
      - cancel_order   → store.set_order_status(order_id, "cancelled")
      - process_return → store.set_order_status(order_id, "returned")
      - get_order      → 只读，无副作用
    """

    for call in fixture_calls:
        name = call.get("name")
        args = call.get("arguments", {}) or {}
        order_id = str(args.get("order_id", ""))
        if name == "cancel_order":
            store.set_order_status(order_id, "cancelled")
        elif name == "process_return":
            store.set_order_status(order_id, "returned")
        elif name == "get_order":
            continue
        else:
            raise ValueError(f"unsupported fixture_call name: {name!r}")


def compute_task_hash(payload: dict[str, Any], runner: Any) -> tuple[str, dict[str, Any]]:
    """按 task payload 推演最终态 + 计算 hash，输入 yaml dict + runner 模块，输出 (hash, 最终态)。"""

    initial_state = payload.get("initial_state") or {}
    if not isinstance(initial_state, dict):
        raise ValueError("initial_state must be an object")
    store = runner.StatefulToolStore(initial_state)
    fixture_calls = payload.get("fixture_calls") or []
    if not isinstance(fixture_calls, list):
        raise ValueError("fixture_calls must be a list")
    apply_fixture_calls(store, fixture_calls)
    final_state = store.snapshot()
    return _state_sha256(final_state), final_state


def upsert_expected_state_hash(yaml_path: Path, new_hash: str) -> bool:
    """把 expected_state_hash 写回 yaml，保留注释和顺序，输入路径 + 新 hash，输出是否实际改动。"""

    yml = YAML(typ="rt")
    yml.preserve_quotes = True
    yml.width = 4096
    with yaml_path.open("r", encoding="utf-8") as fh:
        data = yml.load(fh)
    if data is None or "scoring" not in data:
        raise ValueError(f"{yaml_path}: missing top-level scoring block")
    scoring = data["scoring"]
    if scoring.get("type") != "tool_state":
        raise ValueError(f"{yaml_path}: scoring.type is not tool_state")
    if scoring.get("expected_state_hash") == new_hash:
        return False
    # 期望把 expected_state_hash 紧跟 type 之后，便于人眼对照；若 ruamel 不支持精确插入位置，
    # 直接 assign 会落到尾部，但不影响语义。这里优先 assign 即可。
    scoring["expected_state_hash"] = new_hash
    with yaml_path.open("w", encoding="utf-8") as fh:
        yml.dump(data, fh)
    return True


def main() -> int:
    """脚本入口，输入 sys.argv，输出退出码。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tasks-dir",
        default=str(TASKS_DIR),
        help=f"tau_*.yaml 所在目录（默认 {TASKS_DIR}）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印 hash，不写回 yaml",
    )
    args = parser.parse_args()

    runner = _load_runner_module()
    tasks_dir = Path(args.tasks_dir)
    yaml_files = sorted(tasks_dir.glob("tau_*.yaml"))
    if not yaml_files:
        print(f"no tau_*.yaml found under {tasks_dir}", file=sys.stderr)
        return 1

    changed = 0
    for yaml_path in yaml_files:
        payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            print(f"{yaml_path.name}: skip (not a mapping)")
            continue
        scoring = payload.get("scoring") or {}
        if scoring.get("type") != "tool_state":
            print(f"{yaml_path.name}: skip (scoring.type={scoring.get('type')!r})")
            continue
        new_hash, final_state = compute_task_hash(payload, runner)
        existing = scoring.get("expected_state_hash")
        print(f"{yaml_path.name}: hash={new_hash}")
        print(f"  final_state={_canonical_state_json(final_state)}")
        if existing and existing != new_hash:
            print(f"  WARNING: existing hash {existing} mismatches new {new_hash}")
        if args.dry_run:
            continue
        if upsert_expected_state_hash(yaml_path, new_hash):
            changed += 1
            print("  -> written")
        else:
            print("  -> unchanged")
    print(f"done. files written: {changed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
