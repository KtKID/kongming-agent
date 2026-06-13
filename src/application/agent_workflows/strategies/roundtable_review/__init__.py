"""roundtable_review 策略包公开入口。

本脚本汇总 Multi-Agent Roundtable Review 策略的公开类。
作用是让 AgentWorkflowManager 注册 RoundtableReviewStrategy，并让测试通过包路径导入。
关键执行流程：导入 strategy 中的 RoundtableReviewStrategy 并放入 __all__。
关键类：RoundtableReviewStrategy 负责圆桌评审状态机。
"""

from __future__ import annotations

from application.agent_workflows.strategies.roundtable_review.strategy import (
    RoundtableReviewStrategy,
)

__all__ = ["RoundtableReviewStrategy"]
