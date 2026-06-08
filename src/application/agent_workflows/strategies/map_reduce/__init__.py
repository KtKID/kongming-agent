"""map_reduce workflow 策略包。

本脚本汇总 map_reduce 策略的公开入口。
作用是让 AgentWorkflowManager 可以注册 MapReduceStrategy，并让测试按包路径导入 helper。
关键执行流程：strategy 解析 payload，planner 生成分片，materializer 准备输入，mapper builder 生成 prompt，validator 校验输出，reducer 归并结果，artifact writer 写细节产物。
关键类：MapReduceStrategy 负责 workflow 状态机。
"""

from __future__ import annotations

from application.agent_workflows.strategies.map_reduce.strategy import MapReduceStrategy

__all__ = ["MapReduceStrategy"]
