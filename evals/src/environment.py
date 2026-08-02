"""Harness Eval 环境解析与 runtime 配置。

# 从 environments.yaml 加载环境预设，结合 CLI 覆盖解析出 ResolvedEvalEnvironment；
# 加载并隔离 Kongming runtime config；构造 session factory 和 approval provider。
# 关键函数：resolve_eval_environment、load_runtime_config、build_session_factory。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from core.contracts import ApprovalAction, ApprovalRequest
from infrastructure.config import load_config
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.model_provider_catalog import ResolvedModelConfig
from infrastructure.config.models import Config
from sessions import SessionBootstrap, build_session
from tools.runtime.approval import AutoAllowApproval, InteractiveApproval

from . import REPO_ROOT
from .models import (
    _DEFAULT_ENVIRONMENT_CONFIG,
    _DEFAULT_MAX_TURNS,
    _DEFAULT_SUITE,
    EvalEnvironmentOverrides,
    ResolvedEvalEnvironment,
)


def _read_yaml(path: Path) -> dict[str, Any]:
    """读取单个 YAML 文件，输入路径，输出字典。"""

    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: YAML root must be an object")
    return payload


def _sha256_file(path: Path) -> str:
    """计算文件 sha256，输入路径，输出 sha256:<hex> 字符串。"""

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_repo_path(value: str | Path) -> Path:
    """把相对仓库路径解析为绝对路径，输入字符串或 Path，输出绝对 Path。"""

    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (REPO_ROOT / path).resolve()


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
        "repeat",
        "runner",
        "artifacts",
        "pricing",
    }
    allowed_nested = {
        "runner": {"max_turns"},
        "artifacts": {"output_dir"},
        "pricing": {
            "currency",
            "input_per_mtok",
            "output_per_mtok",
            "cache_read_per_mtok",
            "cache_write_per_mtok",
        },
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


def _normalized_pricing(
    raw: Any,
    *,
    context: str = "environments.yaml",
) -> dict[str, Any] | None:
    """校验并归一化可选 pricing 块，输入原始值，输出归一化字典或 None。

    规则：pricing 缺省 = None（只报 token 量，不算成本）；配置时 currency /
    input_per_mtok / output_per_mtok 必填，cache 读/写单价缺省回落到 input 单价
    （即"无缓存折扣"的保守口径），所有单价必须为非负数值。
    """

    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{context}: pricing must be an object")
    for required in ("currency", "input_per_mtok", "output_per_mtok"):
        if required not in raw:
            raise ValueError(f"{context}: pricing missing required field {required!r}")
    currency = raw["currency"]
    if not isinstance(currency, str) or not currency.strip():
        raise ValueError(f"{context}: pricing.currency must be a non-empty string")

    def _price(field: str, default: float | None = None) -> float:
        """读取单个单价字段，输入字段名与缺省值，输出非负 float。"""

        value = raw.get(field, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"{context}: pricing.{field} must be a non-negative number")
        return float(value)

    input_price = _price("input_per_mtok")
    return {
        "currency": currency.strip(),
        "input_per_mtok": input_price,
        "output_per_mtok": _price("output_per_mtok"),
        "cache_read_per_mtok": _price("cache_read_per_mtok", input_price),
        "cache_write_per_mtok": _price("cache_write_per_mtok", input_price),
    }


def _validate_environment_entry(
    entry: dict[str, Any],
    *,
    path: Path,
    environment_id: str,
) -> None:
    """校验单个 environment schema，输入配置字典，无返回值。"""

    _reject_unknown_environment_fields(entry, path=path, environment_id=environment_id)
    _normalized_pricing(
        entry.get("pricing"),
        context=f"{path}: environment {environment_id!r}",
    )
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
        _require_environment_field(entry, "repeat", path=path, environment_id=environment_id)
        repeat_val = entry.get("repeat")
        if not isinstance(repeat_val, int) or repeat_val < 4:
            raise ValueError(
                f"preset environment {environment_id!r} repeat={repeat_val} < 4; "
                f"preset runs require repeat≥4 for trustworthy pass^k; "
                f"set --repeat 1 explicitly if you really want a smoke run"
            )


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


def overrides_from_args(args: argparse.Namespace) -> EvalEnvironmentOverrides:
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


def _find_preset(config: Config, preset_id: str) -> ResolvedModelConfig:
    """按 id 从 catalog 门户解析不可变运行快照。"""
    manager = ModelCatalogManager()
    try:
        return manager.resolve_runtime(config.model, preset_id=preset_id)
    except ValueError as exc:
        available = ", ".join(model.preset_id for model in manager.list_models()) or "<none>"
        raise ValueError(f"unknown preset {preset_id!r}; available presets: {available}") from exc


def _api_keys_present(config: Config, preset_id: str | None) -> dict[str, bool]:
    """记录 preset 关联密钥是否存在，输入 Config 和 preset id，输出 env 名到布尔值。"""

    if not preset_id:
        return {}
    runtime = _find_preset(config, preset_id)
    if not runtime.api_key_env:
        return {}
    return {runtime.api_key_env: bool(os.environ.get(runtime.api_key_env))}


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
    env_repeat_raw = entry.get("repeat")
    env_repeat: int | None = None
    if env_repeat_raw is not None:
        env_repeat = _validate_positive_int(env_repeat_raw, field="repeat")
    override_sources["repeat"] = "environment" if env_repeat is not None else "default"

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
        repeat=env_repeat,
        output_dir=output_dir,
        api_keys_present=_api_keys_present(config, preset),
        override_sources=override_sources,
        pricing=_normalized_pricing(entry.get("pricing")),
    )


def effective_task_approval_mode(environment: ResolvedEvalEnvironment, task) -> str:
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


def approval_provider_for(mode: str):
    """按有效审批模式构造底层 provider，输入模式，输出 ApprovalProvider。"""

    if mode == "auto_allow":
        return AutoAllowApproval()
    if mode == "interactive":
        if not sys.stdin.isatty():
            raise RuntimeError("interactive approval requires a TTY")
        return InteractiveApproval(_prompt_eval_approval)
    raise ValueError(f"unsupported effective approval mode: {mode}")


def fixture_semantics(environment: ResolvedEvalEnvironment) -> dict[str, Any] | None:
    """描述 fixture 模式验证边界，输入环境，输出 summary 元数据。"""

    if environment.mode != "fixture":
        return None
    return {
        "uses_real_runner": True,
        "uses_real_llm_provider": False,
        "tool_execution_checks_tool_loop": True,
        "non_tool_tasks_check": [
            "SessionEngine.run request/response path",
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
        _find_preset(config, environment.preset)
        config = config.model_copy(
            update={
                "model": config.model.model_copy(
                    update={"preset_id": environment.preset, "reasoning_effort": None}
                )
            }
        )

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
    resolved_model = ModelCatalogManager().resolve_runtime(config.model)

    bootstrap = SessionBootstrap(
        agent_name="harness-runtime-eval",
        model_name=resolved_model.name,
        instruction_sources=["harness-runtime-eval"],
        instruction_text_hash="sha256:" + hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
        instruction_text=instructions,
        created_at=time.time(),
        cwd=str(REPO_ROOT),
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
