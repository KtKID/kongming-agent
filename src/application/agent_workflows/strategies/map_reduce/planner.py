"""Map-Reduce 输入规划器。

本脚本负责把 MapReduceWorkflowSpec 的输入范围转换为稳定的 MapShard 列表。
作用是让 map_reduce 策略在启动 mapper 子 agent 前完成文件发现、路径边界校验、分片和摘要生成。
关键执行流程：MapReducePlanner.plan 校验 input root，按 path_glob 或 file_list 收集普通文件，
再按 by_file_count 或 by_directory 生成稳定分片。
关键函数：plan 生成分片，_discover_files 收集文件，_build_file_count_groups 按数量分组，
_build_directory_groups 按目录分组，_build_shard 生成 MapShard。
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from application.agent_workflows.strategies.map_reduce.contracts import (
    MapReduceInputSource,
    MapReduceWorkflowSpec,
    MapShard,
    ShardStrategy,
)

_DIGEST_CHUNK_SIZE = 1024 * 1024
_ROOT_MODULE_HINT = "root"
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[a-zA-Z]:[\\/]")


class MapReducePlannerError(ValueError):
    """planner 输入或分片约束无法满足时抛出的错误。"""


@dataclass(frozen=True)
class _PlannerFile:
    """planner 内部文件元数据，保存相对路径、绝对路径、大小、token 估算和内容摘要。"""

    rel_path: str
    abs_path: Path
    size_bytes: int
    estimated_tokens: int
    content_digest: str


class MapReducePlanner:
    """根据 workspace 根目录和 workflow spec 生成稳定 mapper shard。"""

    def __init__(self, *, workspace_root: Path | str) -> None:
        """初始化 planner，输入为 workspace 根目录，输出为可复用的 planner 实例。"""
        self._workspace_root = Path(workspace_root).expanduser().resolve()

    def plan(self, spec: MapReduceWorkflowSpec) -> tuple[MapShard, ...]:
        """生成 MapShard，输入为 workflow spec，输出为稳定排序的分片元组。"""
        input_root = self._resolve_input_root(spec.input_source)
        files = self._discover_files(spec.input_source, input_root)
        if not files:
            raise MapReducePlannerError("map_reduce planner found no input files")
        groups = self._build_groups(files, spec.shard_strategy)
        if len(groups) > spec.shard_strategy.max_shards:
            raise MapReducePlannerError(
                "map_reduce planner cannot satisfy max_shards while preserving max_files_per_shard"
            )
        return tuple(
            self._build_shard(
                files=group,
                display_order=index,
                shard_reason=self._shard_reason(spec.shard_strategy),
            )
            for index, group in enumerate(groups, start=1)
        )

    def _resolve_input_root(self, input_source: MapReduceInputSource) -> Path:
        """解析输入根目录，输入为 input_source，输出为位于 workspace 内的绝对目录。"""
        _validate_relative_path_text(
            input_source.root_dir,
            field_name="input_source.root_dir",
            allow_current_dir=True,
            allow_glob=False,
        )
        raw_root = Path(_normalize_relative_path_text(input_source.root_dir))
        if raw_root.is_absolute():
            raise MapReducePlannerError("input_source.root_dir must be relative")
        resolved = (self._workspace_root / raw_root).resolve()
        if not _is_relative_to(resolved, self._workspace_root):
            raise MapReducePlannerError(
                f"input_source.root_dir escapes workspace_root: {input_source.root_dir!r}"
            )
        if not resolved.exists():
            raise MapReducePlannerError(
                f"input_source.root_dir does not exist: {input_source.root_dir!r}"
            )
        if not resolved.is_dir():
            raise MapReducePlannerError(
                f"input_source.root_dir must resolve to a directory: {input_source.root_dir!r}"
            )
        return resolved

    def _discover_files(
        self,
        input_source: MapReduceInputSource,
        input_root: Path,
    ) -> tuple[_PlannerFile, ...]:
        """收集输入文件，输入为来源配置和输入根目录，输出为稳定排序的文件元组。"""
        if input_source.kind == "path_glob":
            rel_paths = self._discover_glob_files(input_source, input_root)
        elif input_source.kind == "file_list":
            rel_paths = self._discover_listed_files(input_source, input_root)
        else:
            raise MapReducePlannerError(f"unsupported input_source.kind: {input_source.kind!r}")

        excluded = self._discover_excluded_paths(input_source.exclude, input_root)
        selected = tuple(path for path in rel_paths if path not in excluded)
        return tuple(self._to_planner_file(path, input_root) for path in selected)

    def _discover_glob_files(
        self,
        input_source: MapReduceInputSource,
        input_root: Path,
    ) -> tuple[str, ...]:
        """按 include glob 发现文件，输入为 input_source，输出为相对 input root 的路径。"""
        patterns = input_source.include or ("**/*",)
        discovered: set[str] = set()
        for pattern in patterns:
            discovered.update(self._glob_relative_files(input_root, pattern))
        return tuple(sorted(discovered))

    def _discover_listed_files(
        self,
        input_source: MapReduceInputSource,
        input_root: Path,
    ) -> tuple[str, ...]:
        """解析显式文件列表，输入为 input_source，输出为相对 input root 的路径。"""
        discovered: set[str] = set()
        for raw_path in input_source.files:
            _validate_relative_path_text(
                raw_path,
                field_name="input_source.files[]",
                allow_current_dir=False,
                allow_glob=False,
            )
            rel_path = _normalize_relative_path_text(raw_path)
            candidate = input_root / rel_path
            self._ensure_file_inside_input_root(candidate, input_root, rel_path)
            discovered.add(rel_path)
        return tuple(sorted(discovered))

    def _discover_excluded_paths(
        self,
        exclude_patterns: tuple[str, ...],
        input_root: Path,
    ) -> frozenset[str]:
        """解析 exclude glob，输入为排除规则，输出为相对 input root 的路径集合。"""
        excluded: set[str] = set()
        for pattern in exclude_patterns:
            excluded.update(self._glob_relative_files(input_root, pattern))
        return frozenset(excluded)

    def _glob_relative_files(self, input_root: Path, pattern: str) -> tuple[str, ...]:
        """执行单条 glob，输入为输入根和规则，输出为相对 input root 的普通文件路径。"""
        _validate_relative_path_text(
            pattern,
            field_name="input_source glob",
            allow_current_dir=False,
            allow_glob=True,
        )
        normalized_pattern = _normalize_relative_path_text(pattern)
        try:
            candidates = tuple(input_root.glob(normalized_pattern))
        except ValueError as exc:
            raise MapReducePlannerError(f"invalid glob pattern {pattern!r}: {exc}") from exc

        rel_paths: set[str] = set()
        for candidate in candidates:
            resolved = candidate.resolve()
            if not self._is_regular_file_inside_input_root(resolved, input_root):
                continue
            rel_paths.add(_relative_posix(candidate, input_root))
        return tuple(sorted(rel_paths))

    def _is_regular_file_inside_input_root(self, path: Path, input_root: Path) -> bool:
        """判断候选文件是否可作为输入，输入为绝对路径和输入根，输出为布尔值。"""
        return _is_relative_to(path, input_root) and path.is_file()

    def _ensure_file_inside_input_root(
        self,
        path: Path,
        input_root: Path,
        rel_path: str,
    ) -> None:
        """校验显式文件，输入为绝对路径、输入根和原始相对路径，输出为通过或异常。"""
        resolved = path.resolve()
        if not _is_relative_to(resolved, input_root):
            raise MapReducePlannerError(f"input file escapes input root: {rel_path!r}")
        if not path.exists():
            raise MapReducePlannerError(f"input file does not exist: {rel_path!r}")
        if not path.is_file():
            raise MapReducePlannerError(f"input file must be a regular file: {rel_path!r}")

    def _to_planner_file(self, rel_path: str, input_root: Path) -> _PlannerFile:
        """生成内部文件元数据，输入为相对路径和输入根，输出为 _PlannerFile。"""
        abs_path = input_root / rel_path
        self._ensure_file_inside_input_root(abs_path, input_root, rel_path)
        size_bytes = abs_path.stat().st_size
        return _PlannerFile(
            rel_path=rel_path,
            abs_path=abs_path,
            size_bytes=size_bytes,
            estimated_tokens=_estimate_tokens(size_bytes),
            content_digest=_hash_file(abs_path),
        )

    def _build_groups(
        self,
        files: tuple[_PlannerFile, ...],
        strategy: ShardStrategy,
    ) -> tuple[tuple[_PlannerFile, ...], ...]:
        """按策略生成文件组，输入为文件和分片策略，输出为稳定排序的文件组。"""
        self._validate_strategy(strategy)
        if strategy.kind == "by_file_count":
            groups = _build_file_count_groups(files, strategy)
        elif strategy.kind == "by_directory":
            groups = _build_directory_groups(files, strategy)
            groups = _pack_groups_to_respect_max_shards(groups, strategy)
        else:
            raise MapReducePlannerError(f"unsupported shard_strategy.kind: {strategy.kind!r}")

        groups = _split_to_min_shards(groups, strategy)
        groups = tuple(sorted(groups, key=_group_sort_key))
        if len(groups) > strategy.max_shards:
            raise MapReducePlannerError(
                f"map_reduce planner produced {len(groups)} shards beyond max_shards="
                f"{strategy.max_shards}"
            )
        return groups

    def _validate_strategy(self, strategy: ShardStrategy) -> None:
        """校验分片策略，输入为 ShardStrategy，输出为通过或异常。"""
        if strategy.max_files_per_shard < 1:
            raise MapReducePlannerError("shard_strategy.max_files_per_shard must be >= 1")
        if strategy.min_shards < 1:
            raise MapReducePlannerError("shard_strategy.min_shards must be >= 1")
        if strategy.max_shards < 1:
            raise MapReducePlannerError("shard_strategy.max_shards must be >= 1")
        if strategy.max_estimated_tokens_per_shard < 1:
            raise MapReducePlannerError(
                "shard_strategy.max_estimated_tokens_per_shard must be >= 1"
            )

    def _build_shard(
        self,
        *,
        files: tuple[_PlannerFile, ...],
        display_order: int,
        shard_reason: str,
    ) -> MapShard:
        """构造单个 MapShard，输入为文件组和序号，输出为完整 shard dataclass。"""
        rel_paths = tuple(file.rel_path for file in files)
        estimated_tokens = sum(file.estimated_tokens for file in files)
        shard_digest = _shard_digest(files)
        module_hint = _module_hint(rel_paths)
        shard_id = f"shard-{shard_digest.removeprefix('sha256:')[:16]}"
        shard_name = f"{display_order:03d}-{_slug(module_hint or 'mixed')}"
        return MapShard(
            shard_id=shard_id,
            shard_name=shard_name,
            display_order=display_order,
            files=rel_paths,
            module_hint=module_hint,
            shard_reason=shard_reason,
            estimated_tokens=estimated_tokens,
            shard_digest=shard_digest,
            context=_build_context(
                shard_id=shard_id,
                shard_name=shard_name,
                module_hint=module_hint,
                files=rel_paths,
                estimated_tokens=estimated_tokens,
            ),
        )

    def _shard_reason(self, strategy: ShardStrategy) -> str:
        """生成分片原因，输入为策略，输出为审计可读的原因字符串。"""
        if strategy.kind == "by_directory":
            return "directory_group"
        if strategy.kind == "by_file_count":
            return "file_count_group"
        raise MapReducePlannerError(f"unsupported shard_strategy.kind: {strategy.kind!r}")


def _build_file_count_groups(
    files: tuple[_PlannerFile, ...],
    strategy: ShardStrategy,
) -> tuple[tuple[_PlannerFile, ...], ...]:
    """按文件数量和 token 上限切分，输入为文件列表和策略，输出为文件组。"""
    ordered = tuple(sorted(files, key=lambda file: file.rel_path))
    return _pack_files_by_limits(ordered, strategy)


def _build_directory_groups(
    files: tuple[_PlannerFile, ...],
    strategy: ShardStrategy,
) -> tuple[tuple[_PlannerFile, ...], ...]:
    """按父目录和 limits 切分，输入为文件列表和策略，输出为目录优先的文件组。"""
    directories: dict[str, list[_PlannerFile]] = {}
    for file in sorted(files, key=lambda item: item.rel_path):
        directories.setdefault(_parent_directory(file.rel_path), []).append(file)

    groups: list[tuple[_PlannerFile, ...]] = []
    for _, directory_files in sorted(directories.items(), key=lambda item: item[0]):
        groups.extend(_pack_files_by_limits(tuple(directory_files), strategy))
    return tuple(groups)


def _pack_files_by_limits(
    files: tuple[_PlannerFile, ...],
    strategy: ShardStrategy,
) -> tuple[tuple[_PlannerFile, ...], ...]:
    """按文件数和 token 上限装箱，输入为文件和策略，输出为满足 limits 的文件组。"""
    groups: list[tuple[_PlannerFile, ...]] = []
    current: list[_PlannerFile] = []
    current_tokens = 0
    for file in files:
        if file.estimated_tokens > strategy.max_estimated_tokens_per_shard:
            raise MapReducePlannerError(
                "single input file exceeds shard_strategy.max_estimated_tokens_per_shard: "
                f"{file.rel_path}"
            )
        would_exceed_files = len(current) >= strategy.max_files_per_shard
        would_exceed_tokens = (
            current_tokens + file.estimated_tokens > strategy.max_estimated_tokens_per_shard
        )
        if current and (would_exceed_files or would_exceed_tokens):
            groups.append(tuple(current))
            current = []
            current_tokens = 0
        current.append(file)
        current_tokens += file.estimated_tokens
    if current:
        groups.append(tuple(current))
    return tuple(groups)


def _pack_groups_to_respect_max_shards(
    groups: tuple[tuple[_PlannerFile, ...], ...],
    strategy: ShardStrategy,
) -> tuple[tuple[_PlannerFile, ...], ...]:
    """合并小目录组，输入为目录组和策略，输出为尽量满足 max_shards 的文件组。"""
    if len(groups) <= strategy.max_shards:
        return groups

    packed: list[tuple[_PlannerFile, ...]] = []
    current: list[_PlannerFile] = []
    current_tokens = 0
    for group in groups:
        group_tokens = sum(file.estimated_tokens for file in group)
        would_exceed_files = len(current) + len(group) > strategy.max_files_per_shard
        would_exceed_tokens = (
            current_tokens + group_tokens > strategy.max_estimated_tokens_per_shard
        )
        if current and (would_exceed_files or would_exceed_tokens):
            packed.append(tuple(current))
            current = []
            current_tokens = 0
        current.extend(group)
        current_tokens += group_tokens
    if current:
        packed.append(tuple(current))
    return tuple(packed)


def _split_to_min_shards(
    groups: tuple[tuple[_PlannerFile, ...], ...],
    strategy: ShardStrategy,
) -> tuple[tuple[_PlannerFile, ...], ...]:
    """按 min_shards 细分，输入为现有组和策略，输出为达到可行最小数量的组。"""
    target = min(strategy.min_shards, strategy.max_shards, sum(len(group) for group in groups))
    result = list(groups)
    while len(result) < target:
        split_index = _largest_splittable_group_index(result)
        if split_index is None:
            break
        group = result.pop(split_index)
        middle = math.ceil(len(group) / 2)
        result.insert(split_index, group[middle:])
        result.insert(split_index, group[:middle])
    return tuple(result)


def _largest_splittable_group_index(groups: list[tuple[_PlannerFile, ...]]) -> int | None:
    """查找最大可拆组，输入为文件组列表，输出为可拆组下标或 None。"""
    best_index: int | None = None
    best_size = 1
    for index, group in enumerate(groups):
        if len(group) > best_size:
            best_index = index
            best_size = len(group)
    return best_index


def _group_sort_key(group: tuple[_PlannerFile, ...]) -> tuple[str, ...]:
    """生成文件组排序键，输入为文件组，输出为稳定路径元组。"""
    return tuple(file.rel_path for file in group)


def _validate_relative_path_text(
    value: str,
    *,
    field_name: str,
    allow_current_dir: bool,
    allow_glob: bool,
) -> None:
    """校验相对路径文本，输入为路径和规则，输出为通过或异常。"""
    if not isinstance(value, str) or not value.strip():
        raise MapReducePlannerError(f"{field_name} must be a non-empty relative path")
    if value != value.strip():
        raise MapReducePlannerError(f"{field_name} must not contain surrounding whitespace")
    if any(ord(char) < 32 for char in value):
        raise MapReducePlannerError(f"{field_name} must not contain control characters")
    normalized = _normalize_relative_path_text(value)
    if value.startswith(("/", "\\")) or _WINDOWS_ABSOLUTE_PATH_RE.match(value):
        raise MapReducePlannerError(f"{field_name} must be relative")
    if not allow_current_dir and normalized in {"", "."}:
        raise MapReducePlannerError(f"{field_name} must be a non-empty relative path")
    if any(segment == ".." for segment in normalized.split("/")):
        raise MapReducePlannerError(f"{field_name} must stay inside input root")
    if not allow_glob and any(char in normalized for char in "*?[]{}"):
        raise MapReducePlannerError(f"{field_name} must be a concrete relative path")


def _normalize_relative_path_text(value: str) -> str:
    """规范化相对路径文本，输入为原始路径，输出为 POSIX 分隔符路径。"""
    return value.replace("\\", "/")


def _relative_posix(path: Path, root: Path) -> str:
    """计算 POSIX 相对路径，输入为绝对路径和根目录，输出为相对 input root 的路径。"""
    return path.relative_to(root).as_posix()


def _is_relative_to(path: Path, root: Path) -> bool:
    """判断路径归属，输入为候选路径和根路径，输出为是否位于根路径内。"""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _estimate_tokens(size_bytes: int) -> int:
    """估算 token 数，输入为文件字节数，输出为按字节数除以四向上取整的值。"""
    if size_bytes <= 0:
        return 0
    return math.ceil(size_bytes / 4)


def _hash_file(path: Path) -> str:
    """计算文件内容摘要，输入为文件路径，输出为 sha256 摘要字符串。"""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(_DIGEST_CHUNK_SIZE), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _shard_digest(files: tuple[_PlannerFile, ...]) -> str:
    """计算 shard 摘要，输入为文件元数据，输出为稳定 sha256 摘要。"""
    digest = hashlib.sha256()
    for file in files:
        digest.update(file.rel_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(file.size_bytes).encode("ascii"))
        digest.update(b"\0")
        digest.update(file.content_digest.encode("ascii"))
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _module_hint(files: tuple[str, ...]) -> str | None:
    """推断模块提示，输入为 shard 文件路径，输出为公共目录或 None。"""
    if not files:
        return None
    parents = tuple(_parent_directory(path) for path in files)
    common_parts = list(PurePosixPath(parents[0]).parts)
    for parent in parents[1:]:
        parent_parts = PurePosixPath(parent).parts
        prefix: list[str] = []
        for part, other in zip(common_parts, parent_parts, strict=False):
            if part != other:
                break
            prefix.append(part)
        common_parts = prefix
        if not common_parts:
            return None
    if not common_parts or common_parts == ["."]:
        return _ROOT_MODULE_HINT
    return PurePosixPath(*common_parts).as_posix()


def _parent_directory(rel_path: str) -> str:
    """读取父目录，输入为 POSIX 相对路径，输出为父目录或 root。"""
    parent = PurePosixPath(rel_path).parent.as_posix()
    if parent == ".":
        return _ROOT_MODULE_HINT
    return parent


def _slug(value: str) -> str:
    """生成展示名片段，输入为原始文本，输出为稳定小写短标识。"""
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-").lower()
    return normalized[:48] or "shard"


def _build_context(
    *,
    shard_id: str,
    shard_name: str,
    module_hint: str | None,
    files: tuple[str, ...],
    estimated_tokens: int,
) -> str:
    """生成 shard 上下文，输入为 shard 元数据，输出为 mapper prompt 可注入文本。"""
    file_lines = "\n".join(f"- {path}" for path in files)
    module_text = module_hint or "mixed"
    return (
        f"Shard ID: {shard_id}\n"
        f"Shard name: {shard_name}\n"
        f"Module hint: {module_text}\n"
        f"Files assigned: {len(files)}\n"
        f"Estimated tokens: {estimated_tokens}\n"
        "Paths are relative to input_source.root_dir and use POSIX separators.\n"
        "Files:\n"
        f"{file_lines}"
    )


__all__ = ["MapReducePlanner", "MapReducePlannerError"]
