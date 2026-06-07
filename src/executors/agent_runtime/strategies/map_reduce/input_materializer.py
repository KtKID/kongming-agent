"""map_reduce mapper 输入物化器。

本脚本负责把 planner 生成的 shard 文件复制到 mapper 的 scoped workdir。
作用是让 mapper 子 agent 只通过 `agents/<task_run_id>/work/` 读取本 shard 输入，同时保留原始路径、物化路径、内容摘要和截断信息。
关键执行流程：解析 input_source.root_dir，逐个复制 shard.files 到 work/input/，写入 work/input_manifest.json，返回 MapperInputManifest。
关键函数：MapperInputMaterializer.materialize 物化单个 shard，MapperInputMaterializer.materialize_many 批量物化 shard。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from executors.agent_runtime.strategies.map_reduce_contracts import (
    MapperInputManifest,
    MapReduceWorkflowSpec,
    MapShard,
    MaterializedInputFile,
)
from executors.agent_runtime.subagent_permissions import to_jsonable


class MapperInputMaterializer:
    """把 shard 输入复制到 mapper scoped workdir 的 input/ 目录。"""

    def __init__(self, *, workspace_root: Path, max_file_bytes: int | None = None) -> None:
        """初始化物化器，输入为工作区根目录和可选单文件截断上限，输出为可复用实例。"""
        if max_file_bytes is not None and max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive when provided")
        self._workspace_root = workspace_root.expanduser().resolve()
        self._max_file_bytes = max_file_bytes

    def materialize(
        self,
        *,
        workflow_dir: Path,
        task_run_id: str,
        shard: MapShard,
        spec: MapReduceWorkflowSpec,
    ) -> MapperInputManifest:
        """物化单个 shard，输入为 workflow 目录、mapper 运行 ID、shard 和 spec，输出为输入清单。"""
        if not task_run_id.strip():
            raise ValueError("task_run_id must be non-empty")
        input_root = self._resolve_input_root(spec)
        work_dir = workflow_dir.expanduser().resolve() / "agents" / task_run_id / "work"
        input_dir = work_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)

        materialized_files = tuple(
            self._materialize_one_file(
                input_root=input_root,
                input_dir=input_dir,
                raw_file_path=raw_file_path,
            )
            for raw_file_path in shard.files
        )
        manifest = MapperInputManifest(
            shard_id=shard.shard_id,
            task_run_id=task_run_id,
            input_dir="input",
            files=materialized_files,
            materialized_at=_now_iso(),
        )
        self._write_json(work_dir / "input_manifest.json", to_jsonable(manifest))
        return manifest

    def materialize_many(
        self,
        *,
        workflow_dir: Path,
        shard_task_run_ids: dict[str, str],
        shards: tuple[MapShard, ...],
        spec: MapReduceWorkflowSpec,
    ) -> tuple[MapperInputManifest, ...]:
        """批量物化 shard，输入为 shard 到 task_run_id 的映射，输出为稳定顺序的 manifest 列表。"""
        manifests: list[MapperInputManifest] = []
        for shard in shards:
            task_run_id = shard_task_run_ids.get(shard.shard_id)
            if not isinstance(task_run_id, str) or not task_run_id.strip():
                raise ValueError(f"missing task_run_id for shard {shard.shard_id!r}")
            manifests.append(
                self.materialize(
                    workflow_dir=workflow_dir,
                    task_run_id=task_run_id,
                    shard=shard,
                    spec=spec,
                )
            )
        return tuple(manifests)

    def _resolve_input_root(self, spec: MapReduceWorkflowSpec) -> Path:
        """解析输入根目录，输入为 workflow spec，输出为 workspace_root 下的绝对路径。"""
        root_dir = Path(spec.input_source.root_dir).expanduser()
        candidate = root_dir if root_dir.is_absolute() else self._workspace_root / root_dir
        input_root = candidate.resolve()
        if not _is_relative_to(input_root, self._workspace_root):
            raise ValueError(
                f"map_reduce input_source.root_dir must stay inside workspace_root: {input_root}"
            )
        if not input_root.exists() or not input_root.is_dir():
            raise FileNotFoundError(f"map_reduce input root does not exist: {input_root}")
        return input_root

    def _materialize_one_file(
        self,
        *,
        input_root: Path,
        input_dir: Path,
        raw_file_path: str,
    ) -> MaterializedInputFile:
        """物化单个文件，输入为输入根、目标 input 目录和 shard 文件路径，输出为文件映射记录。"""
        source = _resolve_input_file(input_root=input_root, raw_file_path=raw_file_path)
        original_path = source.relative_to(input_root).as_posix()
        content = source.read_bytes()
        digest = _content_digest(content)
        materialized_content, truncated, reason = self._maybe_truncate(content)
        materialized_path = f"input/{original_path}"
        destination = input_dir / original_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(materialized_content)
        return MaterializedInputFile(
            original_path=original_path,
            materialized_path=materialized_path,
            content_digest=digest,
            truncated=truncated,
            truncation_reason=reason,
        )

    def _maybe_truncate(self, content: bytes) -> tuple[bytes, bool, str | None]:
        """按单文件上限截断内容，输入为原始 bytes，输出为物化 bytes、截断标记和原因。"""
        if self._max_file_bytes is None or len(content) <= self._max_file_bytes:
            return content, False, None
        return (
            content[: self._max_file_bytes],
            True,
            f"文件超过 max_file_bytes={self._max_file_bytes}，仅物化前 {self._max_file_bytes} bytes",
        )

    def _write_json(self, path: Path, payload: object) -> None:
        """原子写入 JSON，输入为目标路径和 payload，输出为目标文件更新。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), "utf-8")
        tmp.replace(path)


def _resolve_input_file(*, input_root: Path, raw_file_path: str) -> Path:
    """解析 shard 文件路径，输入为输入根和原始路径，输出为位于 input_root 内的绝对文件路径。"""
    raw_path = Path(raw_file_path).expanduser()
    candidate = raw_path if raw_path.is_absolute() else input_root / raw_path
    source = candidate.resolve()
    if not _is_relative_to(source, input_root):
        raise ValueError(f"map_reduce shard file is outside input root: {raw_file_path}")
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"map_reduce shard file does not exist: {source}")
    return source


def _content_digest(content: bytes) -> str:
    """计算内容摘要，输入为文件 bytes，输出为 sha256 摘要字符串。"""
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _is_relative_to(path: Path, root: Path) -> bool:
    """判断路径归属，输入为候选路径和根目录，输出为是否位于根目录内。"""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _now_iso() -> str:
    """生成当前时间，输入为空，输出为 UTC ISO 8601 字符串。"""
    return datetime.now(UTC).isoformat()


__all__ = ["MapperInputMaterializer"]
