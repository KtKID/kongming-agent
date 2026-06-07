"""map_reduce 旧契约文件的包内 re-export。

本脚本负责把已落地的 map_reduce_contracts.py 暴露到 strategies/map_reduce/contracts.py。
作用是让新策略包拥有本地 contracts 入口，同时保持旧 import 和旧单测稳定。
关键执行流程：导入旧契约模块，转发其公开符号，后续清理 task 可再迁移实现位置。
关键对象：MapReduceWorkflowSpec、MapShard、MapperOutputEnvelope、ReducerOutput 和校验函数。
"""

from __future__ import annotations

from executors.agent_runtime.strategies import map_reduce_contracts as _contracts
from executors.agent_runtime.strategies.map_reduce_contracts import *  # noqa: F403

__all__ = _contracts.__all__
