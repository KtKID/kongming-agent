"""Kongming Harness Eval 执行器（薄包装）。

# 实际实现已拆分到 evals/src/ 包。本文件保持原 CLI 调用路径和符号 re-export 不变。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evals.src.__main__ import main  # noqa: E402
from evals.src.environment import (  # noqa: E402
    _normalized_pricing,
    resolve_eval_environment,
)
from evals.src.fake_tools import (  # noqa: E402
    StatefulToolStore,
    _fixture_state_calls,
    _fixture_tool_calls,
)
from evals.src.loader import load_tasks  # noqa: E402
from evals.src.metrics import (  # noqa: E402
    aggregate_task_metrics,
    compute_cost,
    empty_token_totals,
    merge_token_totals,
    trial_metrics,
    usage_totals_from_events,
)
from evals.src.models import (  # noqa: E402
    EvalEnvironmentOverrides,
    ResolvedEvalEnvironment,
    RuntimeTaskResult,
    ScoreResult,
    Task,
    validate_run_id,
)
from evals.src.report import render_report  # noqa: E402
from evals.src.runner import (  # noqa: E402
    run_harness_environment,
    run_resolved_environment,
    run_suite_async,
    run_task,
)
from evals.src.sandbox import (  # noqa: E402
    _pytest_env,
    apply_model_diff,
    init_repo_with_base,
)
from evals.src.scoring import (  # noqa: E402
    _arguments_contain,
    _dotted_get,
    _state_sha256,
    aggregate_pass_hat_k,
    pass_hat_k,
    score_response,
    score_tool_execution,
    score_tool_state,
)

_validate_run_id = validate_run_id
_apply_model_diff = apply_model_diff
_init_repo_with_base = init_repo_with_base

__all__ = [
    "EvalEnvironmentOverrides",
    "ResolvedEvalEnvironment",
    "RuntimeTaskResult",
    "ScoreResult",
    "StatefulToolStore",
    "Task",
    "_apply_model_diff",
    "_arguments_contain",
    "_dotted_get",
    "_fixture_state_calls",
    "_fixture_tool_calls",
    "_init_repo_with_base",
    "_normalized_pricing",
    "_pytest_env",
    "_state_sha256",
    "_validate_run_id",
    "aggregate_pass_hat_k",
    "aggregate_task_metrics",
    "apply_model_diff",
    "compute_cost",
    "empty_token_totals",
    "init_repo_with_base",
    "load_tasks",
    "main",
    "merge_token_totals",
    "pass_hat_k",
    "render_report",
    "resolve_eval_environment",
    "run_harness_environment",
    "run_resolved_environment",
    "run_suite_async",
    "run_task",
    "score_response",
    "score_tool_execution",
    "score_tool_state",
    "trial_metrics",
    "usage_totals_from_events",
    "validate_run_id",
]

if __name__ == "__main__":
    raise SystemExit(main())
