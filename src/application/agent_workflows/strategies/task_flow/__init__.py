"""task_flow workflow 策略包公开入口。

本脚本汇总 Task Flow 策略的公开类和 parser。
作用是让 AgentWorkflowManager 注册 TaskFlowStrategy，并让测试通过包路径导入策略合同。
关键执行流程：导入 strategy 中的 TaskFlowStrategy 和 parse_task_flow_spec 并放入 __all__。
关键类：TaskFlowStrategy 负责计划创建和进度 artifact 物化。
"""

from __future__ import annotations

from application.agent_workflows.strategies.task_flow.strategy import (
    TaskFlowStrategy,
    parse_task_flow_spec,
)

__all__ = ["TaskFlowStrategy", "parse_task_flow_spec"]
