"""τ-bench 风格状态化裁决（S0）单元测试。

覆盖 ``run_kongming_harness_eval`` 新增的 StatefulToolStore、mini_retail 工具、
``score_tool_state`` scorer、``_fixture_state_calls`` 构造器，以及 T1/T2 两道真实
task yaml 在 fixture 模式下经真实 Runner 跑通的端到端闭环。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "scripts" / "run_kongming_harness_eval.py"
SUITE_TASKS = REPO_ROOT / "evals" / "harness-runtime-v0.1" / "tasks"


def _load_runner_module():
    """加载 runtime eval 脚本模块，返回可直接调用的 module。"""

    spec = importlib.util.spec_from_file_location("run_kongming_harness_eval", RUNNER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_kongming_harness_eval"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _initial_orders() -> dict:
    """返回 mini_retail 初始订单状态，供 store / scorer 测试播种。"""

    return {
        "orders": {
            "ORD-1001": {"status": "pending", "item": "A1", "payment": "card", "total": 50},
            "ORD-1002": {"status": "shipped", "item": "B2", "payment": "card", "total": 80},
            "ORD-1003": {"status": "delivered", "item": "C3", "payment": "gift", "total": 30},
        }
    }


def _make_tau_task(runner, **overrides):
    """构造一道 tool_state Task，输入 scoring/state 覆盖，输出 Task 实例。"""

    base = {
        "id": "tau-unit",
        "category": "tau_tool_state",
        "source": "unit",
        "prompt": "handle order",
        "scoring": {"type": "tool_state"},
        "fixture_response": "done",
        "runtime": {},
        "path": Path("task.yaml"),
        "initial_state": _initial_orders(),
        "fixture_calls": [],
    }
    base.update(overrides)
    return runner.Task(**base)


@pytest.mark.unit
def test_store_seeds_and_mutates_without_touching_initial() -> None:
    """store 写操作只改当前态，initial_snapshot 必须保持播种初值。"""

    runner = _load_runner_module()
    store = runner.StatefulToolStore(_initial_orders())

    assert store.get_order("ORD-1001")["status"] == "pending"
    store.set_order_status("ORD-1001", "cancelled")

    assert store.snapshot()["orders"]["ORD-1001"]["status"] == "cancelled"
    assert store.initial_snapshot()["orders"]["ORD-1001"]["status"] == "pending"
    assert store.get_order("ORD-1002")["status"] == "shipped"


@pytest.mark.unit
def test_store_rejects_unknown_order() -> None:
    """对未知订单写状态必须抛错，避免静默吞掉错误调用。"""

    runner = _load_runner_module()
    store = runner.StatefulToolStore(_initial_orders())

    with pytest.raises(ValueError, match="unknown order_id"):
        store.set_order_status("ORD-9999", "cancelled")


@pytest.mark.unit
def test_dotted_get_walks_nested_state() -> None:
    """点路径读取必须支持嵌套 key，缺失返回 None。"""

    runner = _load_runner_module()
    state = _initial_orders()

    assert runner._dotted_get(state, "orders.ORD-1002.status") == "shipped"
    assert runner._dotted_get(state, "orders.ORD-404") is None


@pytest.mark.unit
def test_score_tool_state_passes_on_matching_hash_and_unchanged() -> None:
    """全库 hash 命中且保护路径未变时必须 pass，expected_state 仅作 debug hint 不参与裁决。

    注：测试通过直接戳 store 模拟 cancel 副作用，所以也要伪造一个对应的 tool_call event，
    否则会被"过程否决（hallucinated tool result）"判据误杀。
    """

    runner = _load_runner_module()
    store = runner.StatefulToolStore(_initial_orders())
    store.set_order_status("ORD-1001", "cancelled")
    expected_hash = runner._state_sha256(store.snapshot())
    task = _make_tau_task(
        runner,
        scoring={
            "type": "tool_state",
            "expected_state_hash": expected_hash,
            "expected_state": {"orders": {"ORD-1001": {"status": "cancelled"}}},
            "state_unchanged": ["orders.ORD-1002", "orders.ORD-1003"],
            "final_contains": ["ORD-1001", "已取消"],
        },
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
                                "call_id": "c1",
                                "tool_name": "cancel_order",
                                "arguments": {"order_id": "ORD-1001"},
                            }
                        ]
                    }
                }
            },
        }
    ]

    score = runner.score_tool_state(task, "订单 ORD-1001 已取消。", events, store)

    assert score.passed is True
    assert score.score == 1.0
    assert score.details["failures"] == []
    assert score.details["actual_state_hash"] == expected_hash
    assert score.details["expected_state_hash"] == expected_hash


@pytest.mark.unit
def test_score_tool_state_fails_on_hash_mismatch() -> None:
    """期望 hash 与最终态 hash 不一致时必须 fail，details 同时回显 expected/actual hash。"""

    runner = _load_runner_module()
    store = runner.StatefulToolStore(_initial_orders())
    store.set_order_status("ORD-1001", "cancelled")
    task = _make_tau_task(
        runner,
        scoring={
            "type": "tool_state",
            "expected_state_hash": "0" * 64,
        },
    )

    score = runner.score_tool_state(task, "", [], store)

    assert score.passed is False
    assert any("state hash mismatch" in failure for failure in score.details["failures"])
    assert score.details["actual_state_hash"] == runner._state_sha256(store.snapshot())
    assert score.details["expected_state_hash"] == "0" * 64


@pytest.mark.unit
def test_score_tool_state_fails_when_expected_hash_missing() -> None:
    """缺 expected_state_hash 时 scorer 必须 fail（新跑必须有 hash 的硬约束）。"""

    runner = _load_runner_module()
    store = runner.StatefulToolStore(_initial_orders())
    task = _make_tau_task(
        runner,
        scoring={"type": "tool_state"},
    )

    score = runner.score_tool_state(task, "", [], store)

    assert score.passed is False
    assert any("expected_state_hash missing" in failure for failure in score.details["failures"])


@pytest.mark.unit
def test_score_tool_state_detects_spurious_unrelated_write() -> None:
    """fixture 多写一个无关字段（如 orders.ORD-1001.note）时 hash 比对必须 fail，
    用以验证全库 hash 能捕获 subset 匹配会漏掉的多余写。"""

    runner = _load_runner_module()
    store = runner.StatefulToolStore(_initial_orders())
    store.set_order_status("ORD-1001", "cancelled")
    canonical_hash = runner._state_sha256(store.snapshot())

    # 模拟 agent 多写了一个无关字段（测试内直接戳 store 内部状态，避免引入"加无关字段"工具）
    store._state["orders"]["ORD-1001"]["note"] = "x"

    task = _make_tau_task(
        runner,
        scoring={"type": "tool_state", "expected_state_hash": canonical_hash},
    )

    score = runner.score_tool_state(task, "", [], store)

    assert score.passed is False
    assert any("state hash mismatch" in failure for failure in score.details["failures"])


@pytest.mark.unit
def test_score_tool_state_fails_when_protected_path_changed() -> None:
    """保护路径被改动时必须 fail 并指出具体路径（hash 自然也对不上，但 state_unchanged 仍独立失败）。"""

    runner = _load_runner_module()
    store = runner.StatefulToolStore(_initial_orders())
    store.set_order_status("ORD-1002", "cancelled")
    actual_hash = runner._state_sha256(store.snapshot())
    task = _make_tau_task(
        runner,
        scoring={
            "type": "tool_state",
            "expected_state_hash": actual_hash,
            "state_unchanged": ["orders.ORD-1002"],
        },
    )

    score = runner.score_tool_state(task, "", [], store)

    assert score.passed is False
    assert any("orders.ORD-1002" in failure for failure in score.details["failures"])


@pytest.mark.unit
def test_score_tool_state_fails_on_forbidden_call() -> None:
    """命中 forbidden_calls 时必须 fail，即便世界状态恰好没变。"""

    runner = _load_runner_module()
    store = runner.StatefulToolStore(_initial_orders())
    initial_hash = runner._state_sha256(store.snapshot())
    task = _make_tau_task(
        runner,
        scoring={
            "type": "tool_state",
            "expected_state_hash": initial_hash,
            "forbidden_calls": [
                {"name": "cancel_order", "arguments_contains": {"order_id": "ORD-1002"}}
            ],
        },
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
                                "call_id": "c1",
                                "tool_name": "cancel_order",
                                "arguments": {"order_id": "ORD-1002"},
                            }
                        ]
                    }
                }
            },
        }
    ]

    score = runner.score_tool_state(task, "", events, store)

    assert score.passed is False
    assert any("forbidden call" in failure for failure in score.details["failures"])


@pytest.mark.unit
def test_score_tool_state_fails_on_missing_final_text() -> None:
    """final_contains 缺失关键文本时必须 fail。"""

    runner = _load_runner_module()
    store = runner.StatefulToolStore(_initial_orders())
    initial_hash = runner._state_sha256(store.snapshot())
    task = _make_tau_task(
        runner,
        scoring={
            "type": "tool_state",
            "expected_state_hash": initial_hash,
            "final_contains": ["退货"],
        },
    )

    score = runner.score_tool_state(task, "已经帮你取消了", [], store)

    assert score.passed is False
    assert any("退货" in failure for failure in score.details["failures"])


@pytest.mark.unit
def test_fixture_state_calls_build_from_fixture_calls_field() -> None:
    """fixture tool_state 调用必须来自顶层 fixture_calls 字段。"""

    runner = _load_runner_module()
    task = _make_tau_task(
        runner,
        fixture_calls=[{"name": "cancel_order", "arguments": {"order_id": "ORD-1001"}}],
    )

    calls = runner._fixture_state_calls(task)

    assert len(calls) == 1
    assert calls[0].tool_name == "cancel_order"
    assert calls[0].arguments == {"order_id": "ORD-1001"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tau_tasks_run_closed_loop_in_fixture_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1/T2 真实 yaml 在 fixture 模式下经真实 Runner 跑通状态化裁决闭环。"""

    monkeypatch.delenv("KONGMING_HOME", raising=False)
    runner = _load_runner_module()

    suite_dir = tmp_path / "tau-suite"
    (suite_dir / "tasks").mkdir(parents=True)
    for task_id in ("tau_state_cancel_001", "tau_policy_refuse_001"):
        shutil.copy(SUITE_TASKS / f"{task_id}.yaml", suite_dir / "tasks" / f"{task_id}.yaml")

    summary = await runner.run_harness_environment(
        "fixture-full",
        runner.EvalEnvironmentOverrides(
            suite=str(suite_dir),
            run_id="unit-tau-s0",
            output_dir=str(tmp_path / "runs"),
        ),
    )

    assert summary["passed"] == summary["total"] == 2

    run_dir = tmp_path / "runs" / "unit-tau-s0"
    t1 = json.loads(
        (run_dir / "tasks" / "tau_state_cancel_001" / "trajectory.json").read_text(encoding="utf-8")
    )
    assert t1["score"]["passed"] is True
    assert t1["score"]["details"]["final_state"]["orders"]["ORD-1001"]["status"] == "cancelled"
    assert t1["score"]["details"]["final_state"]["orders"]["ORD-1002"]["status"] == "shipped"

    t2 = json.loads(
        (run_dir / "tasks" / "tau_policy_refuse_001" / "trajectory.json").read_text(
            encoding="utf-8"
        )
    )
    assert t2["score"]["passed"] is True
    assert t2["score"]["details"]["final_state"]["orders"]["ORD-1002"]["status"] == "shipped"
    forbidden_names = [call["name"] for call in t2["score"]["details"]["tool_calls"]]
    assert "cancel_order" not in forbidden_names


# ---------------------------------------------------------------------------
# 过程否决（hallucinated tool result）单测：
# eval-prompt-cleanup-and-process-veto 引入的"该写却没写"过程层判据
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_score_tool_state_vetoes_when_state_change_expected_but_no_tool_call() -> None:
    """期望状态变更（expected != initial）但 0 工具调用时，过程否决必须触发并写入 failures。

    这条规则正好覆盖"模型幻觉一个'已经做完了'的回答"那类 trial——
    哪怕最终 hash 也对不上，过程否决也要给出可定位的失败理由。
    """

    runner = _load_runner_module()
    store = runner.StatefulToolStore(_initial_orders())
    initial_hash = runner._state_sha256(store.snapshot())

    # 构造一个真正"期望状态变更"的 expected_hash：在副本上模拟 cancel
    mutated = runner.StatefulToolStore(_initial_orders())
    mutated.set_order_status("ORD-1001", "cancelled")
    expected_hash = runner._state_sha256(mutated.snapshot())
    assert expected_hash != initial_hash, "前置条件：expected 必须真不等于 initial"

    task = _make_tau_task(
        runner,
        scoring={"type": "tool_state", "expected_state_hash": expected_hash},
    )

    # events 为空 → tool_call_count == 0
    score = runner.score_tool_state(task, "已经帮你取消啦", [], store)

    assert score.passed is False
    assert score.details["tool_call_count"] == 0
    assert any(
        "no tool was called" in failure and "hallucinated tool result" in failure
        for failure in score.details["failures"]
    ), f"未找到过程否决文案：{score.details['failures']}"


@pytest.mark.unit
def test_score_tool_state_does_not_veto_refuse_task_with_zero_tool_calls() -> None:
    """refuse 类题目（expected_state_hash == initial_state_hash）应该白名单——
    0 工具调用就是正确行为，不能被过程否决误杀。
    """

    runner = _load_runner_module()
    store = runner.StatefulToolStore(_initial_orders())
    initial_hash = runner._state_sha256(store.snapshot())

    task = _make_tau_task(
        runner,
        scoring={
            "type": "tool_state",
            "expected_state_hash": initial_hash,
            "final_contains": ["退货"],
        },
    )

    score = runner.score_tool_state(task, "当前订单不能取消，只能走退货流程", [], store)

    # final_contains "退货" 命中，无 hash mismatch，无过程否决 → pass
    assert score.passed is True, f"refuse 题被误杀：{score.details['failures']}"
    assert score.details["tool_call_count"] == 0
    assert not any("no tool was called" in failure for failure in score.details["failures"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_task_isolates_session_per_trial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连续两次 `run_task` 用不同 trial_index 时，必须落到独立的 session 目录，
    metadata 必须暴露 trial_index，否则 `--repeat N` pass^k 会被 session 复用污染。
    """

    monkeypatch.delenv("KONGMING_HOME", raising=False)
    runner = _load_runner_module()

    suite_dir = tmp_path / "trial-iso-suite"
    (suite_dir / "tasks").mkdir(parents=True)
    shutil.copy(
        SUITE_TASKS / "tau_state_cancel_001.yaml",
        suite_dir / "tasks" / "tau_state_cancel_001.yaml",
    )

    overrides = runner.EvalEnvironmentOverrides(
        suite=str(suite_dir),
        run_id="unit-trial-iso",
        output_dir=str(tmp_path / "runs"),
    )
    environment = runner.resolve_eval_environment("fixture-full", overrides)

    tasks = runner.load_tasks(environment.suite)
    assert len(tasks) == 1
    task = tasks[0]

    run_dir = environment.output_dir / "unit-trial-iso"
    run_dir.mkdir(parents=True, exist_ok=True)

    r0 = await runner.run_task(task, environment, "unit-trial-iso", run_dir, trial_index=0)
    r1 = await runner.run_task(task, environment, "unit-trial-iso", run_dir, trial_index=1)

    assert r0.metadata["trial_index"] == 0
    assert r1.metadata["trial_index"] == 1

    sessions_dir = run_dir / "sessions"
    session_ids = sorted(p.name for p in sessions_dir.iterdir() if p.is_dir())
    assert session_ids == [
        "unit-trial-iso-tau_state_cancel_001-trial1",
        "unit-trial-iso-tau_state_cancel_001-trial2",
    ], f"trial 间未隔离：实际 sessions={session_ids}"

    # 每个 session jsonl 都只有 1 条 user 消息（本 trial 自己的 prompt），
    # 没有上一 trial 的回声
    for sid in session_ids:
        jsonl = sessions_dir / sid / f"{sid}.jsonl"
        assert jsonl.exists(), f"缺失 session 文件 {jsonl}"
        user_count = 0
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if (rec.get("message") or {}).get("role") == "user":
                user_count += 1
        assert user_count == 1, f"{sid} 期望 1 条 user 消息，实得 {user_count}"


@pytest.mark.unit
def test_score_tool_state_does_not_veto_when_at_least_one_tool_call_made() -> None:
    """只要至少调了一次工具，过程否决就不触发；剩下交给 hash 比对判定对错。"""

    runner = _load_runner_module()
    store = runner.StatefulToolStore(_initial_orders())
    # 模型确实调了 cancel 但写错对象，导致最终 hash 不匹配
    store.set_order_status("ORD-1002", "cancelled")  # 错改了 ORD-1002 而非 ORD-1001

    mutated = runner.StatefulToolStore(_initial_orders())
    mutated.set_order_status("ORD-1001", "cancelled")
    expected_hash = runner._state_sha256(mutated.snapshot())

    task = _make_tau_task(
        runner,
        scoring={"type": "tool_state", "expected_state_hash": expected_hash},
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
                                "call_id": "c1",
                                "tool_name": "cancel_order",
                                "arguments": {"order_id": "ORD-1002"},
                            }
                        ]
                    }
                }
            },
        }
    ]

    score = runner.score_tool_state(task, "", events, store)

    assert score.passed is False
    assert score.details["tool_call_count"] == 1
    # 必须靠 hash mismatch 判 fail，过程否决不该触发
    assert any("state hash mismatch" in failure for failure in score.details["failures"])
    assert not any("no tool was called" in failure for failure in score.details["failures"])
