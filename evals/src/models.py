"""Harness Eval 数据类。

# 定义评测流水线中的核心值对象：Task、ScoreResult、RuntimeTaskResult、
# EvalEnvironmentOverrides、ResolvedEvalEnvironment。所有数据类 frozen=True，
# 跨模块传递时不可变。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


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
    initial_state: dict[str, Any] = dc_field(default_factory=dict)
    fixture_calls: list[dict[str, Any]] = dc_field(default_factory=list)


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
    repeat: int | None
    output_dir: Path
    api_keys_present: dict[str, bool]
    override_sources: dict[str, str]
    # 可选计费单价（每 MTok），来自 environments.yaml pricing 块；None = 未配置，只报 token 量。
    pricing: dict[str, Any] | None = None

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
            "repeat": self.repeat,
            "output_dir": str(self.output_dir),
            "api_keys_present": self.api_keys_present,
            "override_sources": self.override_sources,
            "pricing": self.pricing,
        }


def validate_run_id(run_id: str) -> str:
    """校验 run id 为单段路径名，输入 run id，输出原值。"""

    if not run_id or not _RUN_ID_RE.fullmatch(run_id) or not run_id.strip("."):
        raise ValueError("run_id must contain only letters, digits, underscore, dash and dot")
    return run_id


_DEFAULT_SUITE = Path("evals/harness-runtime-v0.1")
_DEFAULT_ENVIRONMENT_CONFIG = _DEFAULT_SUITE / "environments.yaml"
_DEFAULT_MAX_TURNS = 50
