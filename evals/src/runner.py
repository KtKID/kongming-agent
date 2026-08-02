"""Harness Eval 执行器（单题 / 整套运行）。

# 关键函数：run_task（执行单题）、run_resolved_environment（执行整套评测并落盘）、
# run_suite_async（CLI 入口异步执行）、run_harness_environment（Python API 入口）。
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from core.agent_spec import AgentSpec
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from runtime_assembly.session_engine import SessionEngine
from tools.runtime.registry import ToolRegistry

from .environment import (
    approval_provider_for,
    build_session_factory,
    effective_task_approval_mode,
    fixture_semantics,
    isolated_home,
    load_runtime_config,
    overrides_from_args,
    resolve_eval_environment,
)
from .fake_tools import (
    EvalNoopCompactor,
    FixtureRuntimeLLM,
    RecordingEventSink,
    StatefulToolStore,
    build_eval_retail_tools,
    build_eval_tools,
)
from .loader import load_tasks
from .metrics import (
    aggregate_task_metrics,
    compute_cost,
    empty_token_totals,
    merge_token_totals,
    trial_metrics,
)
from .models import (
    EvalEnvironmentOverrides,
    ResolvedEvalEnvironment,
    RuntimeTaskResult,
    ScoreResult,
    Task,
    validate_run_id,
)
from .report import render_report, write_json
from .scoring import (
    aggregate_pass_hat_k,
    score_response,
    score_tool_execution,
    score_tool_state,
)


def _utc_run_id() -> str:
    """生成 UTC run id，输入为空，输出适合路径使用的时间戳。"""

    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


async def run_task(
    task: Task,
    environment: ResolvedEvalEnvironment,
    run_id: str,
    run_dir: Path,
    *,
    trial_index: int = 0,
) -> RuntimeTaskResult:
    """执行单题 runtime eval，输入 Task、resolved environment、run 目录与 trial 序号，输出结果。"""

    started = time.monotonic()
    sink = RecordingEventSink()
    final_content = ""
    error: str | None = None
    result_status = "failed"
    turn_count = 0
    effective_approval = effective_task_approval_mode(environment, task)
    environment_metadata = environment.as_metadata()
    metadata: dict[str, Any] = {
        **environment_metadata,
        "mode": environment.mode,
        "effective_approval_mode": effective_approval,
        "task_runtime": task.runtime,
        "trial_index": trial_index,
    }
    task_home = run_dir / "tasks" / task.id / "kongming_home"
    runtime: SessionEngine | None = None
    scoring_type = task.scoring.get("type")
    tool_state_store = (
        StatefulToolStore(task.initial_state) if scoring_type == "tool_state" else None
    )
    try:
        with isolated_home(task_home):
            config = load_runtime_config(
                environment,
                run_dir,
                effective_approval_mode=effective_approval,
            )
            if scoring_type == "tool_execution":
                registry = build_eval_tools()
            elif scoring_type == "tool_state":
                assert tool_state_store is not None
                registry = build_eval_retail_tools(tool_state_store)
            else:
                registry = ToolRegistry()
            instructions = ""
            model_catalog_manager = ModelCatalogManager()
            resolved_model = model_catalog_manager.resolve_runtime(config.model)
            agent_spec = AgentSpec(
                name=f"harness-{environment.profile}",
                instructions="",
                default_model=resolved_model.name,
                tool_names=tuple(registry.names()),
                max_turns=environment.runner_max_turns,
                reasoning_effort=config.model.reasoning_effort,
                metadata={"profile": environment.profile},
            )
            llm_provider = (
                FixtureRuntimeLLM(task)
                if environment.mode == "fixture" and not environment.preset
                else None
            )
            runtime = SessionEngine.build(
                config,
                event_sinks=[sink],
                tools=registry,
                enabled_tool_names=registry.names(),
                approval=approval_provider_for(effective_approval),
                agent_spec=agent_spec,
                instructions=instructions,
                session_factory=build_session_factory(config, instructions),
                model_catalog_manager=model_catalog_manager,
                model_config=resolved_model,
                message_compactor=(
                    EvalNoopCompactor() if environment.compactor_mode == "noop-script" else None
                ),
                llm_provider=llm_provider,
            )
            session_id = f"{run_id}-{task.id}-trial{trial_index + 1}"
            result = await runtime.run(task.prompt, session_id=session_id)
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
    if scoring_type == "tool_execution":
        score = score_tool_execution(task, final_content, sink.events)
    elif scoring_type == "tool_state":
        assert tool_state_store is not None
        score = score_tool_state(task, final_content, sink.events, tool_state_store)
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
    repeat: int = 1,
) -> dict[str, Any]:
    """异步执行已解析环境，输入 environment、可选 run id 和 repeat 次数，输出 summary。"""

    suite_dir = environment.suite
    tasks = load_tasks(suite_dir)
    resolved_run_id = validate_run_id(_utc_run_id() if run_id is None else run_id)
    output_root = environment.output_dir
    run_dir = output_root / resolved_run_id
    if run_dir.exists():
        shutil.rmtree(run_dir)
    task_records: list[dict[str, Any]] = []
    category_stats: dict[str, dict[str, float]] = defaultdict(
        lambda: {"total": 0.0, "passed": 0.0, "score": 0.0}
    )
    environment_metadata = environment.as_metadata()

    effective_repeat = 1 if environment.mode == "fixture" else max(1, repeat)
    pass_k_per_task: list[tuple[int, int]] = []

    for task in tasks:
        task_run_dir = run_dir / "tasks" / task.id
        task_run_dir.mkdir(parents=True, exist_ok=True)

        successes = 0
        last_result: RuntimeTaskResult | None = None
        trial_stats: list[dict[str, Any]] = []
        for trial_index in range(effective_repeat):
            task_result = await run_task(
                task,
                environment,
                resolved_run_id,
                run_dir,
                trial_index=trial_index,
            )
            if task_result.score.passed:
                successes += 1
            # 每个 trial 都从事件流抽取 usage/轮数指标，跨 trial 聚合成本账。
            trial_stats.append(
                trial_metrics(
                    task_result.events,
                    turn_count=task_result.turn_count,
                    duration_ms=task_result.duration_ms,
                )
            )
            last_result = task_result

        assert last_result is not None
        task_result = last_result
        task_metrics = aggregate_task_metrics(trial_stats)
        task_cost = compute_cost(task_metrics["tokens"], environment.pricing)
        if task_cost is not None:
            task_metrics["cost"] = task_cost
        record = {
            "id": task.id,
            "category": task.category,
            "source": task.source,
            "passed": task_result.score.passed,
            "score": task_result.score.score,
            "duration_ms": task_result.duration_ms,
            "error": task_result.error,
            "details": task_result.score.details,
            "metrics": task_metrics,
            "metadata": task_result.metadata,
        }
        if effective_repeat > 1:
            record["repeat"] = {"n": effective_repeat, "successes": successes}
        pass_k_per_task.append((effective_repeat, successes))
        write_json(
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
    # 全 run 成本账：跨题累加 token / LLM 调用 / 轮数 / 时长，可选按 pricing 换算成本。
    run_tokens = empty_token_totals()
    run_llm_calls = 0
    run_turns_total = 0
    run_trials = 0
    run_duration_ms_total = 0
    for record in task_records:
        record_metrics = cast(dict[str, Any], record["metrics"])
        run_tokens = merge_token_totals(run_tokens, record_metrics["tokens"])
        run_llm_calls += int(record_metrics["llm_calls"])
        run_turns_total += int(record_metrics["turns_total"])
        run_trials += int(record_metrics["trials"])
        run_duration_ms_total += int(record_metrics["duration_ms_total"])
    run_metrics: dict[str, Any] = {
        "trials": run_trials,
        "turns_total": run_turns_total,
        "llm_calls": run_llm_calls,
        "duration_ms_total": run_duration_ms_total,
        "tokens": run_tokens,
    }
    run_cost = compute_cost(run_tokens, environment.pricing)
    if run_cost is not None:
        run_metrics["cost"] = run_cost
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
        "metrics": run_metrics,
        "run_dir": str(run_dir),
        "repeat": effective_repeat,
        "trust_warning": (
            "repeat=1 单次采样，不可信"
            if effective_repeat == 1 and environment.mode == "preset"
            else None
        ),
    }
    if effective_repeat > 1:
        ks = sorted({1, 2, effective_repeat} & set(range(1, effective_repeat + 1)))
        summary["pass_hat_k"] = aggregate_pass_hat_k(pass_k_per_task, ks)
    else:
        summary["pass_hat_k"] = None
        if environment.mode == "fixture":
            summary["pass_hat_k_note"] = "fixture 确定性重放，pass^k 不适用"
        else:
            summary["pass_hat_k_note"] = "repeat=1，需 --repeat N (N>=2) 计算 pass^k"
    fixture_sem = fixture_semantics(environment)
    if fixture_sem is not None:
        summary["fixture_semantics"] = fixture_sem
    write_json(run_dir / "summary.json", summary)
    write_json(run_dir / "tasks.json", {"tasks": task_records})
    (run_dir / "report.md").write_text(render_report(summary, task_records), encoding="utf-8")
    return summary


async def run_suite_async(args: argparse.Namespace) -> dict[str, Any]:
    """异步执行整套 runtime eval，输入 CLI 参数，输出 summary。"""

    environment = resolve_eval_environment(args.environment, overrides_from_args(args))
    cli_repeat = getattr(args, "repeat", None)
    if cli_repeat is not None:
        effective_repeat = cli_repeat
        environment.override_sources["repeat"] = "cli"
    elif environment.repeat is not None:
        effective_repeat = environment.repeat
    else:
        effective_repeat = 1
        environment.override_sources["repeat"] = "default"
    return await run_resolved_environment(environment, run_id=args.run_id, repeat=effective_repeat)


async def run_harness_environment(
    environment_id: str,
    overrides: EvalEnvironmentOverrides | None = None,
) -> dict[str, Any]:
    """Python API 入口，输入 environment id 和覆盖项，输出 suite summary。"""

    environment = resolve_eval_environment(environment_id, overrides)
    return await run_resolved_environment(
        environment,
        run_id=overrides.run_id if overrides else None,
        repeat=environment.repeat or 1,
    )
