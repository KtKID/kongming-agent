"""MapReduce mapper 输出校验入口。

本脚本为新版 `strategies/map_reduce/` 策略包提供 mapper 输出校验门面。
作用是复用旧 `map_reduce_contracts.py` 中已经稳定的 JSON 提取、字段校验和错误收敛逻辑。
关键执行流程：策略传入 mapper final content 和期望 shard ID，本脚本代理旧 validator，返回 MapperValidationResult。
关键函数：MapReduceMapperOutputValidator.validate 代理类校验，validate_mapper_output 代理函数校验。
"""

from __future__ import annotations

from executors.agent_runtime.strategies.map_reduce_contracts import (
    MapperOutputValidator as _LegacyMapperOutputValidator,
)
from executors.agent_runtime.strategies.map_reduce_contracts import (
    MapperValidationResult,
)
from executors.agent_runtime.strategies.map_reduce_contracts import (
    validate_mapper_output as _validate_mapper_output,
)


class MapReduceMapperOutputValidator:
    """MapReduce mapper 输出校验门面。"""

    def __init__(self, validator: _LegacyMapperOutputValidator | None = None) -> None:
        """初始化校验器，输入为可选旧 validator，输出为可复用的新版门面实例。"""
        self._validator = validator or _LegacyMapperOutputValidator()

    def validate(self, content: str, *, expected_shard_id: str = "") -> MapperValidationResult:
        """校验 mapper 输出，输入为 final content 和期望 shard ID，输出为结构化校验结果。"""
        return self._validator.validate(content, expected_shard_id=expected_shard_id)


def validate_mapper_output(content: str, *, expected_shard_id: str) -> MapperValidationResult:
    """校验 mapper 输出，输入为 final content 和期望 shard ID，输出为结构化校验结果。"""
    return _validate_mapper_output(content, expected_shard_id=expected_shard_id)


__all__ = ["MapReduceMapperOutputValidator", "MapperValidationResult", "validate_mapper_output"]
