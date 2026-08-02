"""Harness Eval 成本 / 轮数指标单元测试。

覆盖 ``evals/src/metrics.py`` 的 usage 归一化（anthropic / openai 两种口径）、
trial → task 聚合、pricing 换算（compute_cost 精确算术 + 未配置返回 None）、
``_normalized_pricing`` 校验规则、``render_report`` 的"成本与轮数"段渲染，
以及 fixture 模式闭环 run 后 summary/tasks/report 三处指标落盘。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "scripts" / "run_kongming_harness_eval.py"
SUITE_TASKS = REPO_ROOT / "evals" / "harness-runtime-v0.1" / "tasks"

# 真实 preset run（anthropic provider）落盘的 usage payload 样本，字段口径以此为准。
_ANTHROPIC_USAGE = {
    "provider_kind": "anthropic",
    "input_tokens": 491,
    "output_tokens": 53,
    "cache_read_input_tokens": 114,
    "cache_creation_input_tokens": 0,
    "prompt_tokens": 605,
    "completion_tokens": 53,
    "total_tokens": 658,
}


def _load_runner_module():
    """加载 runtime eval 脚本模块，返回可直接调用的 module。"""

    spec = importlib.util.spec_from_file_location("run_kongming_harness_eval", RUNNER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_kongming_harness_eval"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _usage_event(payload: dict[str, Any]) -> dict[str, Any]:
    """构造一条 kind=usage 事件，输入 payload，输出事件字典。"""

    return {"kind": "usage", "turn": 1, "payload": payload}


def _make_summary(**overrides: Any) -> dict[str, Any]:
    """构造 render_report 需要的最小 summary，输入覆盖项，输出 summary 字典。"""

    base: dict[str, Any] = {
        "run_id": "unit-metrics",
        "suite": "evals/harness-runtime-v0.1",
        "mode": "fixture",
        "model": "fixture",
        "environment_id": "fixture-full",
        "profile": "full",
        "approval_mode": "auto_allow",
        "session_backend": "file",
        "compactor_mode": "noop-script",
        "runner_max_turns": 50,
        "environment": {},
        "total": 1,
        "passed": 1,
        "score": 1.0,
        "categories": {},
        "repeat": 1,
        "pass_hat_k": None,
        "pass_hat_k_note": "fixture 确定性重放，pass^k 不适用",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# usage 归一化：anthropic / openai / 空 payload 三种口径
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_usage_totals_normalizes_anthropic_payload() -> None:
    """anthropic 口径 payload 必须逐字段归一化，非 usage 事件必须被忽略。"""

    runner = _load_runner_module()
    events = [
        {"kind": "run.start", "payload": {}},
        _usage_event(dict(_ANTHROPIC_USAGE)),
        {"kind": "tool.end", "payload": {"output": "x"}},
    ]

    totals = runner.usage_totals_from_events(events)

    assert totals["llm_calls"] == 1
    assert totals["tokens"] == {
        "prompt": 605,
        "uncached_prompt": 491,
        "cache_read": 114,
        "cache_write": 0,
        "completion": 53,
        "total": 658,
    }


@pytest.mark.unit
def test_usage_totals_derives_missing_fields_per_style() -> None:
    """openai 口径推导未命中量；anthropic 无 compat 字段时推导 prompt/total。"""

    runner = _load_runner_module()

    openai = runner.usage_totals_from_events(
        [_usage_event({"prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 30})]
    )
    assert openai["tokens"]["prompt"] == 100
    assert openai["tokens"]["cache_read"] == 30
    assert openai["tokens"]["uncached_prompt"] == 70
    assert openai["tokens"]["completion"] == 20
    assert openai["tokens"]["total"] == 120

    anthropic_raw = runner.usage_totals_from_events(
        [
            _usage_event(
                {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 90,
                    "cache_creation_input_tokens": 20,
                }
            )
        ]
    )
    assert anthropic_raw["tokens"]["prompt"] == 120  # 10 未命中 + 90 cache读 + 20 cache写
    assert anthropic_raw["tokens"]["uncached_prompt"] == 10
    assert anthropic_raw["tokens"]["total"] == 125


@pytest.mark.unit
def test_usage_totals_counts_calls_but_zero_tokens_on_empty_or_bad_payload() -> None:
    """fixture 伪 LLM 空 payload：llm_calls 照计、token 记 0；非法值必须归 0 不炸。"""

    runner = _load_runner_module()
    events = [
        _usage_event({}),
        _usage_event({"input_tokens": -5, "output_tokens": True}),
    ]

    totals = runner.usage_totals_from_events(events)

    assert totals["llm_calls"] == 2
    assert totals["tokens"] == runner.empty_token_totals()


# ---------------------------------------------------------------------------
# trial → task 聚合
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_aggregate_task_metrics_sums_tokens_and_reports_turn_stats() -> None:
    """跨 trial 聚合：token/调用/时长求和，轮数报均值与最大值，per_trial 留明细。"""

    runner = _load_runner_module()
    trial_a = runner.trial_metrics(
        [_usage_event(dict(_ANTHROPIC_USAGE))], turn_count=3, duration_ms=1000
    )
    trial_b = runner.trial_metrics(
        [_usage_event(dict(_ANTHROPIC_USAGE)), _usage_event(dict(_ANTHROPIC_USAGE))],
        turn_count=5,
        duration_ms=2000,
    )

    metrics = runner.aggregate_task_metrics([trial_a, trial_b])

    assert metrics["trials"] == 2
    assert metrics["turns_total"] == 8
    assert metrics["turns_mean"] == 4.0
    assert metrics["turns_max"] == 5
    assert metrics["llm_calls"] == 3
    assert metrics["duration_ms_total"] == 3000
    assert metrics["tokens"]["prompt"] == 605 * 3
    assert metrics["tokens"]["completion"] == 53 * 3
    assert len(metrics["per_trial"]) == 2
    assert metrics["per_trial"][0]["turns"] == 3


@pytest.mark.unit
def test_aggregate_task_metrics_handles_empty_trials() -> None:
    """空 trial 列表必须输出全 0 结构，不抛 ZeroDivisionError。"""

    runner = _load_runner_module()

    metrics = runner.aggregate_task_metrics([])

    assert metrics["trials"] == 0
    assert metrics["turns_mean"] == 0.0
    assert metrics["tokens"] == runner.empty_token_totals()


# ---------------------------------------------------------------------------
# pricing 换算与校验
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compute_cost_exact_breakdown_and_none_without_pricing() -> None:
    """成本按四个 bucket × 每 MTok 单价精确换算；未配置 pricing 必须返回 None。"""

    runner = _load_runner_module()
    tokens = {
        "prompt": 3_500_000,
        "uncached_prompt": 1_000_000,
        "cache_read": 2_000_000,
        "cache_write": 500_000,
        "completion": 250_000,
        "total": 3_750_000,
    }
    pricing = {
        "currency": "USD",
        "input_per_mtok": 1.0,
        "output_per_mtok": 4.0,
        "cache_read_per_mtok": 0.1,
        "cache_write_per_mtok": 1.25,
    }

    cost = runner.compute_cost(tokens, pricing)

    assert cost is not None
    assert cost["currency"] == "USD"
    assert cost["breakdown"] == {
        "uncached_prompt": 1.0,
        "cache_read": 0.2,
        "cache_write": 0.625,
        "completion": 1.0,
    }
    assert cost["total"] == 2.825
    assert runner.compute_cost(tokens, None) is None


@pytest.mark.unit
def test_normalized_pricing_defaults_and_validation() -> None:
    """cache 读/写单价缺省回落 input 单价；缺必填、负数、bool、空币种必须报错。"""

    runner = _load_runner_module()

    assert runner._normalized_pricing(None) is None

    normalized = runner._normalized_pricing(
        {"currency": " USD ", "input_per_mtok": 2, "output_per_mtok": 8}
    )
    assert normalized == {
        "currency": "USD",
        "input_per_mtok": 2.0,
        "output_per_mtok": 8.0,
        "cache_read_per_mtok": 2.0,
        "cache_write_per_mtok": 2.0,
    }

    with pytest.raises(ValueError, match="missing required field"):
        runner._normalized_pricing({"currency": "USD", "input_per_mtok": 1})
    with pytest.raises(ValueError, match="non-negative number"):
        runner._normalized_pricing({"currency": "USD", "input_per_mtok": -1, "output_per_mtok": 4})
    with pytest.raises(ValueError, match="non-negative number"):
        runner._normalized_pricing(
            {"currency": "USD", "input_per_mtok": True, "output_per_mtok": 4}
        )
    with pytest.raises(ValueError, match="non-empty string"):
        runner._normalized_pricing({"currency": " ", "input_per_mtok": 1, "output_per_mtok": 4})
    with pytest.raises(ValueError, match="must be an object"):
        runner._normalized_pricing("USD")


# ---------------------------------------------------------------------------
# 报表渲染
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_render_report_metrics_section_with_and_without_pricing() -> None:
    """有 cost 时渲染估算成本与每题成本列；无 pricing 时明示只报 token 量。"""

    runner = _load_runner_module()
    tokens = {
        "prompt": 605,
        "uncached_prompt": 491,
        "cache_read": 114,
        "cache_write": 0,
        "completion": 53,
        "total": 658,
    }
    task_metrics = {
        "trials": 1,
        "turns_total": 3,
        "turns_mean": 3.0,
        "turns_max": 3,
        "llm_calls": 1,
        "duration_ms_total": 1200,
        "tokens": dict(tokens),
    }
    record = {
        "id": "tau_state_cancel_001",
        "category": "tau_tool_state",
        "passed": True,
        "score": 1.0,
        "details": {},
        "metrics": dict(task_metrics),
    }
    run_metrics: dict[str, Any] = {
        "trials": 1,
        "turns_total": 3,
        "llm_calls": 1,
        "duration_ms_total": 1200,
        "tokens": dict(tokens),
    }

    without_pricing = runner.render_report(_make_summary(metrics=dict(run_metrics)), [dict(record)])
    assert "## 成本与轮数" in without_pricing
    assert "未配置 pricing，仅报 token 量" in without_pricing
    assert "prompt `605`" in without_pricing
    assert "`18.8%`" in without_pricing  # 114 / 605 缓存命中率
    assert "| `tau_state_cancel_001` | 3.0 | 1 | 605 | 114 | 0 | 53 |" in without_pricing
    assert "成本 |" not in without_pricing

    cost = {"currency": "USD", "total": 0.000703, "breakdown": {}}
    record_with_cost = dict(record)
    record_with_cost["metrics"] = {**task_metrics, "cost": dict(cost)}
    with_pricing = runner.render_report(
        _make_summary(metrics={**run_metrics, "cost": dict(cost)}),
        [record_with_cost],
    )
    assert "估算成本：`0.000703 USD`" in with_pricing
    assert "| 成本 |" in with_pricing
    assert "0.000703 USD |" in with_pricing


@pytest.mark.unit
def test_render_report_backward_compatible_without_metrics() -> None:
    """旧版 summary（无 metrics 键）必须照常渲染且不出现成本段。"""

    runner = _load_runner_module()
    record = {
        "id": "legacy_001",
        "category": "coding",
        "passed": True,
        "score": 1.0,
        "details": {},
    }

    report = runner.render_report(_make_summary(), [record])

    assert "## 成本与轮数" not in report
    assert "## 任务明细" in report


# ---------------------------------------------------------------------------
# fixture 闭环：summary / tasks 记录 / report.md 三处指标落盘
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fixture_run_persists_metrics_in_summary_tasks_and_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fixture 闭环 run 后：summary.metrics 为各题之和、每题记录带 per_trial、
    report.md 含成本与轮数段；fixture usage 为空 → llm_calls 照计、token 记 0、无 cost。
    """

    monkeypatch.delenv("KONGMING_HOME", raising=False)
    runner = _load_runner_module()

    suite_dir = tmp_path / "metrics-suite"
    (suite_dir / "tasks").mkdir(parents=True)
    for task_id in ("tau_state_cancel_001", "tau_policy_refuse_001"):
        shutil.copy(SUITE_TASKS / f"{task_id}.yaml", suite_dir / "tasks" / f"{task_id}.yaml")

    summary = await runner.run_harness_environment(
        "fixture-full",
        runner.EvalEnvironmentOverrides(
            suite=str(suite_dir),
            run_id="unit-metrics-run",
            output_dir=str(tmp_path / "runs"),
        ),
    )

    run_metrics = summary["metrics"]
    assert run_metrics["trials"] == 2
    assert run_metrics["llm_calls"] >= 1  # usage 事件每次 LLM 响应必发，空 payload 也计次
    assert run_metrics["turns_total"] >= 2
    assert run_metrics["tokens"] == runner.empty_token_totals()  # fixture usage 为空
    assert "cost" not in run_metrics  # fixture-full 未配置 pricing

    run_dir = tmp_path / "runs" / "unit-metrics-run"
    tasks_payload = json.loads((run_dir / "tasks.json").read_text(encoding="utf-8"))
    records = tasks_payload["tasks"]
    assert len(records) == 2
    for record in records:
        metrics = record["metrics"]
        assert metrics["trials"] == 1
        assert len(metrics["per_trial"]) == 1
        assert metrics["tokens"] == runner.empty_token_totals()
    assert run_metrics["llm_calls"] == sum(r["metrics"]["llm_calls"] for r in records)
    assert run_metrics["turns_total"] == sum(r["metrics"]["turns_total"] for r in records)

    report_text = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "## 成本与轮数" in report_text
    assert "未配置 pricing，仅报 token 量" in report_text
