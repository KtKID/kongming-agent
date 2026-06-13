"""代码评审 roundtable 内置角色。

本脚本负责提供 roundtable_review 的 code review 预设角色。
作用是把原先写在 contracts.py 的固定 reviewer 迁出为可被 AgentRoleManager
统一管理和选择的内置 role preset。
关键执行流程：code_review_role_presets 返回 AgentRolePreset，AgentRoleManager
装配这些内置角色，roundtable 通过 participants.select 解析成 reviewer 任务。
关键函数：code_review_role_presets 返回角色库用 preset。
"""

from __future__ import annotations

from application.agent_roles import AgentRolePreset


def code_review_role_presets() -> tuple[AgentRolePreset, ...]:
    """返回代码评审内置角色，输入为空，输出 role preset 元组。"""
    return (
        AgentRolePreset(
            role_id="architecture_reviewer",
            title="架构 Agent",
            role="从模块边界、公开门户、依赖方向、扩展点和演进成本审查设计。",
        ),
        AgentRolePreset(
            role_id="code_quality_reviewer",
            title="代码质量 Agent",
            role="从命名、一致性、抽象层级、复杂度、可读性和维护成本审查实现。",
        ),
        AgentRolePreset(
            role_id="test_reviewer",
            title="测试 Agent",
            role="从测试入口、边界条件、回归风险、可观测断言和缺失用例审查方案。",
        ),
        AgentRolePreset(
            role_id="performance_reviewer",
            title="性能 Agent",
            role="从热路径、IO 次数、缓存、并发、资源占用和规模上限审查风险。",
        ),
        AgentRolePreset(
            role_id="safety_stability_reviewer",
            title="安全/稳定性 Agent",
            role="从权限边界、异常处理、数据一致性、失败恢复和误用防护审查风险。",
        ),
    )


__all__ = ["code_review_role_presets"]
