"""内置节点模板与预置工作流定义。"""

from __future__ import annotations

from hosts.web.workflow.models import (
    EdgeTrigger,
    NodeCategory,
    NodeTemplate,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
)

# ── 内置节点模板 ───────────────────────────────────────────

_NODE_TEMPLATES: dict[str, NodeTemplate] = {
    t.id: t
    for t in [
        NodeTemplate(
            id="x-spec",
            name="Spec 规划",
            category=NodeCategory.ANALYSIS,
            detection_file="01-goals-and-boundaries.md",
        ),
        NodeTemplate(
            id="x-req",
            name="需求拆解",
            category=NodeCategory.PLANNING,
            detection_file="dev-checklist.md",
        ),
        NodeTemplate(
            id="x-dev",
            name="开发执行",
            category=NodeCategory.EXECUTION,
            detection_file="dev-report.md",
        ),
        NodeTemplate(
            id="x-verify",
            name="事实验证",
            category=NodeCategory.GATE,
            detection_file="reports/verify/verify-report-*.md",
        ),
        NodeTemplate(
            id="x-qa-gate",
            name="质量门禁",
            category=NodeCategory.GATE,
            detection_file="reports/qa-gate/qa-gate-report-*.md",
        ),
        NodeTemplate(
            id="x-fix",
            name="修复",
            category=NodeCategory.REPAIR,
            detection_file="reports/fix/fix-*.md",
        ),
    ]
}

# ── 预置工作流 ─────────────────────────────────────────────

_PRESET_DEFINITIONS: list[WorkflowDefinition] = [
    WorkflowDefinition(
        id="full-pipeline",
        name="完整流水线",
        description="x-spec → x-req → x-dev → x-verify → x-qa-gate，qa-gate 失败走 x-fix 修复后回到 x-verify",
        nodes=[
            WorkflowNode(id="n-spec", template_id="x-spec", position=0),
            WorkflowNode(id="n-req", template_id="x-req", position=1),
            WorkflowNode(id="n-dev", template_id="x-dev", position=2),
            WorkflowNode(id="n-verify", template_id="x-verify", position=3),
            WorkflowNode(id="n-qa-gate", template_id="x-qa-gate", position=4),
            WorkflowNode(id="n-fix", template_id="x-fix", position=5),
        ],
        edges=[
            WorkflowEdge(id="e-spec-req", from_node_id="n-spec", to_node_id="n-req"),
            WorkflowEdge(id="e-req-dev", from_node_id="n-req", to_node_id="n-dev"),
            WorkflowEdge(id="e-dev-verify", from_node_id="n-dev", to_node_id="n-verify"),
            WorkflowEdge(id="e-verify-qa", from_node_id="n-verify", to_node_id="n-qa-gate"),
            WorkflowEdge(
                id="e-qa-fix",
                from_node_id="n-qa-gate",
                to_node_id="n-fix",
                trigger=EdgeTrigger.ON_FAILED,
            ),
            WorkflowEdge(
                id="e-fix-verify",
                from_node_id="n-fix",
                to_node_id="n-verify",
                trigger=EdgeTrigger.ON_COMPLETED,
            ),
        ],
        is_preset=True,
    ),
    WorkflowDefinition(
        id="quick-dev",
        name="快速开发",
        description="x-req → x-dev → x-verify，适合小功能快速迭代",
        nodes=[
            WorkflowNode(id="n-req", template_id="x-req", position=0),
            WorkflowNode(id="n-dev", template_id="x-dev", position=1),
            WorkflowNode(id="n-verify", template_id="x-verify", position=2),
        ],
        edges=[
            WorkflowEdge(id="e-req-dev", from_node_id="n-req", to_node_id="n-dev"),
            WorkflowEdge(id="e-dev-verify", from_node_id="n-dev", to_node_id="n-verify"),
        ],
        is_preset=True,
    ),
]


# ── 公开接口 ───────────────────────────────────────────────


def get_node_templates() -> dict[str, NodeTemplate]:
    return dict(_NODE_TEMPLATES)


def get_preset_definitions() -> list[WorkflowDefinition]:
    return list(_PRESET_DEFINITIONS)
