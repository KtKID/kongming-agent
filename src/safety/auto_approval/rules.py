"""规则数据结构 + yaml 加载 + 物化到用户可编辑位置。

本模块做三件事：

1. 定义 ``MatchSpec`` 联合体 + ``RuleDefinition`` dataclass（无业务逻辑）
2. 从 yaml 加载 ``RuleSet``
3. **物化内置 yaml 到用户可编辑位置**（启动时一次；用户编辑物化后的版本）

不依赖 matchers / policy / config_store / audit。是 ``policy.py`` 的数据基础。

新增规则的两种方式：
- 改内置 ``default_rules.yaml`` → 升级会被覆盖（仅维护者用）
- 改物化后的 ``<home>/web/auto_approval/rules.yaml`` → 永久持有（推荐）

matcher kind 扩展需在 ``matchers.py`` 对应增加分派分支。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

# ---------------------------------------------------------------------------
# MatchSpec 联合体（discriminated by `kind`）
# ---------------------------------------------------------------------------
#
# 注意：使用 Python 3.10+ Union 语法。MatchSpec 实例由 yaml 解析后是 dict；
# matchers.py 通过 dict["kind"] 分派调用具体 matcher 函数。
# 这里只是文档化形状，不做 runtime 验证（runtime 验证由 matcher 自己负责）。
#
# 七种 MatchSpec 形状：
#   {kind: bash_cmd_first,         command: str}
#   {kind: bash_cmd_first_prefix,  prefix: str}
#   {kind: bash_cmd_first_in,      commands: list[str]}
#   {kind: bash_cmd_pattern,       pattern: str}        # 正则
#   {kind: edit_path_glob,         globs: list[str]}
#   {kind: edit_path_pattern,      pattern: str}        # 正则
#   {kind: mcp_not_in_allowlist,   allowlist: list[str]}

MatchKind = Literal[
    "bash_cmd_first",
    "bash_cmd_first_prefix",
    "bash_cmd_first_in",
    "bash_cmd_pattern",
    "edit_path_glob",
    "edit_path_pattern",
    "mcp_not_in_allowlist",
]

# 集中维护 supported kinds，matchers.py 注册时用同一份
SUPPORTED_MATCH_KINDS: frozenset[str] = frozenset(
    [
        "bash_cmd_first",
        "bash_cmd_first_prefix",
        "bash_cmd_first_in",
        "bash_cmd_pattern",
        "edit_path_glob",
        "edit_path_pattern",
        "mcp_not_in_allowlist",
    ],
)


@dataclass(frozen=True, slots=True)
class RuleDefinition:
    """单条规则定义。

    ``enabled`` 字段是「默认值」——per-cwd 配置可以覆盖。policy.py 在
    classify 时会用 ``ConfigStore`` 的 ``rule_overrides`` 覆盖此默认值。
    """

    id: str
    description: str
    match: dict[str, Any]
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("RuleDefinition.id must be non-empty")
        kind = self.match.get("kind") if isinstance(self.match, dict) else None
        if kind not in SUPPORTED_MATCH_KINDS:
            raise ValueError(
                f"RuleDefinition[{self.id}] unsupported match.kind: {kind!r}; "
                f"supported = {sorted(SUPPORTED_MATCH_KINDS)}",
            )


@dataclass(frozen=True, slots=True)
class RuleSet:
    """规则集 + 全局默认配置。"""

    version: int
    default_timeout_ms: int
    rules: tuple[RuleDefinition, ...] = field(default_factory=tuple)

    def find(self, rule_id: str) -> RuleDefinition | None:
        for r in self.rules:
            if r.id == rule_id:
                return r
        return None


# ---------------------------------------------------------------------------
# yaml 加载
# ---------------------------------------------------------------------------


_DEFAULT_YAML = Path(__file__).resolve().parent / "default_rules.yaml"


def load_default_rules(yaml_path: Path | None = None) -> RuleSet:
    """加载内置 default_rules.yaml。

    Args:
        yaml_path: 测试可覆盖路径；默认 ``default_rules.yaml``。

    Returns:
        ``RuleSet``，含全部规则定义。

    Raises:
        FileNotFoundError: yaml 文件不存在
        ValueError: yaml 结构不合法（缺字段 / kind 非法 / id 重复）
        yaml.YAMLError: yaml 语法错误
    """
    path = yaml_path or _DEFAULT_YAML
    if not path.exists():
        raise FileNotFoundError(f"default_rules.yaml not found: {path}")

    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"default_rules.yaml root must be a mapping, got {type(raw)!r}")

    version = raw.get("version")
    if not isinstance(version, int):
        raise ValueError("default_rules.yaml: 'version' must be int")

    default_timeout_ms = raw.get("default_timeout_ms")
    if not isinstance(default_timeout_ms, int) or default_timeout_ms <= 0:
        raise ValueError("default_rules.yaml: 'default_timeout_ms' must be positive int")

    rules_raw = raw.get("rules")
    if not isinstance(rules_raw, list):
        raise ValueError("default_rules.yaml: 'rules' must be a list")

    seen_ids: set[str] = set()
    rules: list[RuleDefinition] = []
    for i, item in enumerate(rules_raw):
        if not isinstance(item, dict):
            raise ValueError(f"default_rules.yaml: rules[{i}] must be a mapping")
        rule_id = item.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise ValueError(f"default_rules.yaml: rules[{i}] missing 'id'")
        if rule_id in seen_ids:
            raise ValueError(f"default_rules.yaml: duplicate rule id {rule_id!r}")
        seen_ids.add(rule_id)

        description = item.get("description", "")
        enabled = item.get("enabled", True)
        match = item.get("match")
        if not isinstance(match, dict):
            raise ValueError(
                f"default_rules.yaml: rules[{i}] ({rule_id}) missing 'match' dict",
            )

        rules.append(
            RuleDefinition(
                id=rule_id,
                description=str(description),
                match=match,
                enabled=bool(enabled),
            ),
        )

    return RuleSet(
        version=version,
        default_timeout_ms=default_timeout_ms,
        rules=tuple(rules),
    )


def materialize_user_rules_yaml(home: Path) -> Path:
    """把内置 default_rules.yaml 物化到用户可编辑位置（首次启动时复制）。

    Args:
        home: kongming home，一般由 ``get_kongming_home()`` 产出。

    Returns:
        物化后的 yaml 绝对路径。用户应编辑这份。

    行为：
    - 目标路径 = ``<home>/web/auto_approval/rules.yaml``
    - 若目标已存在 → 不覆盖（保留用户改动），直接返回路径
    - 若不存在 → 从内置 ``default_rules.yaml`` 复制（保留所有注释）

    后续 ``load_default_rules(yaml_path=...)`` 用这份路径加载。
    """
    target_dir = Path(home) / "web" / "auto_approval"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "rules.yaml"
    if not target.exists():
        shutil.copy2(_DEFAULT_YAML, target)
    return target


__all__ = [
    "SUPPORTED_MATCH_KINDS",
    "MatchKind",
    "RuleDefinition",
    "RuleSet",
    "load_default_rules",
    "materialize_user_rules_yaml",
]
