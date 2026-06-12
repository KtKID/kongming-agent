"""Contracts and validation for agent workflow map-reduce mode."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

InputSourceKind = Literal["path_glob", "file_list"]
ShardStrategyKind = Literal["by_directory", "by_file_count"]
OutputContract = Literal["code_findings", "raw_text"]
MapperStatus = Literal["completed", "partial", "failed"]
FindingCategory = Literal[
    "bug",
    "architecture",
    "security",
    "test_gap",
    "performance",
    "maintainability",
]
FindingSeverity = Literal["P0", "P1", "P2", "P3"]
ReducerKind = Literal["deterministic"]
ReducerDedupeStrategy = Literal["exact_dedupe_key", "file_line_title"]
ReducerRankingStrategy = Literal["severity_first", "confidence_first", "impact_first"]
FailedShardStage = Literal["mapper", "validation", "reducer"]

_INPUT_SOURCE_KINDS = frozenset({"path_glob", "file_list"})
_SHARD_STRATEGY_KINDS = frozenset({"by_directory", "by_file_count"})
_OUTPUT_CONTRACTS = frozenset({"code_findings", "raw_text"})
_MAPPER_STATUSES = frozenset({"completed", "partial", "failed"})
_FINDING_CATEGORIES = frozenset(
    {"bug", "architecture", "security", "test_gap", "performance", "maintainability"}
)
_FINDING_SEVERITIES = frozenset({"P0", "P1", "P2", "P3"})
_REDUCER_KINDS = frozenset({"deterministic"})
_REDUCER_DEDUPE_STRATEGIES = frozenset({"exact_dedupe_key", "file_line_title"})
_REDUCER_RANKING_STRATEGIES = frozenset({"severity_first", "confidence_first", "impact_first"})
_FAILED_SHARD_STAGES = frozenset({"mapper", "validation", "reducer"})
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[a-zA-Z]:[\\/]")


@dataclass(frozen=True)
class MapperValidationError:
    # 稳定错误类型，便于测试、审计和 reducer 后续分流。
    error_type: str
    # 出错字段路径，使用 JSON path 风格。
    field_path: str
    # 人类可读错误信息。
    message: str
    # 期望值说明，未知时为 None。
    expected: str | None = None
    # 实际值说明，未知时为 None。
    actual: str | None = None
    # 是否建议补跑该 shard。
    retryable: bool = True

    @property
    def code(self) -> str:
        # 兼容通用 contract parser 对错误码的命名。
        return self.error_type

    @property
    def path(self) -> str:
        # 兼容通用 contract parser 对字段路径的命名。
        return self.field_path


ContractValidationError = MapperValidationError


class MapReduceContractError(ValueError):
    """Raised when a map-reduce payload cannot be parsed into runtime contracts."""

    def __init__(self, errors: Sequence[ContractValidationError]) -> None:
        self.errors = tuple(errors)
        message = "; ".join(f"{err.path}: {err.message}" for err in self.errors)
        super().__init__(message or "map-reduce contract validation failed")


@dataclass(frozen=True)
class MapReduceInputSource:
    # 输入类型，v0.1 支持 path_glob、file_list。
    kind: InputSourceKind
    # 仓库根目录，所有相对路径基于该目录解析。
    root_dir: str
    # include glob 列表，用于收集候选文件。
    include: tuple[str, ...]
    # exclude glob 列表，用于排除构建产物、缓存和第三方目录。
    exclude: tuple[str, ...]
    # 显式文件列表，kind=file_list 时使用。
    files: tuple[str, ...]
    # 索引提供者名称，例如 rg、xcodeatlas、codedb；v0.1 默认 rg / pathlib。
    index_provider: str | None
    # 输入快照摘要，用于复跑和审计比对。
    input_digest: str | None


@dataclass(frozen=True)
class ShardStrategy:
    # 分片策略名称，v0.1 支持 by_directory、by_file_count。
    kind: ShardStrategyKind
    # 每个 shard 的最大文件数。
    max_files_per_shard: int
    # 每个 shard 的估算 token 上限。
    max_estimated_tokens_per_shard: int
    # 最小 shard 数，用于保证并发利用率。
    min_shards: int
    # 最大 shard 数，用于控制成本。
    max_shards: int
    # 是否保持目录边界，避免同一目录被拆到多个 shard。
    preserve_directory_boundary: bool
    # 是否使用依赖图把强相关文件放进同一 shard；v0.1 固定为 False。
    prefer_dependency_cohesion: bool


@dataclass(frozen=True)
class MapperSpec:
    # mapper 名称前缀，用于子 agent name 和 report 展示。
    name_prefix: str
    # mapper 系统提示模板 ID。
    prompt_template: str
    # mapper 可见工具白名单，v0.1 默认 read_file、list_dir、write_file。
    tool_names: tuple[str, ...]
    # mapper 可见 skill 白名单。
    skill_names: tuple[str, ...]
    # mapper 权限模式，v0.1 固定 scoped_workdir，通过输入物化读取 shard 内容。
    permission_mode: Literal["scoped_workdir"]
    # mapper 单 shard 最大 turn 数。
    max_turns: int
    # mapper 输出最大字符数。
    max_output_chars: int


@dataclass(frozen=True)
class ReducerSpec:
    # reducer 类型，v0.1 使用 deterministic。
    kind: ReducerKind
    # 去重策略，v0.1 支持 exact_dedupe_key、file_line_title。
    dedupe_strategy: ReducerDedupeStrategy
    # 排序策略，支持 severity_first、confidence_first、impact_first。
    ranking_strategy: ReducerRankingStrategy
    # 最终报告保留的 finding 数量上限。
    max_findings: int
    # 是否把失败 shard 放入最终报告。
    include_failed_shards: bool
    # reducer agent 使用的 prompt 模板 ID；v0.1 deterministic 时应为 None。
    reducer_prompt_template: str | None


@dataclass(frozen=True)
class MapReduceLimits:
    # mapper 最大并发数。
    max_concurrency: int
    # workflow 总超时时间秒数。
    workflow_timeout_seconds: int
    # 单个 mapper 超时时间秒数。
    mapper_timeout_seconds: int
    # reducer 超时时间秒数。
    reducer_timeout_seconds: int
    # mapper 重试次数。
    mapper_retries: int
    # mapper 输出校验失败后的修复重试次数。
    validation_repair_retries: int


@dataclass(frozen=True)
class MapReduceWorkflowSpec:
    # workflow 模式，固定为 map_reduce。
    mode: Literal["map_reduce"]
    # 高层目标，用于 planner、mapper prompt 和 reducer 报告标题。
    objective: str
    # 输入来源，描述分析对象从哪里来。
    input_source: MapReduceInputSource
    # 分片策略，描述如何把输入拆成同构 shard。
    shard_strategy: ShardStrategy
    # 输出契约，v0.1 支持 code_findings 和 raw_text。
    output_contract: OutputContract
    # mapper 子 agent 配置。
    mapper: MapperSpec
    # reducer 归并配置。
    reducer: ReducerSpec
    # workflow 资源限制。
    limits: MapReduceLimits
    # 审计标签，用于关联 task、spec、用户请求或运行来源。
    audit_tags: tuple[str, ...]


@dataclass(frozen=True)
class MapShard:
    # shard 在 workflow 内的稳定 ID。
    shard_id: str
    # shard 展示名称。
    shard_name: str
    # shard 稳定排序序号。
    display_order: int
    # shard 包含的文件路径。
    files: tuple[str, ...]
    # shard 关联模块提示。
    module_hint: str | None
    # shard 生成原因。
    shard_reason: str
    # shard 估算 token 数。
    estimated_tokens: int
    # shard 输入摘要，用于复跑比对。
    shard_digest: str
    # mapper prompt 的 shard 专属上下文。
    context: str


@dataclass(frozen=True)
class MaterializedInputFile:
    # 原始文件路径，相对 input_source.root_dir。
    original_path: str
    # mapper 可读取的物化路径，相对 workdir。
    materialized_path: str
    # 原始文件内容摘要。
    content_digest: str
    # 是否发生截断。
    truncated: bool
    # 截断说明，未截断时为 None。
    truncation_reason: str | None


@dataclass(frozen=True)
class MapperInputManifest:
    # shard ID。
    shard_id: str
    # mapper 运行 ID，对应 agents/<task_run_id>/。
    task_run_id: str
    # 输入物化目录，位于 mapper workdir 内。
    input_dir: str
    # 物化文件列表。
    files: tuple[MaterializedInputFile, ...]
    # 物化时间，UTC ISO8601。
    materialized_at: str


@dataclass(frozen=True)
class CodeLocation:
    # 文件路径，相对 repo root。
    path: str
    # 起始行号，未知时为 None。
    line_start: int | None
    # 结束行号，未知时为 None。
    line_end: int | None
    # 符号名，未知时为 None。
    symbol: str | None
    # 证据摘录，保持短句。
    excerpt: str


@dataclass(frozen=True)
class CodeFinding:
    # finding 稳定去重 key，由 mapper 根据标题、文件和行号生成。
    dedupe_key: str
    # finding 标题。
    title: str
    # finding 类别。
    category: FindingCategory
    # 严重级别。
    severity: FindingSeverity
    # 置信度，取值 0.0 到 1.0。
    confidence: float
    # 涉及代码位置列表。
    locations: tuple[CodeLocation, ...]
    # 代码证据，必须可回到具体文件或符号。
    evidence: str
    # 风险原因，说明该问题为什么成立。
    rationale: str
    # 修复建议，要求可执行。
    recommendation: str
    # 影响范围，例如 runtime、test、docs、api、data。
    impact_area: tuple[str, ...]
    # 来源 shard ID。
    source_shard_id: str


@dataclass(frozen=True)
class MapperCoverage:
    # shard 输入文件总数。
    files_assigned: int
    # mapper 实际查看文件数。
    files_seen_count: int
    # mapper 实际查看符号数。
    symbols_seen_count: int
    # 跳过文件列表。
    skipped_files: tuple[str, ...]
    # 跳过原因列表。
    skip_reasons: tuple[str, ...]


@dataclass(frozen=True)
class MapperError:
    # 错误类型，例如 tool_error、validation_error、timeout、model_error。
    error_type: str
    # 人类可读错误信息。
    message: str
    # 关联文件路径，未知时为 None。
    file_path: str | None
    # 是否建议补跑该 shard。
    retryable: bool


@dataclass(frozen=True)
class MapperOutputEnvelope:
    # 输出契约名称。
    output_contract: OutputContract
    # mapper 处理的 shard ID。
    shard_id: str
    # mapper 运行状态，completed、partial 或 failed。
    status: MapperStatus
    # shard 级摘要。
    summary: str
    # 实际检查过的文件路径。
    files_seen: tuple[str, ...]
    # mapper 产出的 finding 列表。
    findings: tuple[CodeFinding, ...]
    # shard 覆盖率统计。
    coverage: MapperCoverage
    # mapper 过程中遇到的结构化错误。
    errors: tuple[MapperError, ...]


@dataclass(frozen=True)
class CoverageSummary:
    # 输入总文件数。
    total_files_assigned: int
    # 实际查看总文件数。
    total_files_seen: int
    # 实际查看总符号数。
    total_symbols_seen: int
    # 按 shard 汇总的覆盖率条目。
    per_shard: tuple[MapperCoverage, ...]
    # 覆盖率说明。
    notes: str


@dataclass(frozen=True)
class FailedShardReport:
    # shard ID。
    shard_id: str
    # shard 展示名称。
    shard_name: str
    # 失败阶段，mapper、validation 或 reducer。
    failed_stage: FailedShardStage
    # 失败原因。
    reason: str
    # 是否建议补跑。
    retryable: bool
    # 补跑建议。
    retry_hint: str


@dataclass(frozen=True)
class ReducerOutput:
    # reducer 运行状态，completed、partial 或 failed。
    status: MapperStatus
    # workflow ID。
    workflow_id: str
    # 输出契约名称。
    output_contract: OutputContract
    # shard 总数。
    total_shards: int
    # 成功 shard 数。
    completed_shards: int
    # 失败 shard 数。
    failed_shards: int
    # 去重后的 finding 列表。
    deduped_findings: tuple[CodeFinding, ...]
    # 按排序策略选出的重点 finding。
    top_findings: tuple[CodeFinding, ...]
    # 覆盖率汇总。
    coverage_summary: CoverageSummary
    # 失败 shard 汇总。
    failed_shard_reports: tuple[FailedShardReport, ...]
    # 建议后续任务。
    followups: tuple[str, ...]
    # reducer 生成时间，UTC ISO8601。
    reduced_at: str


@dataclass(frozen=True)
class MapperValidationResult:
    # 期望校验的 shard ID。
    expected_shard_id: str
    # mapper 输出中的 shard ID，解析失败或缺失时为 None。
    shard_id: str | None
    # 校验是否通过。
    valid: bool
    # 校验通过后的 mapper 输出。
    output: MapperOutputEnvelope | None
    # 校验失败时的结构化错误列表。
    errors: tuple[MapperValidationError, ...]
    # mapper 原始 final content 的 sha256 摘要。
    raw_content_digest: str
    # 从 mapper final content 提取出的只读 JSON payload 快照。
    payload: Mapping[str, Any] | None = None


class MapperOutputValidator:
    """Validates mapper final text against the v0.1 code_findings contract."""

    def validate(self, content: str, *, expected_shard_id: str = "") -> MapperValidationResult:
        return validate_mapper_output(content, expected_shard_id=expected_shard_id)


def parse_map_reduce_workflow_spec(payload: Mapping[str, Any]) -> MapReduceWorkflowSpec:
    """Parse a tool JSON payload into the executors-owned runtime spec."""

    errors: list[ContractValidationError] = []
    spec = _parse_workflow_spec(payload, errors)
    if errors:
        raise MapReduceContractError(errors)
    return spec


def validate_mapper_output(
    content: str,
    *,
    expected_shard_id: str,
) -> MapperValidationResult:
    """Validate mapper final text and preserve shard context for reducer failures."""

    raw_content_digest = _content_digest(content if isinstance(content, str) else "")
    errors: list[MapperValidationError] = []
    try:
        payload = extract_json_object(content)
    except MapReduceContractError as exc:
        return MapperValidationResult(
            expected_shard_id=expected_shard_id,
            shard_id=None,
            valid=False,
            output=None,
            errors=exc.errors,
            raw_content_digest=raw_content_digest,
            payload=None,
        )

    shard_id = payload.get("shard_id") if isinstance(payload.get("shard_id"), str) else None
    output = _parse_mapper_output(payload, errors)
    _validate_finding_source_shards(output, errors)
    payload_snapshot = _freeze_json_object(payload)
    if expected_shard_id and shard_id != expected_shard_id:
        errors.append(
            MapperValidationError(
                "shard_mismatch",
                "$.shard_id",
                "mapper output shard_id does not match expected shard",
                expected=expected_shard_id,
                actual=shard_id,
                retryable=True,
            )
        )
    if errors:
        return MapperValidationResult(
            expected_shard_id=expected_shard_id,
            shard_id=shard_id,
            valid=False,
            output=None,
            errors=tuple(errors),
            raw_content_digest=raw_content_digest,
            payload=payload_snapshot,
        )
    return MapperValidationResult(
        expected_shard_id=expected_shard_id,
        shard_id=output.shard_id,
        valid=True,
        output=output,
        errors=(),
        raw_content_digest=raw_content_digest,
        payload=payload_snapshot,
    )


def extract_json_object(content: str) -> dict[str, Any]:
    """Extract one JSON object from pure JSON, fenced JSON, or wrapped text."""

    if not isinstance(content, str) or not content.strip():
        raise MapReduceContractError(
            [MapperValidationError("json_not_found", "$", "mapper output is empty")]
        )
    candidates = [
        content.strip(),
        *_fenced_json_candidates(content),
        *_balanced_json_candidates(content),
    ]
    decode_errors: list[str] = []
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            decode_errors.append(str(exc))
            continue
        if isinstance(parsed, dict):
            return parsed
        raise MapReduceContractError(
            [
                ContractValidationError(
                    "json_not_object",
                    "$",
                    "mapper output JSON must be an object",
                )
            ]
        )
    detail = decode_errors[0] if decode_errors else "no JSON object found"
    raise MapReduceContractError(
        [MapperValidationError("json_parse_failed", "$", f"failed to parse JSON: {detail}")]
    )


def _parse_workflow_spec(
    raw: Mapping[str, Any],
    errors: list[ContractValidationError],
) -> MapReduceWorkflowSpec:
    obj = _mapping(raw, "$", errors)
    input_source = _parse_input_source(
        _mapping(obj.get("input_source"), "$.input_source", errors), errors
    )
    shard_strategy = _parse_shard_strategy(
        _mapping(obj.get("shard_strategy"), "$.shard_strategy", errors),
        errors,
    )
    mapper = _parse_mapper_spec(_mapping(obj.get("mapper"), "$.mapper", errors), errors)
    reducer = _parse_reducer_spec(_mapping(obj.get("reducer"), "$.reducer", errors), errors)
    limits = _parse_limits(_mapping(obj.get("limits"), "$.limits", errors), errors)
    mode = _literal(
        obj.get("mode", "map_reduce"),
        "$.mode",
        {"map_reduce"},
        errors,
        default="map_reduce",
    )
    output_contract = _literal(
        obj.get("output_contract"),
        "$.output_contract",
        _OUTPUT_CONTRACTS,
        errors,
        default="code_findings",
    )
    return MapReduceWorkflowSpec(
        mode=mode,  # type: ignore[arg-type]
        objective=_str(obj.get("objective"), "$.objective", errors),
        input_source=input_source,
        shard_strategy=shard_strategy,
        output_contract=output_contract,  # type: ignore[arg-type]
        mapper=mapper,
        reducer=reducer,
        limits=limits,
        audit_tags=_tuple_str(obj.get("audit_tags", ()), "$.audit_tags", errors),
    )


def _parse_input_source(
    obj: Mapping[str, Any],
    errors: list[ContractValidationError],
) -> MapReduceInputSource:
    return MapReduceInputSource(
        kind=_literal(
            obj.get("kind"),
            "$.input_source.kind",
            _INPUT_SOURCE_KINDS,
            errors,
            default="path_glob",
        ),  # type: ignore[arg-type]
        root_dir=_path_str(
            obj.get("root_dir"),
            "$.input_source.root_dir",
            errors,
            allow_current_dir=True,
            allow_glob=False,
        ),
        include=_tuple_path(
            obj.get("include", ()),
            "$.input_source.include",
            errors,
            allow_glob=True,
        ),
        exclude=_tuple_path(
            obj.get("exclude", ()),
            "$.input_source.exclude",
            errors,
            allow_glob=True,
        ),
        files=_tuple_path(
            obj.get("files", ()),
            "$.input_source.files",
            errors,
            allow_glob=False,
        ),
        index_provider=_optional_str(
            obj.get("index_provider"), "$.input_source.index_provider", errors
        ),
        input_digest=_optional_str(obj.get("input_digest"), "$.input_source.input_digest", errors),
    )


def _parse_shard_strategy(
    obj: Mapping[str, Any],
    errors: list[ContractValidationError],
) -> ShardStrategy:
    prefer_dependency_cohesion = _bool(
        obj.get("prefer_dependency_cohesion"),
        "$.shard_strategy.prefer_dependency_cohesion",
        errors,
    )
    if prefer_dependency_cohesion:
        errors.append(
            MapperValidationError(
                "literal_error",
                "$.shard_strategy.prefer_dependency_cohesion",
                "v0.1 requires prefer_dependency_cohesion to be false",
                expected="False",
                actual="True",
                retryable=False,
            )
        )
    return ShardStrategy(
        kind=_literal(
            obj.get("kind"),
            "$.shard_strategy.kind",
            _SHARD_STRATEGY_KINDS,
            errors,
            default="by_file_count",
        ),  # type: ignore[arg-type]
        max_files_per_shard=_int(
            obj.get("max_files_per_shard"),
            "$.shard_strategy.max_files_per_shard",
            errors,
            min_value=1,
        ),
        max_estimated_tokens_per_shard=_int(
            obj.get("max_estimated_tokens_per_shard"),
            "$.shard_strategy.max_estimated_tokens_per_shard",
            errors,
            min_value=1,
        ),
        min_shards=_int(obj.get("min_shards"), "$.shard_strategy.min_shards", errors, min_value=1),
        max_shards=_int(obj.get("max_shards"), "$.shard_strategy.max_shards", errors, min_value=1),
        preserve_directory_boundary=_bool(
            obj.get("preserve_directory_boundary"),
            "$.shard_strategy.preserve_directory_boundary",
            errors,
        ),
        prefer_dependency_cohesion=prefer_dependency_cohesion,
    )


def _parse_mapper_spec(
    obj: Mapping[str, Any],
    errors: list[ContractValidationError],
) -> MapperSpec:
    return MapperSpec(
        name_prefix=_str(obj.get("name_prefix"), "$.mapper.name_prefix", errors),
        prompt_template=_str(obj.get("prompt_template"), "$.mapper.prompt_template", errors),
        tool_names=_tuple_str(obj.get("tool_names", ()), "$.mapper.tool_names", errors),
        skill_names=_tuple_str(obj.get("skill_names", ()), "$.mapper.skill_names", errors),
        permission_mode=_literal(
            obj.get("permission_mode"),
            "$.mapper.permission_mode",
            {"scoped_workdir"},
            errors,
            default="scoped_workdir",
        ),  # type: ignore[arg-type]
        max_turns=_int(obj.get("max_turns"), "$.mapper.max_turns", errors, min_value=1),
        max_output_chars=_int(
            obj.get("max_output_chars"),
            "$.mapper.max_output_chars",
            errors,
            min_value=1,
        ),
    )


def _parse_reducer_spec(
    obj: Mapping[str, Any],
    errors: list[ContractValidationError],
) -> ReducerSpec:
    return ReducerSpec(
        kind=_literal(
            obj.get("kind"),
            "$.reducer.kind",
            _REDUCER_KINDS,
            errors,
            default="deterministic",
        ),  # type: ignore[arg-type]
        dedupe_strategy=_literal(
            obj.get("dedupe_strategy"),
            "$.reducer.dedupe_strategy",
            _REDUCER_DEDUPE_STRATEGIES,
            errors,
            default="exact_dedupe_key",
        ),  # type: ignore[arg-type]
        ranking_strategy=_literal(
            obj.get("ranking_strategy"),
            "$.reducer.ranking_strategy",
            _REDUCER_RANKING_STRATEGIES,
            errors,
            default="severity_first",
        ),  # type: ignore[arg-type]
        max_findings=_int(obj.get("max_findings"), "$.reducer.max_findings", errors, min_value=1),
        include_failed_shards=_bool(
            obj.get("include_failed_shards"),
            "$.reducer.include_failed_shards",
            errors,
        ),
        reducer_prompt_template=_optional_str(
            obj.get("reducer_prompt_template"),
            "$.reducer.reducer_prompt_template",
            errors,
        ),
    )


def _parse_limits(
    obj: Mapping[str, Any],
    errors: list[ContractValidationError],
) -> MapReduceLimits:
    return MapReduceLimits(
        max_concurrency=_int(
            obj.get("max_concurrency"), "$.limits.max_concurrency", errors, min_value=1
        ),
        workflow_timeout_seconds=_int(
            obj.get("workflow_timeout_seconds"),
            "$.limits.workflow_timeout_seconds",
            errors,
            min_value=1,
        ),
        mapper_timeout_seconds=_int(
            obj.get("mapper_timeout_seconds"),
            "$.limits.mapper_timeout_seconds",
            errors,
            min_value=1,
        ),
        reducer_timeout_seconds=_int(
            obj.get("reducer_timeout_seconds"),
            "$.limits.reducer_timeout_seconds",
            errors,
            min_value=1,
        ),
        mapper_retries=_int(
            obj.get("mapper_retries"), "$.limits.mapper_retries", errors, min_value=0
        ),
        validation_repair_retries=_int(
            obj.get("validation_repair_retries"),
            "$.limits.validation_repair_retries",
            errors,
            min_value=0,
        ),
    )


def _parse_mapper_output(
    raw: Mapping[str, Any],
    errors: list[ContractValidationError],
) -> MapperOutputEnvelope:
    obj = _mapping(raw, "$", errors)
    findings_raw = _sequence(obj.get("findings"), "$.findings", errors)
    errors_raw = _sequence(obj.get("errors"), "$.errors", errors)
    return MapperOutputEnvelope(
        output_contract=_literal(
            obj.get("output_contract"),
            "$.output_contract",
            _OUTPUT_CONTRACTS,
            errors,
            default="code_findings",
        ),  # type: ignore[arg-type]
        shard_id=_str(obj.get("shard_id"), "$.shard_id", errors),
        status=_literal(
            obj.get("status"),
            "$.status",
            _MAPPER_STATUSES,
            errors,
            default="failed",
        ),  # type: ignore[arg-type]
        summary=_str(obj.get("summary"), "$.summary", errors),
        files_seen=_tuple_str(obj.get("files_seen"), "$.files_seen", errors),
        findings=tuple(
            _parse_finding(
                _mapping(item, f"$.findings[{index}]", errors), f"$.findings[{index}]", errors
            )
            for index, item in enumerate(findings_raw)
        ),
        coverage=_parse_coverage(
            _mapping(obj.get("coverage"), "$.coverage", errors), "$.coverage", errors
        ),
        errors=tuple(
            _parse_mapper_error(
                _mapping(item, f"$.errors[{index}]", errors),
                f"$.errors[{index}]",
                errors,
            )
            for index, item in enumerate(errors_raw)
        ),
    )


def _validate_finding_source_shards(
    output: MapperOutputEnvelope,
    errors: list[ContractValidationError],
) -> None:
    if not output.shard_id:
        return
    for index, finding in enumerate(output.findings):
        if finding.source_shard_id != output.shard_id:
            errors.append(
                MapperValidationError(
                    "shard_mismatch",
                    f"$.findings[{index}].source_shard_id",
                    "finding source_shard_id must match mapper output shard_id",
                    expected=output.shard_id,
                    actual=finding.source_shard_id,
                    retryable=True,
                )
            )


def _parse_finding(
    obj: Mapping[str, Any],
    path: str,
    errors: list[ContractValidationError],
) -> CodeFinding:
    locations_raw = _sequence(obj.get("locations"), f"{path}.locations", errors)
    if isinstance(locations_raw, Sequence) and not locations_raw:
        errors.append(
            MapperValidationError(
                "value_error",
                f"{path}.locations",
                "expected at least one code location",
                expected="non-empty array",
                actual="[]",
                retryable=True,
            )
        )
    return CodeFinding(
        dedupe_key=_str(obj.get("dedupe_key"), f"{path}.dedupe_key", errors),
        title=_str(obj.get("title"), f"{path}.title", errors),
        category=_literal(
            obj.get("category"),
            f"{path}.category",
            _FINDING_CATEGORIES,
            errors,
            default="maintainability",
        ),  # type: ignore[arg-type]
        severity=_literal(
            obj.get("severity"),
            f"{path}.severity",
            _FINDING_SEVERITIES,
            errors,
            default="P3",
        ),  # type: ignore[arg-type]
        confidence=_confidence(obj.get("confidence"), f"{path}.confidence", errors),
        locations=tuple(
            _parse_location(
                _mapping(item, f"{path}.locations[{index}]", errors),
                f"{path}.locations[{index}]",
                errors,
            )
            for index, item in enumerate(locations_raw)
        ),
        evidence=_str(obj.get("evidence"), f"{path}.evidence", errors),
        rationale=_str(obj.get("rationale"), f"{path}.rationale", errors),
        recommendation=_str(obj.get("recommendation"), f"{path}.recommendation", errors),
        impact_area=_tuple_str(obj.get("impact_area"), f"{path}.impact_area", errors),
        source_shard_id=_str(obj.get("source_shard_id"), f"{path}.source_shard_id", errors),
    )


def _parse_location(
    obj: Mapping[str, Any],
    path: str,
    errors: list[ContractValidationError],
) -> CodeLocation:
    return CodeLocation(
        path=_str(obj.get("path"), f"{path}.path", errors),
        line_start=_optional_int(obj.get("line_start"), f"{path}.line_start", errors, min_value=1),
        line_end=_optional_int(obj.get("line_end"), f"{path}.line_end", errors, min_value=1),
        symbol=_optional_str(obj.get("symbol"), f"{path}.symbol", errors),
        excerpt=_str(obj.get("excerpt"), f"{path}.excerpt", errors),
    )


def _parse_coverage(
    obj: Mapping[str, Any],
    path: str,
    errors: list[ContractValidationError],
) -> MapperCoverage:
    return MapperCoverage(
        files_assigned=_int(
            obj.get("files_assigned"), f"{path}.files_assigned", errors, min_value=0
        ),
        files_seen_count=_int(
            obj.get("files_seen_count"),
            f"{path}.files_seen_count",
            errors,
            min_value=0,
        ),
        symbols_seen_count=_int(
            obj.get("symbols_seen_count"),
            f"{path}.symbols_seen_count",
            errors,
            min_value=0,
        ),
        skipped_files=_tuple_str(obj.get("skipped_files", ()), f"{path}.skipped_files", errors),
        skip_reasons=_tuple_str(obj.get("skip_reasons", ()), f"{path}.skip_reasons", errors),
    )


def _parse_mapper_error(
    obj: Mapping[str, Any],
    path: str,
    errors: list[ContractValidationError],
) -> MapperError:
    return MapperError(
        error_type=_str(obj.get("error_type"), f"{path}.error_type", errors),
        message=_str(obj.get("message"), f"{path}.message", errors),
        file_path=_optional_str(obj.get("file_path"), f"{path}.file_path", errors),
        retryable=_bool(obj.get("retryable"), f"{path}.retryable", errors),
    )


def _mapping(
    value: object,
    path: str,
    errors: list[ContractValidationError],
) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    errors.append(
        ContractValidationError(
            "type_error",
            path,
            "expected object",
            expected="object",
            actual=_actual_value(value),
        )
    )
    return {}


def _sequence(
    value: object,
    path: str,
    errors: list[ContractValidationError],
) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return value
    errors.append(
        ContractValidationError(
            "type_error",
            path,
            "expected array",
            expected="array",
            actual=_actual_value(value),
        )
    )
    return ()


def _str(value: object, path: str, errors: list[ContractValidationError]) -> str:
    if isinstance(value, str) and value.strip():
        return value
    errors.append(
        ContractValidationError(
            "type_error",
            path,
            "expected non-empty string",
            expected="non-empty string",
            actual=_actual_value(value),
        )
    )
    return ""


def _optional_str(
    value: object,
    path: str,
    errors: list[ContractValidationError],
) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    errors.append(
        ContractValidationError(
            "type_error",
            path,
            "expected string or null",
            expected="string or null",
            actual=_actual_value(value),
        )
    )
    return None


def _int(
    value: object,
    path: str,
    errors: list[ContractValidationError],
    *,
    min_value: int | None = None,
) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        if min_value is not None and value < min_value:
            errors.append(
                ContractValidationError(
                    "value_error",
                    path,
                    f"expected integer >= {min_value}",
                    expected=f"integer >= {min_value}",
                    actual=str(value),
                )
            )
        return value
    errors.append(
        ContractValidationError(
            "type_error",
            path,
            "expected integer",
            expected="integer",
            actual=_actual_value(value),
        )
    )
    return 0


def _optional_int(
    value: object,
    path: str,
    errors: list[ContractValidationError],
    *,
    min_value: int | None = None,
) -> int | None:
    if value is None:
        return None
    return _int(value, path, errors, min_value=min_value)


def _confidence(
    value: object,
    path: str,
    errors: list[ContractValidationError],
) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number) and 0.0 <= number <= 1.0:
            return number
        errors.append(
            ContractValidationError(
                "value_error",
                path,
                "expected number between 0.0 and 1.0",
                expected="number between 0.0 and 1.0",
                actual=_actual_value(value),
            )
        )
        return number
    errors.append(
        ContractValidationError(
            "type_error",
            path,
            "expected number",
            expected="number",
            actual=_actual_value(value),
        )
    )
    return 0.0


def _bool(value: object, path: str, errors: list[ContractValidationError]) -> bool:
    if isinstance(value, bool):
        return value
    errors.append(
        ContractValidationError(
            "type_error",
            path,
            "expected boolean",
            expected="boolean",
            actual=_actual_value(value),
        )
    )
    return False


def _literal(
    value: object,
    path: str,
    allowed: frozenset[str] | set[str],
    errors: list[ContractValidationError],
    *,
    default: str,
) -> str:
    if isinstance(value, str) and value in allowed:
        return value
    choices = ", ".join(sorted(allowed))
    errors.append(
        ContractValidationError(
            "literal_error",
            path,
            f"expected one of: {choices}",
            expected=choices,
            actual=_actual_value(value),
        )
    )
    return default


def _tuple_str(
    value: object,
    path: str,
    errors: list[ContractValidationError],
) -> tuple[str, ...]:
    sequence = _sequence(value, path, errors)
    items: list[str] = []
    for index, item in enumerate(sequence):
        if isinstance(item, str):
            items.append(item)
        else:
            errors.append(
                ContractValidationError(
                    "type_error",
                    f"{path}[{index}]",
                    "expected string",
                    expected="string",
                    actual=_actual_value(item),
                )
            )
    return tuple(items)


def _path_str(
    value: object,
    path: str,
    errors: list[ContractValidationError],
    *,
    allow_current_dir: bool,
    allow_glob: bool,
) -> str:
    text = _str(value, path, errors)
    if not text:
        return ""
    _validate_relative_path_text(
        text,
        path,
        errors,
        allow_current_dir=allow_current_dir,
        allow_glob=allow_glob,
    )
    return text


def _tuple_path(
    value: object,
    path: str,
    errors: list[ContractValidationError],
    *,
    allow_glob: bool,
) -> tuple[str, ...]:
    sequence = _sequence(value, path, errors)
    items: list[str] = []
    for index, item in enumerate(sequence):
        item_path = f"{path}[{index}]"
        if not isinstance(item, str) or not item.strip():
            errors.append(
                ContractValidationError(
                    "path_error",
                    item_path,
                    "expected non-empty relative path",
                    expected="non-empty relative path",
                    actual=_actual_value(item),
                )
            )
            continue
        _validate_relative_path_text(
            item,
            item_path,
            errors,
            allow_current_dir=False,
            allow_glob=allow_glob,
        )
        items.append(item)
    return tuple(items)


def _validate_relative_path_text(
    value: str,
    path: str,
    errors: list[ContractValidationError],
    *,
    allow_current_dir: bool,
    allow_glob: bool,
) -> None:
    if value != value.strip():
        errors.append(
            MapperValidationError(
                "path_error",
                path,
                "relative path must not contain leading or trailing whitespace",
                expected="trimmed relative path",
                actual=_actual_value(value),
                retryable=False,
            )
        )
    if _contains_control_char(value):
        errors.append(
            MapperValidationError(
                "path_error",
                path,
                "relative path must not contain control characters",
                expected="relative path without control characters",
                actual=_actual_value(value),
                retryable=False,
            )
        )
    if value.startswith(("/", "\\")) or _WINDOWS_ABSOLUTE_PATH_RE.match(value):
        errors.append(
            MapperValidationError(
                "path_error",
                path,
                "expected relative path",
                expected="relative path",
                actual=_actual_value(value),
                retryable=False,
            )
        )
    normalized = value.replace("\\", "/")
    if not allow_current_dir and normalized in {"", "."}:
        errors.append(
            MapperValidationError(
                "path_error",
                path,
                "expected non-empty relative path",
                expected="non-empty relative path",
                actual=_actual_value(value),
                retryable=False,
            )
        )
    if any(segment == ".." for segment in normalized.split("/")):
        errors.append(
            MapperValidationError(
                "path_error",
                path,
                "relative path must not contain traversal segments",
                expected="relative path without traversal",
                actual=_actual_value(value),
                retryable=False,
            )
        )
    if not allow_glob and any(char in value for char in "*?[]{}"):
        errors.append(
            MapperValidationError(
                "path_error",
                path,
                "relative file path must not contain glob metacharacters",
                expected="relative file path",
                actual=_actual_value(value),
                retryable=False,
            )
        )


def _contains_control_char(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _freeze_json_object(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({key: _freeze_json(value) for key, value in payload.items()})


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(_freeze_json(item) for item in value)
    return value


def _actual_value(value: object) -> str:
    rendered = repr(value)
    if len(rendered) > 120:
        return f"{rendered[:117]}..."
    return rendered


def _content_digest(content: str) -> str:
    encoded = content.encode("utf-8", errors="replace")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _fenced_json_candidates(content: str) -> list[str]:
    candidates: list[str] = []
    marker = "```"
    start = 0
    while True:
        open_index = content.find(marker, start)
        if open_index == -1:
            return candidates
        body_start = open_index + len(marker)
        newline_index = content.find("\n", body_start)
        if newline_index == -1:
            return candidates
        info = content[body_start:newline_index].strip().lower()
        close_index = content.find(marker, newline_index + 1)
        if close_index == -1:
            return candidates
        body = content[newline_index + 1 : close_index].strip()
        if not info or info == "json":
            candidates.append(body)
        start = close_index + len(marker)


def _balanced_json_candidates(content: str) -> list[str]:
    candidates: list[str] = []
    for index, char in enumerate(content):
        if char != "{":
            continue
        end = _find_balanced_object_end(content, index)
        if end is not None:
            candidates.append(content[index : end + 1])
    return candidates


def _find_balanced_object_end(content: str, start: int) -> int | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(content)):
        char = content[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


__all__ = [
    "CodeFinding",
    "CodeLocation",
    "ContractValidationError",
    "CoverageSummary",
    "FailedShardReport",
    "MapReduceContractError",
    "MapReduceInputSource",
    "MapReduceLimits",
    "MapReduceWorkflowSpec",
    "MapShard",
    "MapperCoverage",
    "MapperError",
    "MapperInputManifest",
    "MapperOutputEnvelope",
    "MapperOutputValidator",
    "MapperSpec",
    "MapperValidationError",
    "MapperValidationResult",
    "MaterializedInputFile",
    "ReducerOutput",
    "ReducerSpec",
    "ShardStrategy",
    "extract_json_object",
    "parse_map_reduce_workflow_spec",
    "validate_mapper_output",
]
