"""map_reduce 细节产物写入器。

本脚本负责写入 workflow 根目录下的 `map_reduce/` 专属产物。
作用是把 shard 计划、mapper 输出索引和 reducer 最终结果分别落盘，供审计、复跑和 Web 投影读取。
关键执行流程：创建 MapReduceArtifactWriter，调用 write_shards、write_mapper_index、write_reducer_result 或 write_all 写入三个 JSON 文件。
关键函数：write_shards 写 map_reduce/shards.json，write_mapper_index 写 map_reduce/mappers/index.json，write_reducer_result 写 map_reduce/reducer/result.json。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from executors.agent_runtime.strategies.map_reduce_contracts import (
    MapShard,
    ReducerOutput,
)
from executors.agent_runtime.subagent_permissions import to_jsonable


@dataclass(frozen=True)
class MapReduceArtifactPaths:
    """map_reduce 三类细节产物路径。"""

    # 分片计划 JSON 路径。
    shards_path: Path
    # mapper 索引 JSON 路径。
    mapper_index_path: Path
    # reducer 结果 JSON 路径。
    reducer_result_path: Path


class MapReduceArtifactWriter:
    """写入 `workflow_dir/map_reduce/` 下的 map_reduce 细节产物。"""

    def __init__(self, *, workflow_dir: Path) -> None:
        """初始化 writer，输入为 workflow 目录，输出为绑定 map_reduce 子目录的 writer。"""
        self._workflow_dir = workflow_dir.expanduser().resolve()

    @property
    def map_reduce_dir(self) -> Path:
        """返回 map_reduce 产物目录，输入为空，输出为绝对路径。"""
        return self._workflow_dir / "map_reduce"

    @property
    def shards_path(self) -> Path:
        """返回 shards.json 路径，输入为空，输出为绝对路径。"""
        return self.map_reduce_dir / "shards.json"

    @property
    def mapper_index_path(self) -> Path:
        """返回 mapper index 路径，输入为空，输出为绝对路径。"""
        return self.map_reduce_dir / "mappers" / "index.json"

    @property
    def reducer_result_path(self) -> Path:
        """返回 reducer result 路径，输入为空，输出为绝对路径。"""
        return self.map_reduce_dir / "reducer" / "result.json"

    def write_shards(self, shards: Sequence[MapShard]) -> Path:
        """写入分片计划，输入为 shard 序列，输出为 shards.json 路径。"""
        payload = {
            "written_at": _now_iso(),
            "shard_count": len(shards),
            "shards": to_jsonable(tuple(shards)),
        }
        self._write_json(self.shards_path, payload)
        return self.shards_path

    def write_mapper_index(self, mapper_records: Sequence[Any]) -> Path:
        """写入 mapper 索引，输入为 mapper 运行或校验摘要序列，输出为 index.json 路径。"""
        payload = {
            "written_at": _now_iso(),
            "mapper_count": len(mapper_records),
            "mappers": to_jsonable(tuple(mapper_records)),
        }
        self._write_json(self.mapper_index_path, payload)
        return self.mapper_index_path

    def write_reducer_result(self, reducer_output: ReducerOutput | Any) -> Path:
        """写入 reducer 结果，输入为 ReducerOutput 或兼容 payload，输出为 result.json 路径。"""
        self._write_json(self.reducer_result_path, to_jsonable(reducer_output))
        return self.reducer_result_path

    def write_all(
        self,
        *,
        shards: Sequence[MapShard],
        mapper_records: Sequence[Any],
        reducer_output: ReducerOutput | Any,
    ) -> MapReduceArtifactPaths:
        """一次写入全部产物，输入为 shards、mapper 记录和 reducer 输出，输出为三类路径。"""
        return MapReduceArtifactPaths(
            shards_path=self.write_shards(shards),
            mapper_index_path=self.write_mapper_index(mapper_records),
            reducer_result_path=self.write_reducer_result(reducer_output),
        )

    def _write_json(self, path: Path, payload: object) -> None:
        """原子写入 JSON，输入为路径和 payload，输出为目标文件更新。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), "utf-8")
        tmp.replace(path)


def _now_iso() -> str:
    """生成当前时间，输入为空，输出为 UTC ISO 8601 字符串。"""
    return datetime.now(UTC).isoformat()


__all__ = ["MapReduceArtifactPaths", "MapReduceArtifactWriter"]
