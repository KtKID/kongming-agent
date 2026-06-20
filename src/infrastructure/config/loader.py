"""统一配置加载。

:func:`load_config` 是唯一入口。读取顺序：

1. 若 ``path`` 参数显式传入，使用该路径。
2. 否则若环境变量 ``KONGMING_CONFIG`` 存在，使用它。
3. 否则使用仓库内的 ``config/setting.yaml``（相对 :data:`_REPO_ROOT`）。

读完 YAML 之后，用 ``KONGMING_<SECTION>_<FIELD>``（全大写）格式的环境变量覆
盖单字段。例如：

- ``KONGMING_MODEL_API_KEY`` → ``model.api_key``
- ``KONGMING_MODEL_BASE_URL`` → ``model.base_url``
- ``KONGMING_RUNNER_MAX_TURNS`` → ``runner.max_turns``
- ``KONGMING_TOOL_SHELL_ENABLED`` → ``tool.shell.enabled``

覆盖规则只影响**标量字段**（str / int / float / bool），不允许通过环境变量
整体替换一个 section。多层嵌套（例如 ``tool.shell.enabled``）按下划线切成
一串 section path，从 YAML 产出的原始 dict 上按路径覆盖。

覆盖值类型解析规则：读出的环境变量都是字符串，交给 pydantic 在构造
:class:`Config` 时做强转（pydantic v2 对 ``"true"``/``"false"``/``"1"`` 等都
有内置解析）。这里不做提前 cast，避免在配置层出现第二套类型规则。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from infrastructure.config.errors import ConfigLoadError, ConfigValidationError
from infrastructure.config.models import Config

# 仓库根。src/infrastructure/config/loader.py → src/infrastructure/ → src/ → <repo root>。
_REPO_ROOT: Path = Path(__file__).resolve().parents[3]
_DEFAULT_CONFIG_PATH: Path = _REPO_ROOT / "config" / "setting.yaml"

# 环境变量前缀。任何以 ``KONGMING_`` 开头、且按 ``_`` 切分后能落到有效 section
# path 的变量都会参与覆盖。
_ENV_PREFIX = "KONGMING_"

# 已知的配置字段路径（tuple of section parts）。用于把 ``KONGMING_MODEL_API_KEY``
# 这种变量名匹配回 ``("model", "api_key")``。
#
# 这里不做反射扫描 pydantic 模型，而是显式列出——因为字段名里本身可能含下划线
# （如 ``api_key`` / ``base_url`` / ``max_turns``），反射解析歧义难排除。
_ENV_FIELD_PATHS: tuple[tuple[str, ...], ...] = (
    ("model", "provider"),
    ("model", "name"),
    ("model", "base_url"),
    ("model", "api_key"),
    ("model", "timeout"),
    ("model", "max_tokens"),
    ("model", "temperature"),
    ("model", "reasoning_effort"),
    ("runner", "max_turns"),
    ("session", "backend"),
    ("session", "store_path"),
    ("session", "file_store_path"),
    ("trace", "output_path"),
    ("trace", "auto_flush"),
    ("trace", "raw_llm"),
    ("logging", "level"),
    ("host", "kind"),
    ("approval", "mode"),
    ("tool", "shell", "enabled"),
    ("tool", "shell", "timeout_seconds"),
    ("tool", "shell", "max_stream_bytes"),
    ("tool", "shell", "terminate_grace_seconds"),
    ("tool", "file", "enabled"),
    ("tool", "file", "read_max_bytes"),
    ("mcp", "enabled"),
    ("web_search", "enabled"),
    ("web_search", "provider_name"),
    ("web_search", "search_tool_name"),
    ("web_search", "search_tool_names"),
    ("web_search", "max_results"),
    ("compactor", "enabled"),
    ("compactor", "max_messages"),
    ("compactor", "keep_recent"),
    ("compactor", "keep_system"),
    ("compactor", "tool_result_max_chars"),
    ("retry", "max_retries"),
    ("retry", "retry_backoff"),
    ("evolution", "memory", "enabled"),
    ("evolution", "memory", "root_path"),
    ("evolution", "memory", "inject_prompt"),
    ("evolution", "memory", "read_max_chars"),
    ("evolution", "memory", "view_max_chars"),
    ("evolution", "learning", "enabled"),
    ("evolution", "learning", "mode"),
    ("evolution", "learning", "background"),
    ("evolution", "learning", "model_name"),
    ("evolution", "learning", "base_url"),
    ("evolution", "learning", "api_key_env"),
    ("evolution", "learning", "provider"),
    ("evolution", "learning", "reasoning_effort"),
    ("evolution", "learning", "every_n_runs"),
    ("evolution", "learning", "min_user_turns"),
    ("evolution", "learning", "max_history_messages"),
    ("evolution", "learning", "max_nutrients"),
    ("evolution", "learning", "nutrient_confidence_threshold"),
    ("evolution", "learning", "review_timeout_seconds"),
    ("evolution", "learning", "drain_on_close_seconds"),
    ("evolution", "learning", "root_path"),
    ("stream", "enabled"),
    ("stream", "read_timeout"),
    ("stream", "suppress_content_after_tool_call"),
    ("stream", "delta_sampling"),
    ("stream", "periodic_batch_size"),
    # safety v0.1.4
    ("safety", "trusted_workdirs"),
    ("safety", "allow_writes"),
    ("safety", "allow_tools_silent"),
    ("safety", "log_silent_reads"),
    # web v0.1.5
    ("web", "enabled"),
    ("web", "host"),
    ("web", "port"),
    ("web", "server_origin"),
    ("web", "public_origin"),
    ("web", "host_environment"),
    ("web", "dev_mode"),
    ("web", "initial_password"),
    ("web", "idle_timeout_seconds"),
    ("web", "idle_check_interval_seconds"),
    ("web", "dashboard_poll_interval_seconds"),
    ("web", "pending_approval_timeout_seconds"),
    # web.full_log (full-log-v0.1)
    ("web", "full_log", "enabled"),
    ("web", "full_log", "path"),
    ("web", "full_log", "rotate_daily"),
    ("web", "full_log", "include_http_body"),
    ("web", "full_log", "queue_size"),
    ("web", "deep_research_source_provider", "enabled"),
    ("web", "deep_research_source_provider", "provider_name"),
    ("web", "deep_research_source_provider", "search_tool_name"),
    ("web", "deep_research_source_provider", "fetch_tool_name"),
    ("web", "deep_research_source_provider", "search_tool_names"),
    ("web", "deep_research_source_provider", "fetch_tool_names"),
    ("scheduler", "approval", "allow_write_file_create_in_cwd"),
    ("sitian", "version"),
    ("sitian", "default_scan_interval_sec"),
    ("sitian", "idle_sleep_sec"),
    ("sitian", "scanner", "recent_session_window_days"),
    ("sitian", "scanner", "session_recent_user_messages"),
    ("sitian", "scanner", "session_recent_assistant_messages"),
    ("sitian", "scanner", "session_message_max_chars"),
    # sitian.analyzer
    ("sitian", "analyzer", "enabled"),
    ("sitian", "analyzer", "model_name"),
    ("sitian", "analyzer", "base_url"),
    ("sitian", "analyzer", "api_key_env"),
    ("sitian", "analyzer", "max_tokens"),
    ("sitian", "analyzer", "temperature"),
    ("sitian", "analyzer", "timeout"),
    ("sitian", "analyzer", "max_context_chars"),
    ("sitian", "analyzer", "skip_if_unchanged"),
    ("sitian", "analyzer", "full_log_enabled"),
    # sitian.interests
    ("sitian", "interests", "focus"),
)

_SCHEDULER_EXTRA_ENV_NAMES: tuple[str, ...] = (
    "KONGMING_SCHEDULER_ENABLED",
    "KONGMING_SCHEDULER_INTERVAL",
    "KONGMING_SCHEDULER_MAX_INFLIGHT",
    "KONGMING_SCHEDULER_APPROVAL_MODE",
    "KONGMING_SCHEDULER_DEFAULT_MAX_TURNS",
)

# per-module YAML 文件名 → 合并到 Config 的顶层 key。
# 所有配置已合并到 setting.yaml，per-module 文件不再使用。保留空 map 以兼容旧路径。
_MODULE_YAML_MAP: dict[str, str] = {}


def _resolve_config_path(explicit: str | Path | None) -> Path:
    """按优先级解析实际要读的配置文件路径。"""
    if explicit is not None:
        path = Path(explicit)
    else:
        env_path = os.environ.get("KONGMING_CONFIG")
        path = Path(env_path) if env_path else _DEFAULT_CONFIG_PATH
    return path


def _load_yaml(path: Path) -> dict[str, Any]:
    """读 YAML 并做基本结构校验。"""
    if not path.exists():
        raise ConfigLoadError(
            f"config file not found: {path}",
            details={"path": str(path)},
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigLoadError(
            f"failed to read config file {path}: {exc}",
            details={"path": str(path), "cause": type(exc).__name__},
        ) from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigLoadError(
            f"failed to parse YAML at {path}: {exc}",
            details={"path": str(path)},
        ) from exc

    # 允许空文件，按空 dict 处理（但 ``model`` 是必填，后面会在 pydantic 校验时报错）。
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigLoadError(
            f"config root must be a mapping, got {type(data).__name__} at {path}",
            details={"path": str(path)},
        )
    return data


def _env_var_name(path: tuple[str, ...]) -> str:
    """把 section path 翻译成环境变量名。"""
    return _ENV_PREFIX + "_".join(part.upper() for part in path)


def _set_nested(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    """按 section path 写入嵌套 dict，缺失层级会逐级补 dict。"""
    cursor = data
    for key in path[:-1]:
        nxt = cursor.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[key] = nxt
        cursor = nxt
    cursor[path[-1]] = value


def _config_env_names() -> tuple[str, ...]:
    """返回 Config 字段消费的 env 名。"""
    return tuple(_env_var_name(field_path) for field_path in _ENV_FIELD_PATHS) + tuple(
        _SCHEDULER_EXTRA_ENV_NAMES
    )


def _apply_env_overrides(
    data: dict[str, Any],
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """对已经从 YAML 读出来的 dict 按环境变量叠加覆盖。"""
    env_map = os.environ if env is None else env
    for field_path in _ENV_FIELD_PATHS:
        env_name = _env_var_name(field_path)
        if env_name in env_map:
            _set_nested(data, field_path, env_map[env_name])
    _apply_scheduler_env_overrides(data, env=env_map)
    return data


# ---------------------------------------------------------------------------
# Scheduler env overrides（v0.2）
# ---------------------------------------------------------------------------
#
# 与通用 env 覆盖的差异：scheduler 字段的非法值（如 INTERVAL=abc）走"stderr
# warning + 用默认值"路径，而不是抛 ConfigValidationError。这是 cron v0.2 的
# 显式产品决策——env 给出来历未必清晰，不应让 cron 配置错让整个进程起不来。
#
# 实现：在 dict 进入 pydantic 之前先解析 / 校验；解析失败 → 不写入 dict（即
# 走默认值）+ 一行 stderr warning。
_BOOL_TRUE_TOKENS: frozenset[str] = frozenset({"1", "true", "yes", "on"})
_BOOL_FALSE_TOKENS: frozenset[str] = frozenset({"0", "false", "no", "off"})


def _parse_bool_env(raw: str) -> bool | None:
    """解析 0/1/true/false/yes/no/on/off（大小写不敏感）；非法返回 None。"""
    token = raw.strip().lower()
    if token in _BOOL_TRUE_TOKENS:
        return True
    if token in _BOOL_FALSE_TOKENS:
        return False
    return None


def _warn(msg: str) -> None:
    """env 解析失败的统一 stderr 提示。集中一个出口便于将来切到 logger。"""
    print(f"[infrastructure.config] warning: {msg}", file=sys.stderr)


def _apply_scheduler_env_overrides(
    data: dict[str, Any],
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """把 ``KONGMING_SCHEDULER_*`` env 写入 ``data["scheduler"]``。

    支持的 env：

    - ``KONGMING_SCHEDULER_ENABLED``：bool（0/1/true/false/yes/no/on/off）
    - ``KONGMING_SCHEDULER_INTERVAL``：float
    - ``KONGMING_SCHEDULER_MAX_INFLIGHT``：int
    - ``KONGMING_SCHEDULER_APPROVAL_MODE``：str（fail_closed / trust）
    - ``KONGMING_SCHEDULER_DEFAULT_MAX_TURNS``：int（>0，v0.5.1 新增）

    非法值统一 stderr warning + 不写入 dict（保留 yaml / 默认值）。
    """
    env_map = os.environ if env is None else env

    raw_enabled = env_map.get("KONGMING_SCHEDULER_ENABLED")
    if raw_enabled is not None:
        parsed_bool = _parse_bool_env(raw_enabled)
        if parsed_bool is None:
            _warn(
                f"KONGMING_SCHEDULER_ENABLED={raw_enabled!r} is not a valid boolean; using default."
            )
        else:
            _set_nested(data, ("scheduler", "enabled"), parsed_bool)

    raw_interval = env_map.get("KONGMING_SCHEDULER_INTERVAL")
    if raw_interval is not None:
        try:
            value = float(raw_interval)
        except ValueError:
            _warn(
                f"KONGMING_SCHEDULER_INTERVAL={raw_interval!r} is not a valid float; using default."
            )
        else:
            _set_nested(data, ("scheduler", "interval"), value)

    raw_max_inflight = env_map.get("KONGMING_SCHEDULER_MAX_INFLIGHT")
    if raw_max_inflight is not None:
        try:
            value_int = int(raw_max_inflight)
        except ValueError:
            _warn(
                f"KONGMING_SCHEDULER_MAX_INFLIGHT={raw_max_inflight!r} is not a "
                "valid int; using default."
            )
        else:
            _set_nested(data, ("scheduler", "max_inflight"), value_int)

    raw_mode = env_map.get("KONGMING_SCHEDULER_APPROVAL_MODE")
    if raw_mode is not None:
        normalized = raw_mode.strip().lower()
        if normalized in ("fail_closed", "trust"):
            _set_nested(data, ("scheduler", "approval", "mode"), normalized)
        else:
            _warn(
                f"KONGMING_SCHEDULER_APPROVAL_MODE={raw_mode!r} is not a valid mode "
                "(expected 'fail_closed' or 'trust'); using default."
            )

    raw_max_turns = env_map.get("KONGMING_SCHEDULER_DEFAULT_MAX_TURNS")
    if raw_max_turns is not None:
        try:
            value_turns = int(raw_max_turns)
        except ValueError:
            _warn(
                f"KONGMING_SCHEDULER_DEFAULT_MAX_TURNS={raw_max_turns!r} is not a "
                "valid int; using default."
            )
        else:
            if value_turns <= 0:
                _warn(
                    f"KONGMING_SCHEDULER_DEFAULT_MAX_TURNS={raw_max_turns!r} must be > 0; "
                    "using default."
                )
            else:
                _set_nested(data, ("scheduler", "default_max_turns"), value_turns)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并两个 dict，override 值覆盖 base。

    仅处理 dict-in-dict 嵌套；遇到 list / 标量直接替换。
    返回新 dict，不就地修改入参。
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_module_yamls(config_dir: Path, data: dict[str, Any]) -> dict[str, Any]:
    """扫描 config_dir 下的 per-module YAML 文件并合并到 data。

    只加载 :data:`_MODULE_YAML_MAP` 中列出的已知文件；其它文件不碰。
    文件不存在时静默跳过（per-module 文件是可选的）。
    """
    merged = dict(data)
    for filename, top_key in _MODULE_YAML_MAP.items():
        module_path = config_dir / filename
        if not module_path.exists():
            continue
        module_data = _load_yaml(module_path)
        if not module_data:
            continue
        # 模块文件可能顶层就是目标 dict，也可能包了一层与 top_key 同名的 key。
        # 如果顶层有 top_key，取其子 dict；否则整个 module_data 视为目标。
        section = module_data.get(top_key, module_data)
        if not isinstance(section, dict):
            continue
        existing = merged.get(top_key, {})
        if not isinstance(existing, dict):
            existing = {}
        merged[top_key] = _deep_merge(existing, section)
    return merged


def _maybe_load_env_file(config_path: Path | None = None) -> dict[str, str]:
    """尝试加载项目根的 ``.env`` 文件到 ``os.environ``。

    这一层的存在是为了把敏感配置（如 API key）从 YAML 代码库剥离——开发者把
    真实值写进本地 ``.env``（gitignored），运行时由 :mod:`python-dotenv` 注入
    进程环境变量，再走已有的 ``KONGMING_*`` env 覆盖链接入 Config。

    语义：

    - ``.env`` 不存在 → 静默跳过
    - ``python-dotenv`` 未安装 → 静默跳过（不应发生；它是 runtime dep）
    - **不覆盖**已设置的 env 变量（``override=False``）—— 真实 env 优先于 .env，
      让 CI / 容器部署可以用 env 覆盖 .env 而无需删文件

    .env 搜索路径优先从配置文件目录向上查找；未传配置文件时从 cwd 向上查找。
    这样 Web 写回临时 YAML 时也能加载同一套项目级 `.env`。
    """
    if os.environ.get("KONGMING_SKIP_DOTENV", "").lower() in {"1", "true", "yes", "on"}:
        return {}

    config_env_names = _config_env_names()
    before = {name: os.environ.get(name) for name in config_env_names}

    try:
        from dotenv import dotenv_values, load_dotenv
    except ImportError:
        return {}

    start = config_path.parent if config_path is not None else Path.cwd()
    dotenv_path: Path | None = None
    for candidate_dir in (start, *start.parents):
        candidate = candidate_dir / ".env"
        if candidate.exists():
            dotenv_path = candidate
            break

    try:
        if dotenv_path is not None:
            load_dotenv(dotenv_path=dotenv_path, override=False)
            parsed = dotenv_values(dotenv_path)
        else:
            load_dotenv(override=False)
            parsed = {}
    finally:
        for name, old_value in before.items():
            if old_value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old_value

    return {
        key: value
        for key, value in parsed.items()
        if key in config_env_names and isinstance(value, str)
    }


def load_config(
    path: str | Path | None = None,
    *,
    load_env_file: bool = True,
    migrate: bool = True,
) -> Config:
    """加载并校验整体配置。

    Args:
        path: 显式配置文件路径；为 ``None`` 时走 ``KONGMING_CONFIG`` 环境变量，
            再 fallback 到仓库内 ``config/setting.yaml``。
        load_env_file: 是否在加载配置前先把项目根 ``.env`` 注入进程环境变量。
            默认 ``True``——生产体验优先。测试想断言"纯 yaml 默认值"行为时
            显式传 ``False`` 可关闭。
        migrate: 是否在校验前迁移配置文件。正式入口保持默认 ``True``；writer
            保存临时文件时传 ``False``，避免校验步骤改变待写 YAML 的格式范围。

    Returns:
        校验通过的 :class:`Config` 实例。

    Raises:
        ConfigLoadError: 路径不存在、无法读取、YAML 解析失败。
        ConfigValidationError: 字段类型 / 约束 / 跨字段规则不满足。
    """
    resolved = _resolve_config_path(path)
    dotenv_env: dict[str, str] = {}
    if load_env_file:
        dotenv_env = _maybe_load_env_file(resolved) or {}

    if migrate:
        from infrastructure.config.migrations import migrate_config_if_needed

        migrate_config_if_needed(resolved)
    raw_data = _load_yaml(resolved)
    # 加载 per-module YAML 文件（context yaml / tools yaml / llm yaml / infrastructure.tracing yaml）
    config_dir = resolved.parent
    with_modules = _load_module_yamls(config_dir, raw_data)
    merged = _apply_env_overrides(with_modules, env={**dotenv_env, **os.environ})

    try:
        return Config.model_validate(merged)
    except ValidationError as exc:
        raise ConfigValidationError(
            f"config validation failed for {resolved}: {exc}",
            details={"path": str(resolved), "errors": exc.errors()},
        ) from exc


__all__ = ["load_config"]
