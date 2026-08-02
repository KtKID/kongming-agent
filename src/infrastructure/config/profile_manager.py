"""配置 profile 同步管理入口。

本模块服务维护期的配置 profile 同步流程：以 ``config/setting.yaml`` 为主配置，
把 ``config/xspace/setting.yaml`` 视为 XSpace 产品 profile，并用
``sync-policy.yaml`` 记录 sync-copy / xspace-keep / main-only 决策。运行期配置加载仍
只通过 :func:`infrastructure.config.load_config` 读取单个 YAML。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from ruamel.yaml import YAML

from infrastructure.config.loader import load_config
from infrastructure.config.manager import _flatten_dict, _yaml_explicit_paths
from infrastructure.config.writer import PatchItem, round_trip_update

ProfileDecisionAction = Literal["sync-copy", "xspace-keep", "main-only"]

_VALID_ACTIONS: frozenset[str] = frozenset(("sync-copy", "xspace-keep", "main-only"))


@dataclass(frozen=True)
class ProfileDecision:
    """一条 profile 同步决策。"""

    path: str
    action: ProfileDecisionAction
    reason: str
    source_hash: str


@dataclass(frozen=True)
class ProfileReviewIssue:
    """profile review 发现的问题。"""

    path: str
    code: str
    message: str


@dataclass(frozen=True)
class ProfileReview:
    """profile review 汇总结果。"""

    source_path: Path
    target_path: Path
    policy_path: Path
    source_leaf_count: int
    target_leaf_count: int
    decision_count: int
    issues: tuple[ProfileReviewIssue, ...]

    @property
    def ok(self) -> bool:
        """返回 review 是否通过。"""
        return not self.issues


class ConfigProfileManager:
    """配置 profile 同步管理入口。"""

    def __init__(self, source_path: Path, target_path: Path, policy_path: Path) -> None:
        """构造 profile 管理器。

        Args:
            source_path: 主配置 YAML 路径，通常是 ``config/setting.yaml``。
            target_path: profile YAML 路径，通常是 ``config/xspace/setting.yaml``。
            policy_path: profile 决策 YAML 路径。
        """
        self._source_path = source_path
        self._target_path = target_path
        self._policy_path = policy_path

    def review(self) -> ProfileReview:
        """检查主配置、目标 profile 和同步 policy 是否一致。

        Returns:
            :class:`ProfileReview`，包含 leaf 数量和所有待处理问题。
        """
        source_values = _load_config_values(self._source_path)
        target_values = _load_config_values(self._target_path)
        source_paths = _yaml_explicit_paths(self._source_path)
        target_paths = _yaml_explicit_paths(self._target_path)
        decisions, policy_issues = self._load_policy()

        issues: list[ProfileReviewIssue] = list(policy_issues)

        source_leaf_paths = set(source_values)
        target_leaf_paths = set(target_values)

        for path in sorted(source_leaf_paths - source_paths):
            issues.append(
                ProfileReviewIssue(
                    path=path,
                    code="source-missing",
                    message="主配置缺少显式 leaf 字段",
                )
            )

        for path in sorted(target_paths - source_leaf_paths):
            issues.append(
                ProfileReviewIssue(
                    path=path,
                    code="target-unknown",
                    message="XSpace profile 显式字段不在 Config leaf 中",
                )
            )

        for path in sorted(source_leaf_paths - target_leaf_paths):
            issues.append(
                ProfileReviewIssue(
                    path=path,
                    code="target-model-missing",
                    message="XSpace profile 加载结果缺少 Config leaf",
                )
            )

        for path, decision in sorted(decisions.items()):
            expected_hash = source_hash(path, source_values.get(path))
            if path not in source_leaf_paths:
                issues.append(
                    ProfileReviewIssue(
                        path=path,
                        code="decision-unknown-path",
                        message="policy 决策路径不在 Config leaf 中",
                    )
                )
                continue
            if path not in source_paths:
                issues.append(
                    ProfileReviewIssue(
                        path=path,
                        code="decision-source-missing",
                        message="policy 决策路径未在主配置显式声明",
                    )
                )
            if decision.source_hash != expected_hash:
                issues.append(
                    ProfileReviewIssue(
                        path=path,
                        code="decision-stale",
                        message=f"policy source_hash 过期，应为 {expected_hash}",
                    )
                )

        for path in sorted(source_leaf_paths):
            if path not in source_paths:
                continue

            path_decision = decisions.get(path)
            target_has_path = path in target_paths
            source_value = source_values.get(path)
            target_value = target_values.get(path)

            if not target_has_path:
                if path_decision is None:
                    issues.append(
                        ProfileReviewIssue(
                            path=path,
                            code="target-missing-decision-required",
                            message="XSpace profile 缺少字段，需要 main-only 决策",
                        )
                    )
                elif path_decision.action != "main-only":
                    issues.append(
                        ProfileReviewIssue(
                            path=path,
                            code="target-missing-wrong-decision",
                            message="XSpace profile 缺少字段时只能使用 main-only 决策",
                        )
                    )
                continue

            if path_decision is not None and path_decision.action == "main-only":
                issues.append(
                    ProfileReviewIssue(
                        path=path,
                        code="main-only-target-present",
                        message="main-only 决策对应的字段仍在 XSpace profile 中显式存在",
                    )
                )
                continue

            if source_value != target_value:
                if path_decision is None:
                    issues.append(
                        ProfileReviewIssue(
                            path=path,
                            code="xspace-keep-decision-required",
                            message="XSpace profile 值与主配置不同，需要 xspace-keep 决策",
                        )
                    )
                elif path_decision.action != "xspace-keep":
                    issues.append(
                        ProfileReviewIssue(
                            path=path,
                            code="xspace-keep-wrong-decision",
                            message="XSpace profile 值与主配置不同，只能使用 xspace-keep 决策",
                        )
                    )
                continue

            if path_decision is not None and path_decision.action == "xspace-keep":
                issues.append(
                    ProfileReviewIssue(
                        path=path,
                        code="xspace-keep-without-diff",
                        message="xspace-keep 决策对应字段当前值与主配置一致",
                    )
                )

        return ProfileReview(
            source_path=self._source_path,
            target_path=self._target_path,
            policy_path=self._policy_path,
            source_leaf_count=len(source_leaf_paths),
            target_leaf_count=len(target_leaf_paths),
            decision_count=len(decisions),
            issues=tuple(issues),
        )

    def write_decision(self, path: str, action: ProfileDecisionAction, reason: str) -> None:
        """写入或更新一个 policy 决策。

        Args:
            path: Config leaf dot-path。
            action: 同步动作。
            reason: 决策原因。
        """
        if action not in _VALID_ACTIONS:
            raise ValueError(f"unsupported profile decision action: {action}")
        normalized_reason = _normalize_reason(reason)
        source_values = _load_config_values(self._source_path)
        if path not in source_values:
            raise ValueError(f"profile decision path is not a Config leaf: {path}")
        _write_policy(
            self._policy_path,
            source=self._source_path,
            target=self._target_path,
            replacement=ProfileDecision(
                path=path,
                action=action,
                reason=normalized_reason,
                source_hash=source_hash(path, source_values[path]),
            ),
        )

    def sync_copy(self, path: str, reason: str = "XSpace profile 继承主配置值") -> None:
        """把主配置字段复制到 XSpace profile，并记录 sync-copy 决策。

        Args:
            path: Config leaf dot-path。
            reason: 写入 policy 的决策原因。
        """
        source_values = _load_config_values(self._source_path)
        normalized_reason = _normalize_reason(reason)
        if path not in source_values:
            raise ValueError(f"sync path is not a Config leaf: {path}")

        mtime = self._target_path.stat().st_mtime
        round_trip_update(
            self._target_path,
            [PatchItem(path=path, value=source_values[path])],
            mtime,
            create_missing_paths={path},
            load_env_file=False,
        )
        self.write_decision(path, "sync-copy", normalized_reason)

    def _load_policy(self) -> tuple[dict[str, ProfileDecision], tuple[ProfileReviewIssue, ...]]:
        """读取 policy，返回 path 到决策的映射和结构问题。"""
        if not self._policy_path.exists():
            return {}, ()

        yaml = YAML(typ="rt")
        with self._policy_path.open("r", encoding="utf-8") as f:
            doc = yaml.load(f)
        if doc is None:
            return {}, ()
        if not isinstance(doc, dict):
            return {}, (
                ProfileReviewIssue(
                    path="<root>",
                    code="policy-root-invalid",
                    message="sync policy 根节点必须是 mapping",
                ),
            )

        issues: list[ProfileReviewIssue] = []
        decisions: dict[str, ProfileDecision] = {}
        raw_decisions = doc.get("decisions", [])
        if raw_decisions is None:
            raw_decisions = []
        if not isinstance(raw_decisions, list):
            return {}, (
                ProfileReviewIssue(
                    path="decisions",
                    code="policy-decisions-invalid",
                    message="sync policy decisions 必须是列表",
                ),
            )

        for idx, item in enumerate(raw_decisions):
            issue_path = f"decisions.{idx}"
            if not isinstance(item, dict):
                issues.append(
                    ProfileReviewIssue(
                        path=issue_path,
                        code="policy-decision-invalid",
                        message="单条 decision 必须是 mapping",
                    )
                )
                continue
            raw_path = item.get("path")
            raw_action = item.get("action")
            raw_reason = item.get("reason")
            raw_hash = item.get("source_hash")
            if (
                not isinstance(raw_path, str)
                or not raw_path
                or not isinstance(raw_action, str)
                or not raw_action
                or not isinstance(raw_reason, str)
                or not raw_reason
                or not isinstance(raw_hash, str)
                or not raw_hash
            ):
                issues.append(
                    ProfileReviewIssue(
                        path=issue_path,
                        code="policy-decision-fields-invalid",
                        message="decision 必须包含非空 path/action/reason/source_hash",
                    )
                )
                continue
            if raw_action not in _VALID_ACTIONS:
                issues.append(
                    ProfileReviewIssue(
                        path=str(raw_path),
                        code="policy-action-invalid",
                        message=f"不支持的 decision action: {raw_action}",
                    )
                )
                continue
            typed_action = cast(ProfileDecisionAction, raw_action)
            if raw_path in decisions:
                issues.append(
                    ProfileReviewIssue(
                        path=str(raw_path),
                        code="policy-decision-duplicate",
                        message="同一 path 只能有一条 decision",
                    )
                )
                continue
            decisions[raw_path] = ProfileDecision(
                path=raw_path,
                action=typed_action,
                reason=raw_reason,
                source_hash=raw_hash,
            )

        return decisions, tuple(issues)


def source_hash(path: str, value: Any) -> str:
    """计算字段级 source hash。

    Args:
        path: Config leaf dot-path。
        value: 主配置字段的规范化值。

    Returns:
        ``sha256:<16 hex>`` 形式的短 hash。
    """
    payload = {
        "path": path,
        "type": _value_type(value),
        "value": value,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _load_config_values(path: Path) -> dict[str, Any]:
    """加载配置并扁平化为 dot-path 到 JSON 值的映射。"""
    cfg = load_config(path, load_env_file=False)
    dumped = cfg.model_dump(mode="json")
    return dict(_flatten_dict(dumped))


def _normalize_reason(reason: str) -> str:
    """校验并规范化 policy reason，返回去首尾空白后的文本。"""
    normalized = reason.strip()
    if not normalized:
        raise ValueError("profile decision reason must not be empty")
    return normalized


def _value_type(value: Any) -> str:
    """返回稳定的 JSON 值类型描述，用于 hash 区分同值不同类型。"""
    if isinstance(value, dict):
        inner = ",".join(f"{key}:{_value_type(value[key])}" for key in sorted(value))
        return f"dict[{inner}]"
    if isinstance(value, list):
        inner = ",".join(_value_type(item) for item in value)
        return f"list[{inner}]"
    return type(value).__name__


def _write_policy(
    policy_path: Path,
    *,
    source: Path,
    target: Path,
    replacement: ProfileDecision,
) -> None:
    """写入或替换 policy 中的一条 decision。"""
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    if policy_path.exists():
        with policy_path.open("r", encoding="utf-8") as f:
            doc = yaml.load(f)
    else:
        doc = None
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise ValueError(f"sync policy root must be a mapping: {policy_path}")

    doc["version"] = int(doc.get("version") or 1)
    doc["source"] = str(doc.get("source") or _policy_display_path(policy_path, source))
    doc["target"] = str(doc.get("target") or _policy_display_path(policy_path, target))
    decisions = doc.get("decisions")
    if decisions is None:
        decisions = []
        doc["decisions"] = decisions
    if not isinstance(decisions, list):
        raise ValueError("sync policy decisions must be a list")

    replacement_data = {
        "path": replacement.path,
        "action": replacement.action,
        "reason": replacement.reason,
        "source_hash": replacement.source_hash,
    }
    for idx, item in enumerate(decisions):
        if isinstance(item, dict) and item.get("path") == replacement.path:
            decisions[idx] = replacement_data
            break
    else:
        decisions.append(replacement_data)
        decisions.sort(key=lambda item: str(item.get("path", "")) if isinstance(item, dict) else "")

    policy_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = policy_path.with_name(f"{policy_path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            yaml.dump(doc, f)
        tmp_path.replace(policy_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def format_review_issues(issues: Iterable[ProfileReviewIssue]) -> str:
    """把 review issue 列表格式化为 CLI 文本。"""
    return "\n".join(f"- {issue.path}: {issue.code} — {issue.message}" for issue in issues)


def _policy_display_path(policy_path: Path, path: Path) -> str:
    """把 policy 中的路径尽量写成仓库相对路径。"""
    resolved = path.resolve()
    for root in (policy_path.resolve().parents[2], Path.cwd().resolve()):
        try:
            return str(resolved.relative_to(root))
        except ValueError:
            continue
    return str(path)


__all__ = [
    "ConfigProfileManager",
    "ProfileDecision",
    "ProfileDecisionAction",
    "ProfileReview",
    "ProfileReviewIssue",
    "format_review_issues",
    "source_hash",
]
