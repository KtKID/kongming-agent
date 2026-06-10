"""roundtable_review 编排合同与解析。

本脚本定义 Multi-Agent Roundtable Review 的 payload、默认 reviewer、预算和
ReviewBoard 记录结构。
作用是让策略入口获得类型化配置，并在运行前完成输入范围、讨论轮次和预算校验。
关键执行流程：parse_roundtable_review_spec 读取 payload，补默认 reviewer 和 limits，
校验路径、轮次、预算后返回 RoundtableReviewSpec。
关键函数：parse_roundtable_review_spec 解析入口，default_reviewer_specs 提供五类 reviewer，
estimate_tokens 估算文本 token 用量。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

ReviewSeverity = Literal["P0", "P1", "P2", "P3"]
ReviewCommentType = Literal["support", "refute", "supplement"]

_DEFAULT_TOTAL_CHILD_TOKEN_BUDGET = 50_000
_DEFAULT_MAX_DISCUSSION_ROUNDS = 6
_DEFAULT_DISCUSSION_ROUNDS = 2
_DEFAULT_MAX_CONCURRENCY = 5
_DEFAULT_AGENT_TIMEOUT_SECONDS = 600
_DEFAULT_REVIEWER_MAX_TURNS = 3
_DEFAULT_ARBITER_MAX_TURNS = 3
_DEFAULT_MAX_FILES = 80
_DEFAULT_MAX_BYTES_PER_FILE = 80_000
_SCOPED_WORKDIR_MODE = "scoped_workdir"
_SCOPED_TOOL_NAMES = ("read_file", "list_dir")
_SEVERITIES = frozenset({"P0", "P1", "P2", "P3"})
_COMMENT_TYPES = frozenset({"support", "refute", "supplement"})


@dataclass(frozen=True)
class ReviewerSpec:
    # reviewer 稳定 ID。
    agent_id: str
    # 展示名称。
    title: str
    # 关注面。
    focus: str
    # reviewer 提示中的职责说明。
    instructions: str


@dataclass(frozen=True)
class ReviewInputSource:
    # 输入根目录。
    root_dir: str
    # 显式模块、目录或文件路径。
    paths: tuple[str, ...]
    # include glob 列表。
    include: tuple[str, ...]
    # exclude glob 列表。
    exclude: tuple[str, ...]
    # 最大收集文件数。
    max_files: int
    # 单文件最大读取字节数。
    max_bytes_per_file: int


@dataclass(frozen=True)
class RoundtableReviewLimits:
    # 全部子 agent 共享的输出 token 估算预算。
    total_child_token_budget: int
    # 总讨论轮次，包含第 1 轮独立分析。
    discussion_rounds: int
    # 轮次硬上限。
    max_discussion_rounds: int
    # 子 agent 并发数。
    max_concurrency: int
    # 单个 reviewer 最大 turn 数。
    reviewer_max_turns: int
    # arbiter 最大 turn 数。
    arbiter_max_turns: int
    # 单个子 agent 超时秒数。
    agent_timeout_seconds: int


@dataclass(frozen=True)
class RoundtableReviewSpec:
    # workflow 模式。
    mode: Literal["roundtable_review"]
    # 本次评审主题。
    topic: str
    # 评审目标问题。
    objective: str
    # 输入来源。
    input_source: ReviewInputSource
    # reviewer 列表。
    reviewers: tuple[ReviewerSpec, ...]
    # 预算与轮次。
    limits: RoundtableReviewLimits
    # 审计标签。
    audit_tags: tuple[str, ...]


@dataclass(frozen=True)
class SourceFileRecord:
    # 原始相对路径。
    path: str
    # 复制到子 agent workdir 后的相对路径。
    materialized_path: str
    # 文件大小。
    size_bytes: int
    # 是否截断。
    truncated: bool


@dataclass(frozen=True)
class ReviewClaimRecord:
    # claim 稳定 ID。
    claim_id: str
    # 来源 reviewer。
    agent: str
    # 严重等级。
    severity: str
    # 主张。
    claim: str
    # 证据列表。
    evidence: tuple[Any, ...]
    # 风险。
    risk: str
    # 建议。
    suggestion: str
    # 置信度。
    confidence: float
    # 原始 finding。
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class ReviewCommentRecord:
    # comment 稳定 ID。
    comment_id: str
    # 来源 reviewer。
    agent: str
    # 轮次，独立分析为 1，交叉质询从 2 开始。
    round_index: int
    # support/refute/supplement。
    comment_type: str
    # 目标 claim。
    target_claim_id: str
    # 评论内容。
    comment: str
    # 证据列表。
    evidence: tuple[Any, ...]
    # 严重等级调整建议。
    severity_adjustment: str | None
    # 置信度。
    confidence: float
    # 原始 comment。
    raw: Mapping[str, Any]


class RoundtableReviewContractError(ValueError):
    """roundtable_review payload 解析失败。"""


def default_reviewer_specs() -> tuple[ReviewerSpec, ...]:
    """返回默认五类 reviewer，输入为空，输出固定角色列表。"""
    return (
        ReviewerSpec(
            agent_id="architecture_reviewer",
            title="架构 Agent",
            focus="模块边界、职责划分、依赖方向、扩展性",
            instructions="从模块边界、公开门户、依赖方向、扩展点和演进成本审查设计。",
        ),
        ReviewerSpec(
            agent_id="code_quality_reviewer",
            title="代码质量 Agent",
            focus="命名、抽象层次、复杂度、可读性",
            instructions="从命名、一致性、抽象层级、复杂度、可读性和维护成本审查实现。",
        ),
        ReviewerSpec(
            agent_id="test_reviewer",
            title="测试 Agent",
            focus="可测试性、边界条件、回归风险",
            instructions="从测试入口、边界条件、回归风险、可观测断言和缺失用例审查方案。",
        ),
        ReviewerSpec(
            agent_id="performance_reviewer",
            title="性能 Agent",
            focus="热路径、IO、缓存、并发、资源占用",
            instructions="从热路径、IO 次数、缓存、并发、资源占用和规模上限审查风险。",
        ),
        ReviewerSpec(
            agent_id="safety_stability_reviewer",
            title="安全/稳定性 Agent",
            focus="权限、异常处理、数据一致性、失败恢复",
            instructions="从权限边界、异常处理、数据一致性、失败恢复和误用防护审查风险。",
        ),
    )


def parse_roundtable_review_spec(raw: Mapping[str, object]) -> RoundtableReviewSpec:
    """解析 roundtable_review payload，输入为原始映射，输出为类型化 spec。"""
    topic = _required_str(raw, "topic")
    objective = _optional_str(raw.get("objective")) or topic
    input_source = _parse_input_source(raw.get("input_source"), raw.get("module_path"))
    reviewers = _parse_reviewers(raw.get("reviewers"))
    limits = _parse_limits(raw.get("limits"), input_source=input_source)
    audit_tags = tuple(_string_list(raw.get("audit_tags")))
    return RoundtableReviewSpec(
        mode="roundtable_review",
        topic=topic,
        objective=objective,
        input_source=input_source,
        reviewers=reviewers,
        limits=limits,
        audit_tags=audit_tags,
    )


def estimate_tokens(text: str) -> int:
    """估算文本 token，输入为字符串，输出为保守整数。"""
    return max(1, (len(text) + 3) // 4)


def normalize_severity(value: Any) -> str:
    """规范化严重等级，输入为任意值，输出为 P0-P3。"""
    if isinstance(value, str) and value.strip().upper() in _SEVERITIES:
        return value.strip().upper()
    return "P2"


def normalize_comment_type(value: Any) -> str:
    """规范化评论类型，输入为任意值，输出为 support/refute/supplement。"""
    if isinstance(value, str) and value.strip().lower() in _COMMENT_TYPES:
        return value.strip().lower()
    return "supplement"


def _parse_input_source(raw: object, module_path: object) -> ReviewInputSource:
    """解析输入来源，输入为 input_source 或 module_path，输出为 ReviewInputSource。"""
    source = dict(raw) if isinstance(raw, dict) else {}
    paths = _string_list(source.get("paths"))
    if module_path is not None:
        module_paths = _string_list(module_path)
        paths = [*module_paths, *paths]
    include = _string_list(source.get("include"))
    if not paths and not include:
        raise RoundtableReviewContractError(
            "roundtable_review requires input_source.paths, input_source.include, or module_path"
        )
    max_files = _positive_int(source.get("max_files"), _DEFAULT_MAX_FILES)
    max_bytes = _positive_int(source.get("max_bytes_per_file"), _DEFAULT_MAX_BYTES_PER_FILE)
    return ReviewInputSource(
        root_dir=_optional_str(source.get("root_dir")) or ".",
        paths=tuple(paths),
        include=tuple(include),
        exclude=tuple(_string_list(source.get("exclude"))),
        max_files=max_files,
        max_bytes_per_file=max_bytes,
    )


def _parse_reviewers(raw: object) -> tuple[ReviewerSpec, ...]:
    """解析 reviewer 列表，输入为可选 payload 字段，输出为 reviewer specs。"""
    defaults = {reviewer.agent_id: reviewer for reviewer in default_reviewer_specs()}
    if raw is None:
        return default_reviewer_specs()
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        raise RoundtableReviewContractError("reviewers must be an array")
    reviewers: list[ReviewerSpec] = []
    for index, item in enumerate(raw, 1):
        if isinstance(item, str):
            reviewer = defaults.get(item.strip())
            if reviewer is None:
                raise RoundtableReviewContractError(f"unknown reviewer id: {item}")
            reviewers.append(reviewer)
            continue
        if not isinstance(item, Mapping):
            raise RoundtableReviewContractError(f"reviewers[{index}] must be object or string")
        agent_id = _required_str(item, "agent_id")
        reviewers.append(
            ReviewerSpec(
                agent_id=agent_id,
                title=_optional_str(item.get("title")) or agent_id,
                focus=_optional_str(item.get("focus")) or "代码模块设计评审",
                instructions=_optional_str(item.get("instructions"))
                or "从指定关注点审查代码模块设计。",
            )
        )
    if not reviewers:
        raise RoundtableReviewContractError("reviewers must contain at least one reviewer")
    return tuple(reviewers)


def _parse_limits(
    raw: object,
    *,
    input_source: ReviewInputSource,
) -> RoundtableReviewLimits:
    """解析预算限制，输入为 limits 字段，输出为 RoundtableReviewLimits。"""
    limits = dict(raw) if isinstance(raw, dict) else {}
    max_rounds = _positive_int(limits.get("max_discussion_rounds"), _DEFAULT_MAX_DISCUSSION_ROUNDS)
    if max_rounds > _DEFAULT_MAX_DISCUSSION_ROUNDS:
        max_rounds = _DEFAULT_MAX_DISCUSSION_ROUNDS
    requested_rounds = limits.get("discussion_rounds")
    if requested_rounds is None:
        discussion_rounds = _default_discussion_rounds(input_source)
    else:
        discussion_rounds = _positive_int(requested_rounds, _DEFAULT_DISCUSSION_ROUNDS)
    discussion_rounds = max(1, min(discussion_rounds, max_rounds))
    total_budget = _positive_int(
        limits.get("total_child_token_budget"), _DEFAULT_TOTAL_CHILD_TOKEN_BUDGET
    )
    return RoundtableReviewLimits(
        total_child_token_budget=total_budget,
        discussion_rounds=discussion_rounds,
        max_discussion_rounds=max_rounds,
        max_concurrency=_positive_int(limits.get("max_concurrency"), _DEFAULT_MAX_CONCURRENCY),
        reviewer_max_turns=_positive_int(
            limits.get("reviewer_max_turns"), _DEFAULT_REVIEWER_MAX_TURNS
        ),
        arbiter_max_turns=_positive_int(
            limits.get("arbiter_max_turns"), _DEFAULT_ARBITER_MAX_TURNS
        ),
        agent_timeout_seconds=_positive_int(
            limits.get("agent_timeout_seconds"), _DEFAULT_AGENT_TIMEOUT_SECONDS
        ),
    )


def _default_discussion_rounds(input_source: ReviewInputSource) -> int:
    """按问题规模选择默认轮次，输入为输入范围，输出为 2-4 的轮次数。"""
    scope_size = len(input_source.paths) + len(input_source.include)
    if scope_size >= 8 or input_source.max_files >= 60:
        return 4
    if scope_size >= 3 or input_source.max_files >= 20:
        return 3
    return _DEFAULT_DISCUSSION_ROUNDS


def _required_str(raw: Mapping[str, object], key: str) -> str:
    """读取必填字符串，输入为映射和字段名，输出为去空白字符串。"""
    value = raw.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise RoundtableReviewContractError(f"{key} must be a non-empty string")


def _optional_str(value: object) -> str | None:
    """读取可选字符串，输入为任意值，输出为字符串或 None。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _string_list(value: object) -> list[str]:
    """归一化字符串列表，输入为字符串、数组或空值，输出为字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Sequence):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _positive_int(value: object, default: int) -> int:
    """读取正整数，输入为任意值和默认值，输出为正整数。"""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        if parsed > 0:
            return parsed
    return default


__all__ = [
    "ReviewClaimRecord",
    "ReviewCommentRecord",
    "ReviewInputSource",
    "ReviewerSpec",
    "RoundtableReviewContractError",
    "RoundtableReviewLimits",
    "RoundtableReviewSpec",
    "SourceFileRecord",
    "default_reviewer_specs",
    "estimate_tokens",
    "normalize_comment_type",
    "normalize_severity",
    "parse_roundtable_review_spec",
]
