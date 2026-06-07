"""工作流运行子包。

本脚本标记 workflow 子包，并集中说明该子包承载 AgentWorkflowManager facade、workflow 执行上下文和审计结果收口。
作用是把 workflow 生命周期管理从 agent_runtime 根目录中独立出来，便于和具体策略实现分层维护。
关键执行流程：上层装配导入 manager，manager 创建 context 并调用 strategies 子包内的策略，最终写入 workflow 产物。
关键函数：本脚本只提供包标记和导出边界，无独立函数。
"""

from __future__ import annotations

__all__: list[str] = []
